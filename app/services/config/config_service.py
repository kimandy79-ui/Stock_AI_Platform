"""ConfigService (M11) — setup-mode config store.

Setup-mode migration (AD-22.19–22.24):
- Primary config tables: setup_configs (4 rows, one per setup_type) + risk_label_config.
- strategy_configs / runtime_configs / config_activation_log are REMOVED from the
  active schema; the old M21 strategy-config API is retired.
- Activation constraint (AD-22.21 / 01b §14): exactly one active_flag=TRUE per
  setup_type in prod/debug; exactly one active risk_label_config row. Enforced here.
- sector_alias_map seeding is retained (unchanged from legacy).

Boundaries:
- All DB access via injected db_manager (no import duckdb).
- Returns ServiceResult on every path.
- No Streamlit / provider / dashboard logic.
- Writes only to setup_configs, risk_label_config, sector_alias_map.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Final, Protocol

from app.services.config import config_validator as cv
from app.services.config import default_configs
from app.utils import logging_config
from app.utils import service_result
from app.utils.service_result import ServiceResult

_LOG = logging_config.get_logger(__name__)

SEED_VERSION: Final[str] = default_configs.SEED_VERSION
SEED_CREATED_BY: Final[str] = default_configs.SEED_CREATED_BY


class _DbManagerLike(Protocol):
    def connect(self, db_role: str, read_only: bool = ...) -> Any: ...


# --------------------------------------------------------------------------- #
# SQL (setup_configs + risk_label_config + sector_alias_map)
# --------------------------------------------------------------------------- #
_INSERT_SETUP_CONFIG: Final[str] = (
    "INSERT INTO setup_configs "
    "(config_id, setup_type, version, parent_config_id, config_json, config_hash, "
    " active_flag, created_at, notes) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, CAST(now() AS TIMESTAMP), ?) "
    "ON CONFLICT (config_id) DO NOTHING "
    "RETURNING config_id"
)

_SELECT_ACTIVE_SETUP_CONFIG: Final[str] = (
    "SELECT config_id, setup_type, version, config_json, config_hash "
    "FROM setup_configs WHERE setup_type = ? AND active_flag = TRUE LIMIT 1"
)

_SELECT_ALL_ACTIVE_SETUP_CONFIGS: Final[str] = (
    "SELECT config_id, setup_type, version, config_json, config_hash "
    "FROM setup_configs WHERE active_flag = TRUE ORDER BY setup_type"
)

_COUNT_ACTIVE_SETUP: Final[str] = (
    "SELECT COUNT(*) FROM setup_configs WHERE setup_type = ? AND active_flag = TRUE"
)

_DEACTIVATE_SETUP_SIBLINGS: Final[str] = (
    "UPDATE setup_configs SET active_flag = FALSE WHERE setup_type = ?"
)

_ACTIVATE_SETUP_CONFIG: Final[str] = (
    "UPDATE setup_configs SET active_flag = TRUE WHERE config_id = ?"
)

_INSERT_RISK_LABEL_CONFIG: Final[str] = (
    "INSERT INTO risk_label_config "
    "(config_id, version, config_json, config_hash, active_flag, created_at, notes) "
    "VALUES (?, ?, ?, ?, ?, CAST(now() AS TIMESTAMP), ?) "
    "ON CONFLICT (config_id) DO NOTHING "
    "RETURNING config_id"
)

_SELECT_ACTIVE_RISK_LABEL_CONFIG: Final[str] = (
    "SELECT config_id, version, config_json, config_hash "
    "FROM risk_label_config WHERE active_flag = TRUE LIMIT 1"
)

_COUNT_ACTIVE_RISK_LABEL: Final[str] = (
    "SELECT COUNT(*) FROM risk_label_config WHERE active_flag = TRUE"
)

_DEACTIVATE_ALL_RISK_LABEL: Final[str] = (
    "UPDATE risk_label_config SET active_flag = FALSE"
)

_ACTIVATE_RISK_LABEL_CONFIG: Final[str] = (
    "UPDATE risk_label_config SET active_flag = TRUE WHERE config_id = ?"
)

_INSERT_SECTOR_ALIAS: Final[str] = (
    "INSERT INTO sector_alias_map "
    "(source, raw_sector, canonical_sector, active_flag, created_at) "
    "VALUES (?, ?, ?, TRUE, CAST(now() AS TIMESTAMP)) "
    "ON CONFLICT (source, raw_sector) DO NOTHING "
    "RETURNING raw_sector"
)


def _loads(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def _m14_fundamentals_active(setup_config: dict[str, Any]) -> bool:
    """True when M14 would fold a fundamentals adjustment into setup_score.

    Matches ``m14_setup_validators._compute_fundamentals_adjustment``'s own
    activation test: ``enabled`` truthy *and* a non-zero ``weight``.
    """
    block = setup_config.get("fundamentals") or {}
    if not block.get("enabled", False):
        return False
    try:
        return float(block.get("weight", 0.0)) != 0.0
    except (TypeError, ValueError):
        return False


def _step5_fundamentals_weight(risk_label_config: dict[str, Any] | None) -> float:
    """Effective Step 5 fundamentals term weight; seeded default when unspecified."""
    if risk_label_config is None:
        risk_label_config = default_configs.DEFAULT_RISK_LABEL_CONFIG
    block = risk_label_config.get("fundamentals") or {}
    try:
        return float(block.get("score_weight", 0.0))
    except (TypeError, ValueError):
        return 0.0


class ConfigService:
    """Setup-mode versioned config store (setup_configs + risk_label_config)."""

    def __init__(self, db_manager: _DbManagerLike | None = None) -> None:
        if db_manager is None:
            from app.database import duckdb_manager
            db_manager = duckdb_manager
        self._db = db_manager

    def _query(self, db_role: str, sql: str, params: list[Any]) -> list[tuple]:
        connection = self._db.connect(db_role)
        try:
            return connection.execute(sql, params).fetchall()
        finally:
            connection.close()

    # ------------------------------------------------------------------ #
    # Setup config seeding
    # ------------------------------------------------------------------ #
    def seed_default_setup_configs(self, db_role: str) -> ServiceResult:
        """Seed the four active setup configs. Idempotent via ON CONFLICT."""
        run_id = str(uuid.uuid4())
        try:
            cv.validate_db_role(db_role)
        except cv.ConfigValidationError as exc:
            return self._failed(run_id, str(exc), {"db_role": db_role})

        defaults = default_configs.get_default_setup_configs()
        inserted = 0
        connection = self._db.connect(db_role)
        try:
            connection.execute("BEGIN TRANSACTION")
            try:
                for setup_type, payload in defaults.items():
                    cv.validate_setup_type(setup_type)
                    cfg = cv.validate_config_payload(payload)
                    config_hash = cv.deterministic_hash(cfg)
                    config_id = payload.get("config_id", f"setup_{setup_type}_v1")
                    version = payload.get("version", "v1")
                    returned = connection.execute(
                        _INSERT_SETUP_CONFIG,
                        [
                            config_id,
                            setup_type,
                            version,
                            None,
                            cv.canonical_json(cfg),
                            config_hash,
                            True,
                            "seeded default",
                        ],
                    ).fetchall()
                    inserted += len(returned)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        except Exception as exc:  # noqa: BLE001
            _LOG.error("seed_default_setup_configs failed: %s", exc)
            return self._failed(run_id, f"{type(exc).__name__}: {exc}", {"db_role": db_role})
        finally:
            connection.close()

        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=inserted,
            metadata={
                "db_role": db_role,
                "config_kind": "setup",
                "seeded": inserted,
                "requested": len(defaults),
            },
        )

    # ------------------------------------------------------------------ #
    # Preset setup config seeding (Phase 1.5 — simulation sweep inputs only)
    # ------------------------------------------------------------------ #
    def seed_preset_setup_configs(self, db_role: str) -> ServiceResult:
        """Seed literature-anchored preset setup configs. Idempotent via ON CONFLICT.

        Unlike ``seed_default_setup_configs``, presets are always inserted with
        ``active_flag=False`` and this method never calls ``activate_setup_config``
        for any of them — they exist purely as simulation-sweep variant inputs
        and must never affect prod/debug's one-active-per-setup_type invariant.
        """
        run_id = str(uuid.uuid4())
        try:
            cv.validate_db_role(db_role)
        except cv.ConfigValidationError as exc:
            return self._failed(run_id, str(exc), {"db_role": db_role})

        presets = default_configs.get_preset_setup_configs()
        inserted = 0
        connection = self._db.connect(db_role)
        try:
            connection.execute("BEGIN TRANSACTION")
            try:
                for payload in presets:
                    setup_type = payload["setup_type"]
                    cv.validate_setup_type(setup_type)
                    cfg = cv.validate_config_payload(payload)
                    config_hash = cv.deterministic_hash(cfg)
                    config_id = payload["config_id"]
                    version = payload["version"]
                    parent_config_id = payload.get("parent_config_id")
                    returned = connection.execute(
                        _INSERT_SETUP_CONFIG,
                        [
                            config_id,
                            setup_type,
                            version,
                            parent_config_id,
                            cv.canonical_json(cfg),
                            config_hash,
                            False,  # never active — simulation-sweep input only
                            "seeded preset (simulation sweep)",
                        ],
                    ).fetchall()
                    inserted += len(returned)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        except Exception as exc:  # noqa: BLE001
            _LOG.error("seed_preset_setup_configs failed: %s", exc)
            return self._failed(run_id, f"{type(exc).__name__}: {exc}", {"db_role": db_role})
        finally:
            connection.close()

        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=inserted,
            metadata={
                "db_role": db_role,
                "config_kind": "setup_preset",
                "seeded": inserted,
                "requested": len(presets),
            },
        )

    # ------------------------------------------------------------------ #
    # Risk label config seeding
    # ------------------------------------------------------------------ #
    def seed_default_risk_label_config(self, db_role: str) -> ServiceResult:
        """Seed the single active risk-label config. Idempotent."""
        run_id = str(uuid.uuid4())
        try:
            cv.validate_db_role(db_role)
        except cv.ConfigValidationError as exc:
            return self._failed(run_id, str(exc), {"db_role": db_role})

        payload = default_configs.get_default_risk_label_config()
        inserted = 0
        connection = self._db.connect(db_role)
        try:
            connection.execute("BEGIN TRANSACTION")
            try:
                cfg = cv.validate_config_payload(payload)
                config_hash = cv.deterministic_hash(cfg)
                config_id = payload.get("config_id", cv.RISK_LABEL_CONFIG_SEED_ID)
                version = payload.get("version", "risk_v1")
                returned = connection.execute(
                    _INSERT_RISK_LABEL_CONFIG,
                    [
                        config_id,
                        version,
                        cv.canonical_json(cfg),
                        config_hash,
                        True,
                        "seeded default",
                    ],
                ).fetchall()
                inserted += len(returned)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        except Exception as exc:  # noqa: BLE001
            _LOG.error("seed_default_risk_label_config failed: %s", exc)
            return self._failed(run_id, f"{type(exc).__name__}: {exc}", {"db_role": db_role})
        finally:
            connection.close()

        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=inserted,
            metadata={
                "db_role": db_role,
                "config_kind": "risk_label",
                "seeded": inserted,
            },
        )

    def seed_risk_label_config_v2(self, db_role: str) -> ServiceResult:
        """Seed risk_label_config_v2 (CODER_NOTE v3 item 6). Idempotent via ON CONFLICT.

        Unlike ``seed_default_risk_label_config``, this always inserts with
        ``active_flag=False`` and never calls ``activate_risk_label_config`` —
        the new shared earnings/macro block only takes effect once a human
        explicitly activates this version, per the immutable clone-and-version
        rule (CLAUDE.md: exactly one active risk_label_config in prod/debug).
        """
        run_id = str(uuid.uuid4())
        try:
            cv.validate_db_role(db_role)
        except cv.ConfigValidationError as exc:
            return self._failed(run_id, str(exc), {"db_role": db_role})

        payload = default_configs.get_risk_label_config_v2()
        inserted = 0
        connection = self._db.connect(db_role)
        try:
            connection.execute("BEGIN TRANSACTION")
            try:
                cfg = cv.validate_config_payload(payload)
                config_hash = cv.deterministic_hash(cfg)
                config_id = payload["config_id"]
                version = payload["version"]
                returned = connection.execute(
                    _INSERT_RISK_LABEL_CONFIG,
                    [
                        config_id,
                        version,
                        cv.canonical_json(cfg),
                        config_hash,
                        False,
                        "seeded v2 (inactive; shared earnings/macro block)",
                    ],
                ).fetchall()
                inserted += len(returned)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        except Exception as exc:  # noqa: BLE001
            _LOG.error("seed_risk_label_config_v2 failed: %s", exc)
            return self._failed(run_id, f"{type(exc).__name__}: {exc}", {"db_role": db_role})
        finally:
            connection.close()

        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=inserted,
            metadata={
                "db_role": db_role,
                "config_kind": "risk_label_v2",
                "seeded": inserted,
            },
        )

    # ------------------------------------------------------------------ #
    # Sector alias seeding (retained)
    # ------------------------------------------------------------------ #
    def seed_sector_alias_map(self, db_role: str) -> ServiceResult:
        """Seed sector_alias_map from constants.SECTOR_ALIAS_MAP."""
        run_id = str(uuid.uuid4())
        try:
            cv.validate_db_role(db_role)
        except cv.ConfigValidationError as exc:
            return self._failed(run_id, str(exc), {"db_role": db_role})

        seeds = default_configs.get_sector_alias_seeds()
        inserted = 0
        connection = self._db.connect(db_role)
        try:
            connection.execute("BEGIN TRANSACTION")
            try:
                for source, raw_sector, canonical_sector in seeds:
                    returned = connection.execute(
                        _INSERT_SECTOR_ALIAS, [source, raw_sector, canonical_sector]
                    ).fetchall()
                    inserted += len(returned)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        except Exception as exc:  # noqa: BLE001
            _LOG.error("seed_sector_alias_map failed: %s", exc)
            return self._failed(run_id, f"{type(exc).__name__}: {exc}", {"db_role": db_role})
        finally:
            connection.close()

        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=inserted,
            metadata={"db_role": db_role, "config_kind": "sector_alias", "seeded": inserted},
        )

    def seed_defaults(self, db_role: str) -> ServiceResult:
        """Seed setup configs + risk-label config + sector aliases. Idempotent."""
        run_id = str(uuid.uuid4())
        setup = self.seed_default_setup_configs(db_role)
        risk = self.seed_default_risk_label_config(db_role)
        sectors = self.seed_sector_alias_map(db_role)
        errors = [e for r in (setup, risk, sectors) for e in r.errors]
        status = service_result.STATUS_FAILED if errors else service_result.STATUS_SUCCESS
        return ServiceResult(
            status=status,
            run_id=run_id,
            rows_processed=setup.rows_processed + risk.rows_processed + sectors.rows_processed,
            errors=errors,
            metadata={
                "db_role": db_role,
                "setup_seeded": setup.rows_processed,
                "risk_label_seeded": risk.rows_processed,
                "sector_alias_seeded": sectors.rows_processed,
            },
        )

    # ------------------------------------------------------------------ #
    # Setup config reads
    # ------------------------------------------------------------------ #
    def get_active_setup_config(self, db_role: str, setup_type: str) -> ServiceResult:
        """Return the active setup config for the given setup_type."""
        run_id = str(uuid.uuid4())
        try:
            cv.validate_db_role(db_role)
            cv.validate_setup_type(setup_type)
        except cv.ConfigValidationError as exc:
            return self._failed(run_id, str(exc), {"db_role": db_role})

        try:
            rows = self._query(db_role, _SELECT_ACTIVE_SETUP_CONFIG, [setup_type])
        except Exception as exc:  # noqa: BLE001
            return self._failed(run_id, f"{type(exc).__name__}: {exc}", {"db_role": db_role})

        if not rows:
            return self._failed(
                run_id,
                f"no active setup config for setup_type={setup_type!r} (db_role={db_role!r})",
                {"db_role": db_role, "setup_type": setup_type},
            )

        config_id, _st, _ver, config_json, config_hash = rows[0]
        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=1,
            metadata={
                "db_role": db_role,
                "setup_type": setup_type,
                "config_id": config_id,
                "config_json": _loads(config_json),
                "config_hash": config_hash,
            },
        )

    def get_all_active_setup_configs(self, db_role: str) -> ServiceResult:
        """Return all active setup configs as {setup_type: config_json}."""
        run_id = str(uuid.uuid4())
        try:
            cv.validate_db_role(db_role)
        except cv.ConfigValidationError as exc:
            return self._failed(run_id, str(exc), {"db_role": db_role})

        try:
            rows = self._query(db_role, _SELECT_ALL_ACTIVE_SETUP_CONFIGS, [])
        except Exception as exc:  # noqa: BLE001
            return self._failed(run_id, f"{type(exc).__name__}: {exc}", {"db_role": db_role})

        configs_by_type: dict[str, dict[str, Any]] = {}
        configs_by_id: dict[str, dict[str, Any]] = {}
        for config_id, setup_type, _ver, config_json, _hash in rows:
            parsed = _loads(config_json)
            configs_by_type[setup_type] = parsed
            configs_by_id[config_id] = parsed

        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=len(configs_by_id),
            metadata={
                "db_role": db_role,
                "configs": configs_by_type,
                "configs_by_type": configs_by_type,
                "configs_by_id": configs_by_id,
            },
        )

    def assert_universe_config_parity(self, db_role: str) -> ServiceResult:
        """Assert all four active setup configs have identical universe blocks.

        AD-22.23 / 01d Module 13: universe config must be identical across all
        active setup configs. Divergence is a configuration error.
        """
        run_id = str(uuid.uuid4())
        all_result = self.get_all_active_setup_configs(db_role)
        if not all_result.is_ok():
            return all_result

        configs = all_result.metadata.get("configs_by_type", {})
        universe_blocks: dict[str, Any] = {}
        for setup_type, cfg in configs.items():
            universe_blocks[setup_type] = cfg.get("universe", {})

        if not universe_blocks:
            return self._failed(run_id, "no active setup configs found", {"db_role": db_role})

        # All universe blocks must be identical (canonical JSON comparison)
        canonical_blocks = [cv.canonical_json(b) for b in universe_blocks.values()]
        if len(set(canonical_blocks)) > 1:
            return self._failed(
                run_id,
                f"universe config parity mismatch across setup types: {list(universe_blocks)}",
                {"db_role": db_role, "universe_blocks": universe_blocks},
            )

        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=len(universe_blocks),
            metadata={"db_role": db_role, "setup_types_checked": list(universe_blocks)},
        )

    # ------------------------------------------------------------------ #
    # Setup config writes
    # ------------------------------------------------------------------ #
    def activate_setup_config(
        self,
        config_id: str,
        db_role: str,
        setup_type: str,
    ) -> ServiceResult:
        """Activate a setup config; deactivate all siblings for the same setup_type."""
        run_id = str(uuid.uuid4())
        try:
            cv.validate_db_role(db_role)
            cv.validate_setup_type(setup_type)
        except cv.ConfigValidationError as exc:
            return self._failed(run_id, str(exc), {"db_role": db_role})

        connection = self._db.connect(db_role)
        try:
            connection.execute("BEGIN TRANSACTION")
            try:
                connection.execute(_DEACTIVATE_SETUP_SIBLINGS, [setup_type])
                connection.execute(_ACTIVATE_SETUP_CONFIG, [config_id])
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        except Exception as exc:  # noqa: BLE001
            return self._failed(run_id, f"{type(exc).__name__}: {exc}", {"db_role": db_role})
        finally:
            connection.close()

        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=1,
            metadata={"db_role": db_role, "config_id": config_id, "setup_type": setup_type},
        )

    # ------------------------------------------------------------------ #
    # Risk label config reads
    # ------------------------------------------------------------------ #
    def get_active_risk_label_config(self, db_role: str) -> ServiceResult:
        """Return the active risk-label config."""
        run_id = str(uuid.uuid4())
        try:
            cv.validate_db_role(db_role)
        except cv.ConfigValidationError as exc:
            return self._failed(run_id, str(exc), {"db_role": db_role})

        try:
            rows = self._query(db_role, _SELECT_ACTIVE_RISK_LABEL_CONFIG, [])
        except Exception as exc:  # noqa: BLE001
            return self._failed(run_id, f"{type(exc).__name__}: {exc}", {"db_role": db_role})

        if not rows:
            return self._failed(
                run_id,
                f"no active risk_label_config (db_role={db_role!r})",
                {"db_role": db_role},
            )

        config_id, _ver, config_json, config_hash = rows[0]
        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=1,
            metadata={
                "db_role": db_role,
                "config_id": config_id,
                "config_json": _loads(config_json),
                "config_hash": config_hash,
            },
        )

    # ------------------------------------------------------------------ #
    # Validation helpers
    # ------------------------------------------------------------------ #
    def validate_setup_config(
        self,
        config_json: dict[str, Any],
        risk_label_config: dict[str, Any] | None = None,
    ) -> ServiceResult:
        """Validate a setup config payload (structure checks).

        ``risk_label_config`` supplies the counterpart config whose
        ``fundamentals.score_weight`` decides whether Step 5's own fundamentals
        term is active; when omitted the seeded default is assumed. It is only
        read for the double-credit check below.
        """
        run_id = str(uuid.uuid4())
        try:
            cfg = cv.validate_config_payload(config_json)
        except cv.ConfigValidationError as exc:
            return self._failed(run_id, str(exc), {})

        errors: list[str] = []
        setup_type = cfg.get("setup_type")
        try:
            cv.validate_setup_type(setup_type)
        except cv.ConfigValidationError as exc:
            errors.append(str(exc))

        # scoring_weights must sum to 1.0
        weights = cfg.get("scoring_weights", {})
        if weights:
            total = sum(weights.values())
            if abs(total - 1.0) > 1e-6:
                errors.append(f"scoring_weights sum {total:.6f} != 1.0")

        # AD-22.23: RVOL must never hard-reject a pullback setup. m14_setup_validators
        # .validate_pullback() silently overrides rvol_is_hard=True to False as a
        # runtime backstop, but that masks the mistake from whoever authored the
        # config. Reject it here instead, at authoring/clone time, so it's visible
        # before the config is ever created. This is a creation-time-only check —
        # confirmed via full-codebase search that validate_setup_config has no
        # callers in the seeding or pipeline read paths (only test callers today),
        # so it cannot retroactively invalidate any currently-active config.
        if setup_type == "pullback":
            pullback_rvol_is_hard = cfg.get("validation", {}).get("rvol_is_hard")
            if pullback_rvol_is_hard is True:
                errors.append(
                    "pullback config sets rvol_is_hard=True, which violates "
                    "AD-22.23 (RVOL must never hard-reject for pullback)"
                )

        # Double-credit guard. M14 folds its fundamentals adjustment into
        # setup_score; Step 5 weights setup_score at _W_SETUP *and* adds its own
        # term keyed by the same five ticker_fundamentals fields. With both
        # active the signal is counted twice (cf. m15_double_credit_bug_finding.md).
        # step5_proposal_engine._m14_owns_fundamentals suppresses the Step 5 term
        # at scoring time as a runtime backstop, but -- like the AD-22.23 check
        # above -- silently correcting a config hides the authoring mistake, so
        # reject it here where a human can still see it.
        if _m14_fundamentals_active(cfg) and _step5_fundamentals_weight(risk_label_config) != 0.0:
            errors.append(
                "setup_config sets fundamentals.enabled=True with a non-zero weight "
                "while risk_label_config.fundamentals.score_weight is non-zero; the "
                "same ticker_fundamentals fields would be scored twice (M14 folds "
                "them into setup_score, Step 5 adds its own term). Enable exactly "
                "one: either clear fundamentals.enabled here, or set "
                "risk_label_config.fundamentals.score_weight to 0.0"
            )

        if errors:
            return self._failed(run_id, "; ".join(errors), {"errors": errors})

        config_hash = cv.deterministic_hash(cfg)
        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=1,
            metadata={"config_hash": config_hash, "setup_type": setup_type},
        )

    def validate_risk_label_config(self, config_json: dict[str, Any]) -> ServiceResult:
        """Validate a risk-label config payload (structure checks)."""
        run_id = str(uuid.uuid4())
        try:
            cfg = cv.validate_config_payload(config_json)
        except cv.ConfigValidationError as exc:
            return self._failed(run_id, str(exc), {})

        errors: list[str] = []
        fw = cfg.get("factor_weights", {})
        if fw:
            total = sum(fw.values())
            if abs(total - 1.0) > 1e-6:
                errors.append(f"factor_weights sum {total:.6f} != 1.0")

        if errors:
            return self._failed(run_id, "; ".join(errors), {"errors": errors})

        config_hash = cv.deterministic_hash(cfg)
        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=1,
            metadata={"config_hash": config_hash},
        )

    # ------------------------------------------------------------------ #
    # Runtime config reads (retained for pipeline/provider/debug/sim/dashboard)
    # ------------------------------------------------------------------ #
    def get_active_runtime_config(self, db_role: str, config_type: str) -> ServiceResult:
        """Return active runtime config payload for config_type from default_configs.

        Note: runtime_configs table is NOT in the setup-mode schema (retired with
        strategy_configs). Runtime configs are served from in-memory defaults for
        Phase 1. If the table doesn't exist, fall back to defaults gracefully.
        """
        run_id = str(uuid.uuid4())
        try:
            cv.validate_db_role(db_role)
            cv.validate_config_type(config_type)
        except cv.ConfigValidationError as exc:
            return self._failed(run_id, str(exc), {"db_role": db_role})

        defaults = default_configs.get_default_runtime_configs()
        if config_type not in defaults:
            return self._failed(
                run_id,
                f"no runtime config for {config_type!r}",
                {"db_role": db_role, "config_type": config_type},
            )

        cfg = defaults[config_type]
        config_hash = cv.deterministic_hash(cfg)
        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=1,
            metadata={
                "db_role": db_role,
                "config_type": config_type,
                "config_id": f"default_{config_type}",
                "config_json": cfg,
                "config_hash": config_hash,
            },
        )

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _failed(run_id: str, message: str, metadata: dict[str, Any]) -> ServiceResult:
        return ServiceResult(
            status=service_result.STATUS_FAILED,
            run_id=run_id,
            rows_processed=0,
            errors=[message],
            metadata=metadata,
        )
