"""ConfigService (M21 Config Management Addendum §6).

DuckDB-backed, versioned configuration store for strategy and runtime settings.
This is the runtime source of truth for active configs; Python ``DEFAULT_*``
values are only used as fresh-DB seeds.

Boundaries (mirrors the other service modules):

- All DB access goes through the injected ``db_manager`` (no ``import duckdb``).
- Returns a :class:`ServiceResult` on every path.
- No Streamlit / provider / dashboard logic.
- Writes target only the config tables (``strategy_configs``,
  ``runtime_configs``, ``config_activation_log``, ``sector_alias_map``).

V1 scope: ``prod`` / ``debug`` roles only (see
:data:`app.services.config.config_validator.ALLOWED_CONFIG_DB_ROLES`).
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
    """Minimal DB-manager hook needed for test injection."""

    def connect(self, db_role: str, read_only: bool = ...) -> Any: ...


# --------------------------------------------------------------------------- #
# SQL (parameterized; operates only on the config tables; no DDL).
# --------------------------------------------------------------------------- #
_INSERT_STRATEGY: Final[str] = (
    "INSERT INTO strategy_configs "
    "(config_id, db_role, strategy_name, version, parent_config_id, "
    " config_json, config_hash, active_flag, created_at, created_by, notes) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, CAST(now() AS TIMESTAMP), ?, ?) "
    "ON CONFLICT (config_id) DO NOTHING "
    "RETURNING config_id"
)
_SELECT_ACTIVE_STRATEGY: Final[str] = (
    "SELECT config_id, strategy_name, version, config_json, config_hash "
    "FROM strategy_configs WHERE db_role = ? AND active_flag = TRUE "
    "ORDER BY strategy_name"
)
_SELECT_STRATEGY_BY_NAME_ACTIVE: Final[str] = (
    "SELECT COUNT(*) FROM strategy_configs "
    "WHERE db_role = ? AND strategy_name = ? AND active_flag = TRUE"
)
_LIST_STRATEGY: Final[str] = (
    "SELECT config_id, strategy_name, version, parent_config_id, "
    " config_hash, active_flag, created_at, created_by, notes "
    "FROM strategy_configs WHERE db_role = ? "
    "ORDER BY strategy_name, created_at"
)
_LIST_STRATEGY_BY_NAME: Final[str] = (
    "SELECT config_id, strategy_name, version, parent_config_id, "
    " config_hash, active_flag, created_at, created_by, notes "
    "FROM strategy_configs WHERE db_role = ? AND strategy_name = ? "
    "ORDER BY created_at"
)
_GET_STRATEGY: Final[str] = (
    "SELECT config_id, db_role, strategy_name, version, parent_config_id, "
    " config_json, config_hash, active_flag, created_at, created_by, notes "
    "FROM strategy_configs WHERE config_id = ? AND db_role = ?"
)
_GET_STRATEGY_NAME: Final[str] = (
    "SELECT strategy_name FROM strategy_configs "
    "WHERE config_id = ? AND db_role = ?"
)
_DEACTIVATE_STRATEGY_SIBLINGS: Final[str] = (
    "UPDATE strategy_configs SET active_flag = FALSE "
    "WHERE db_role = ? AND strategy_name = ?"
)
_ACTIVATE_STRATEGY: Final[str] = (
    "UPDATE strategy_configs SET active_flag = TRUE "
    "WHERE config_id = ? AND db_role = ?"
)

_INSERT_RUNTIME: Final[str] = (
    "INSERT INTO runtime_configs "
    "(config_id, db_role, config_type, version, parent_config_id, "
    " config_json, config_hash, active_flag, created_at, created_by, notes) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, CAST(now() AS TIMESTAMP), ?, ?) "
    "ON CONFLICT (config_id) DO NOTHING "
    "RETURNING config_id"
)
_SELECT_ACTIVE_RUNTIME: Final[str] = (
    "SELECT config_id, config_type, version, config_json, config_hash "
    "FROM runtime_configs "
    "WHERE db_role = ? AND config_type = ? AND active_flag = TRUE "
    "LIMIT 1"
)
_COUNT_ACTIVE_RUNTIME: Final[str] = (
    "SELECT COUNT(*) FROM runtime_configs "
    "WHERE db_role = ? AND config_type = ? AND active_flag = TRUE"
)
_LIST_RUNTIME: Final[str] = (
    "SELECT config_id, config_type, version, parent_config_id, "
    " config_hash, active_flag, created_at, created_by, notes "
    "FROM runtime_configs WHERE db_role = ? "
    "ORDER BY config_type, created_at"
)
_LIST_RUNTIME_BY_TYPE: Final[str] = (
    "SELECT config_id, config_type, version, parent_config_id, "
    " config_hash, active_flag, created_at, created_by, notes "
    "FROM runtime_configs WHERE db_role = ? AND config_type = ? "
    "ORDER BY created_at"
)
_GET_RUNTIME_TYPE: Final[str] = (
    "SELECT config_type FROM runtime_configs "
    "WHERE config_id = ? AND db_role = ?"
)
_DEACTIVATE_RUNTIME_SIBLINGS: Final[str] = (
    "UPDATE runtime_configs SET active_flag = FALSE "
    "WHERE db_role = ? AND config_type = ?"
)
_ACTIVATE_RUNTIME: Final[str] = (
    "UPDATE runtime_configs SET active_flag = TRUE "
    "WHERE config_id = ? AND db_role = ?"
)

_INSERT_ACTIVATION_LOG: Final[str] = (
    "INSERT INTO config_activation_log "
    "(activation_id, config_id, db_role, config_type, profile_name, "
    " activated_at, activated_by, reason) "
    "VALUES (?, ?, ?, ?, ?, CAST(now() AS TIMESTAMP), ?, ?)"
)

_INSERT_SECTOR_ALIAS: Final[str] = (
    "INSERT INTO sector_alias_map "
    "(source, raw_sector, canonical_sector, active_flag, created_at) "
    "VALUES (?, ?, ?, TRUE, CAST(now() AS TIMESTAMP)) "
    "ON CONFLICT (source, raw_sector) DO NOTHING "
    "RETURNING raw_sector"
)


def _seed_strategy_id(db_role: str, strategy_name: str) -> str:
    return f"seed_strategy_{db_role}_{strategy_name}_{SEED_VERSION}"


def _seed_runtime_id(db_role: str, config_type: str) -> str:
    return f"seed_runtime_{db_role}_{config_type}_{SEED_VERSION}"


def _loads(value: Any) -> Any:
    """Parse a JSON column value that may arrive as text or already-parsed."""
    if isinstance(value, str):
        return json.loads(value)
    return value


class ConfigService:
    """Versioned strategy/runtime config store (control plane, no trading logic)."""

    def __init__(self, db_manager: _DbManagerLike | None = None) -> None:
        if db_manager is None:
            from app.database import duckdb_manager

            db_manager = duckdb_manager
        self._db = db_manager

    # ------------------------------------------------------------------ #
    # DB helpers (open / execute / close — no long-held connection).
    # ------------------------------------------------------------------ #
    def _query(self, db_role: str, sql: str, params: list[Any]) -> list[tuple]:
        connection = self._db.connect(db_role, read_only=True)
        try:
            return connection.execute(sql, params).fetchall()
        finally:
            connection.close()

    # ------------------------------------------------------------------ #
    # Seeding (idempotent — deterministic seed config_id + ON CONFLICT).
    # ------------------------------------------------------------------ #
    def seed_default_strategy_configs(self, db_role: str) -> ServiceResult:
        """Seed normal/aggressive/conservative active strategy configs."""
        run_id = str(uuid.uuid4())
        try:
            cv.validate_db_role(db_role)
        except cv.ConfigValidationError as exc:
            return self._failed(run_id, str(exc), {"db_role": db_role})

        defaults = default_configs.get_default_strategy_configs()
        inserted = 0
        connection = self._db.connect(db_role)
        try:
            connection.execute("BEGIN TRANSACTION")
            try:
                for strategy_name, payload in defaults.items():
                    cv.validate_strategy_name(strategy_name)
                    cfg = cv.validate_config_payload(payload)
                    config_hash = cv.deterministic_hash(cfg)
                    config_id = _seed_strategy_id(db_role, strategy_name)
                    returned = connection.execute(
                        _INSERT_STRATEGY,
                        [
                            config_id,
                            db_role,
                            strategy_name,
                            SEED_VERSION,
                            None,
                            cv.canonical_json(cfg),
                            config_hash,
                            True,
                            SEED_CREATED_BY,
                            "seeded default",
                        ],
                    ).fetchall()
                    inserted += len(returned)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        except Exception as exc:  # noqa: BLE001 - surface as failed result
            _LOG.error("seed_default_strategy_configs failed: %s", exc)
            return self._failed(
                run_id, f"{type(exc).__name__}: {exc}", {"db_role": db_role}
            )
        finally:
            connection.close()

        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=inserted,
            metadata={
                "db_role": db_role,
                "config_kind": "strategy",
                "seeded": inserted,
                "requested": len(defaults),
            },
        )

    def seed_default_runtime_configs(self, db_role: str) -> ServiceResult:
        """Seed one active runtime config per required config_type."""
        run_id = str(uuid.uuid4())
        try:
            cv.validate_db_role(db_role)
        except cv.ConfigValidationError as exc:
            return self._failed(run_id, str(exc), {"db_role": db_role})

        defaults = default_configs.get_default_runtime_configs()
        inserted = 0
        connection = self._db.connect(db_role)
        try:
            connection.execute("BEGIN TRANSACTION")
            try:
                for config_type, payload in defaults.items():
                    cv.validate_config_type(config_type)
                    cfg = cv.validate_config_payload(payload)
                    config_hash = cv.deterministic_hash(cfg)
                    config_id = _seed_runtime_id(db_role, config_type)
                    returned = connection.execute(
                        _INSERT_RUNTIME,
                        [
                            config_id,
                            db_role,
                            config_type,
                            SEED_VERSION,
                            None,
                            cv.canonical_json(cfg),
                            config_hash,
                            True,
                            SEED_CREATED_BY,
                            "seeded default",
                        ],
                    ).fetchall()
                    inserted += len(returned)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        except Exception as exc:  # noqa: BLE001
            _LOG.error("seed_default_runtime_configs failed: %s", exc)
            return self._failed(
                run_id, f"{type(exc).__name__}: {exc}", {"db_role": db_role}
            )
        finally:
            connection.close()

        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=inserted,
            metadata={
                "db_role": db_role,
                "config_kind": "runtime",
                "seeded": inserted,
                "requested": len(defaults),
            },
        )

    def seed_sector_alias_map(self, db_role: str) -> ServiceResult:
        """Seed ``sector_alias_map`` from ``constants.SECTOR_ALIAS_MAP``."""
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
                        _INSERT_SECTOR_ALIAS,
                        [source, raw_sector, canonical_sector],
                    ).fetchall()
                    inserted += len(returned)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        except Exception as exc:  # noqa: BLE001
            _LOG.error("seed_sector_alias_map failed: %s", exc)
            return self._failed(
                run_id, f"{type(exc).__name__}: {exc}", {"db_role": db_role}
            )
        finally:
            connection.close()

        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=inserted,
            metadata={
                "db_role": db_role,
                "config_kind": "sector_alias",
                "seeded": inserted,
                "requested": len(seeds),
            },
        )

    def seed_defaults(self, db_role: str) -> ServiceResult:
        """Seed strategy + runtime + sector-alias defaults for ``db_role``.

        Convenience entry point for fresh DB initialization. ``sector_etf_map``
        is intentionally NOT seeded here: Module 07 remains its sole owner and
        seeds it from ``constants.SECTOR_ETF_MAP`` (already canonical).
        """
        run_id = str(uuid.uuid4())
        strat = self.seed_default_strategy_configs(db_role)
        runtime = self.seed_default_runtime_configs(db_role)
        sectors = self.seed_sector_alias_map(db_role)
        errors = [e for r in (strat, runtime, sectors) for e in r.errors]
        status = (
            service_result.STATUS_FAILED
            if errors
            else service_result.STATUS_SUCCESS
        )
        return ServiceResult(
            status=status,
            run_id=run_id,
            rows_processed=(
                strat.rows_processed
                + runtime.rows_processed
                + sectors.rows_processed
            ),
            errors=errors,
            metadata={
                "db_role": db_role,
                "strategy_seeded": strat.rows_processed,
                "runtime_seeded": runtime.rows_processed,
                "sector_alias_seeded": sectors.rows_processed,
            },
        )

    # ------------------------------------------------------------------ #
    # Strategy reads.
    # ------------------------------------------------------------------ #
    def get_active_strategy_configs(self, db_role: str) -> ServiceResult:
        """Return active strategy configs as ``{strategy_name: config_json}``."""
        run_id = str(uuid.uuid4())
        try:
            cv.validate_db_role(db_role)
        except cv.ConfigValidationError as exc:
            return self._failed(run_id, str(exc), {"db_role": db_role})

        try:
            rows = self._query(db_role, _SELECT_ACTIVE_STRATEGY, [db_role])
        except Exception as exc:  # noqa: BLE001
            return self._failed(
                run_id, f"{type(exc).__name__}: {exc}", {"db_role": db_role}
            )

        configs_by_strategy: dict[str, dict[str, Any]] = {}
        configs_by_id: dict[str, dict[str, Any]] = {}
        config_ids_by_strategy: dict[str, str] = {}
        config_ids: list[str] = []
        for config_id, strategy_name, _version, config_json, _hash in rows:
            parsed = _loads(config_json)
            configs_by_strategy[strategy_name] = parsed
            configs_by_id[config_id] = parsed
            config_ids_by_strategy[strategy_name] = config_id
            config_ids.append(config_id)

        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=len(configs_by_id),
            metadata={
                "db_role": db_role,
                # Keyed by strategy_name (debug/user selection).
                "configs": configs_by_strategy,
                "configs_by_strategy": configs_by_strategy,
                # Keyed by real DB config_id (used as strategy_config_id in
                # output tables).
                "configs_by_id": configs_by_id,
                "config_ids_by_strategy": config_ids_by_strategy,
                "config_ids": config_ids,
            },
        )

    def list_strategy_configs(
        self, db_role: str, strategy_name: str | None = None
    ) -> ServiceResult:
        """List strategy config versions (optionally for one strategy)."""
        run_id = str(uuid.uuid4())
        try:
            cv.validate_db_role(db_role)
            if strategy_name is not None:
                cv.validate_strategy_name(strategy_name)
        except cv.ConfigValidationError as exc:
            return self._failed(run_id, str(exc), {"db_role": db_role})

        sql = _LIST_STRATEGY if strategy_name is None else _LIST_STRATEGY_BY_NAME
        params = [db_role] if strategy_name is None else [db_role, strategy_name]
        try:
            rows = self._query(db_role, sql, params)
        except Exception as exc:  # noqa: BLE001
            return self._failed(
                run_id, f"{type(exc).__name__}: {exc}", {"db_role": db_role}
            )
        versions = [
            {
                "config_id": r[0],
                "strategy_name": r[1],
                "version": r[2],
                "parent_config_id": r[3],
                "config_hash": r[4],
                "active_flag": bool(r[5]),
                "created_at": str(r[6]),
                "created_by": r[7],
                "notes": r[8],
            }
            for r in rows
        ]
        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=len(versions),
            metadata={"db_role": db_role, "versions": versions},
        )

    def get_strategy_config(self, config_id: str, db_role: str) -> ServiceResult:
        """Return a single strategy config by id (with parsed config_json)."""
        run_id = str(uuid.uuid4())
        try:
            cv.validate_db_role(db_role)
        except cv.ConfigValidationError as exc:
            return self._failed(run_id, str(exc), {"db_role": db_role})
        try:
            rows = self._query(db_role, _GET_STRATEGY, [config_id, db_role])
        except Exception as exc:  # noqa: BLE001
            return self._failed(
                run_id, f"{type(exc).__name__}: {exc}", {"db_role": db_role}
            )
        if not rows:
            return self._failed(
                run_id,
                f"strategy config {config_id!r} not found for db_role {db_role!r}",
                {"db_role": db_role, "config_id": config_id},
            )
        r = rows[0]
        config = {
            "config_id": r[0],
            "db_role": r[1],
            "strategy_name": r[2],
            "version": r[3],
            "parent_config_id": r[4],
            "config_json": _loads(r[5]),
            "config_hash": r[6],
            "active_flag": bool(r[7]),
            "created_at": str(r[8]),
            "created_by": r[9],
            "notes": r[10],
        }
        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=1,
            metadata={"db_role": db_role, "config": config},
        )

    # ------------------------------------------------------------------ #
    # Strategy writes.
    # ------------------------------------------------------------------ #
    def create_strategy_config_version(
        self,
        db_role: str,
        strategy_name: str,
        config_json: dict[str, Any],
        version: str | None = None,
        parent_config_id: str | None = None,
        created_by: str | None = None,
        notes: str | None = None,
        activate: bool = False,
    ) -> ServiceResult:
        """Create a new (inactive by default) strategy config version."""
        run_id = str(uuid.uuid4())
        try:
            cv.validate_db_role(db_role)
            cv.validate_strategy_name(strategy_name)
            cfg = cv.validate_config_payload(config_json)
        except cv.ConfigValidationError as exc:
            return self._failed(run_id, str(exc), {"db_role": db_role})

        config_id = str(uuid.uuid4())
        config_hash = cv.deterministic_hash(cfg)
        version = version or f"{strategy_name}_{config_id[:8]}"
        connection = self._db.connect(db_role)
        try:
            connection.execute("BEGIN TRANSACTION")
            try:
                connection.execute(
                    _INSERT_STRATEGY,
                    [
                        config_id,
                        db_role,
                        strategy_name,
                        version,
                        parent_config_id,
                        cv.canonical_json(cfg),
                        config_hash,
                        bool(activate),
                        created_by,
                        notes,
                    ],
                ).fetchall()
                if activate:
                    connection.execute(
                        _DEACTIVATE_STRATEGY_SIBLINGS, [db_role, strategy_name]
                    )
                    connection.execute(_ACTIVATE_STRATEGY, [config_id, db_role])
                    connection.execute(
                        _INSERT_ACTIVATION_LOG,
                        [
                            str(uuid.uuid4()),
                            config_id,
                            db_role,
                            "strategy",          # config_type is always "strategy"
                            strategy_name,       # profile_name carries the name
                            created_by,
                            notes or "created+activated",
                        ],
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        except Exception as exc:  # noqa: BLE001
            return self._failed(
                run_id, f"{type(exc).__name__}: {exc}", {"db_role": db_role}
            )
        finally:
            connection.close()

        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=1,
            metadata={
                "db_role": db_role,
                "config_id": config_id,
                "config_hash": config_hash,
                "strategy_name": strategy_name,
                "active_flag": bool(activate),
            },
        )

    def activate_strategy_config(
        self,
        config_id: str,
        db_role: str,
        activated_by: str | None = None,
        reason: str | None = None,
    ) -> ServiceResult:
        """Activate a strategy config version; deactivate its siblings."""
        run_id = str(uuid.uuid4())
        try:
            cv.validate_db_role(db_role)
        except cv.ConfigValidationError as exc:
            return self._failed(run_id, str(exc), {"db_role": db_role})

        try:
            name_rows = self._query(
                db_role, _GET_STRATEGY_NAME, [config_id, db_role]
            )
        except Exception as exc:  # noqa: BLE001
            return self._failed(
                run_id, f"{type(exc).__name__}: {exc}", {"db_role": db_role}
            )
        if not name_rows:
            return self._failed(
                run_id,
                f"strategy config {config_id!r} not found for db_role {db_role!r}",
                {"db_role": db_role, "config_id": config_id},
            )
        strategy_name = name_rows[0][0]

        connection = self._db.connect(db_role)
        try:
            connection.execute("BEGIN TRANSACTION")
            try:
                connection.execute(
                    _DEACTIVATE_STRATEGY_SIBLINGS, [db_role, strategy_name]
                )
                connection.execute(_ACTIVATE_STRATEGY, [config_id, db_role])
                connection.execute(
                    _INSERT_ACTIVATION_LOG,
                    [
                        str(uuid.uuid4()),
                        config_id,
                        db_role,
                        "strategy",      # config_type = "strategy" (not "strategy:name")
                        strategy_name,   # profile_name = the strategy name
                        activated_by,
                        reason,
                    ],
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        except Exception as exc:  # noqa: BLE001
            return self._failed(
                run_id, f"{type(exc).__name__}: {exc}", {"db_role": db_role}
            )
        finally:
            connection.close()

        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=1,
            metadata={
                "db_role": db_role,
                "config_id": config_id,
                "strategy_name": strategy_name,
            },
        )

    # ------------------------------------------------------------------ #
    # Runtime reads.
    # ------------------------------------------------------------------ #
    def get_active_runtime_config(
        self, db_role: str, config_type: str
    ) -> ServiceResult:
        """Return the active runtime config payload for ``config_type``."""
        run_id = str(uuid.uuid4())
        try:
            cv.validate_db_role(db_role)
            cv.validate_config_type(config_type)
        except cv.ConfigValidationError as exc:
            return self._failed(run_id, str(exc), {"db_role": db_role})
        try:
            rows = self._query(
                db_role, _SELECT_ACTIVE_RUNTIME, [db_role, config_type]
            )
        except Exception as exc:  # noqa: BLE001
            return self._failed(
                run_id, f"{type(exc).__name__}: {exc}", {"db_role": db_role}
            )
        if not rows:
            return self._failed(
                run_id,
                f"no active runtime config for {config_type!r} (db_role {db_role!r})",
                {"db_role": db_role, "config_type": config_type},
            )
        config_id, _ctype, _version, config_json, config_hash = rows[0]
        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=1,
            metadata={
                "db_role": db_role,
                "config_type": config_type,
                "config_id": config_id,
                "config_json": _loads(config_json),
                "config_hash": config_hash,
            },
        )

    def list_runtime_configs(
        self, db_role: str, config_type: str | None = None
    ) -> ServiceResult:
        """List runtime config versions (optionally for one type)."""
        run_id = str(uuid.uuid4())
        try:
            cv.validate_db_role(db_role)
            if config_type is not None:
                cv.validate_config_type(config_type)
        except cv.ConfigValidationError as exc:
            return self._failed(run_id, str(exc), {"db_role": db_role})
        sql = _LIST_RUNTIME if config_type is None else _LIST_RUNTIME_BY_TYPE
        params = [db_role] if config_type is None else [db_role, config_type]
        try:
            rows = self._query(db_role, sql, params)
        except Exception as exc:  # noqa: BLE001
            return self._failed(
                run_id, f"{type(exc).__name__}: {exc}", {"db_role": db_role}
            )
        versions = [
            {
                "config_id": r[0],
                "config_type": r[1],
                "version": r[2],
                "parent_config_id": r[3],
                "config_hash": r[4],
                "active_flag": bool(r[5]),
                "created_at": str(r[6]),
                "created_by": r[7],
                "notes": r[8],
            }
            for r in rows
        ]
        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=len(versions),
            metadata={"db_role": db_role, "versions": versions},
        )

    # ------------------------------------------------------------------ #
    # Runtime writes.
    # ------------------------------------------------------------------ #
    def create_runtime_config_version(
        self,
        db_role: str,
        config_type: str,
        config_json: dict[str, Any],
        version: str | None = None,
        parent_config_id: str | None = None,
        created_by: str | None = None,
        notes: str | None = None,
        activate: bool = False,
    ) -> ServiceResult:
        """Create a new (inactive by default) runtime config version."""
        run_id = str(uuid.uuid4())
        try:
            cv.validate_db_role(db_role)
            cv.validate_config_type(config_type)
            cfg = cv.validate_config_payload(config_json)
        except cv.ConfigValidationError as exc:
            return self._failed(run_id, str(exc), {"db_role": db_role})

        config_id = str(uuid.uuid4())
        config_hash = cv.deterministic_hash(cfg)
        version = version or f"{config_type}_{config_id[:8]}"
        connection = self._db.connect(db_role)
        try:
            connection.execute("BEGIN TRANSACTION")
            try:
                connection.execute(
                    _INSERT_RUNTIME,
                    [
                        config_id,
                        db_role,
                        config_type,
                        version,
                        parent_config_id,
                        cv.canonical_json(cfg),
                        config_hash,
                        bool(activate),
                        created_by,
                        notes,
                    ],
                ).fetchall()
                if activate:
                    connection.execute(
                        _DEACTIVATE_RUNTIME_SIBLINGS, [db_role, config_type]
                    )
                    connection.execute(_ACTIVATE_RUNTIME, [config_id, db_role])
                    connection.execute(
                        _INSERT_ACTIVATION_LOG,
                        [
                            str(uuid.uuid4()),
                            config_id,
                            db_role,
                            config_type,
                            None,
                            created_by,
                            notes or "created+activated",
                        ],
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        except Exception as exc:  # noqa: BLE001
            return self._failed(
                run_id, f"{type(exc).__name__}: {exc}", {"db_role": db_role}
            )
        finally:
            connection.close()

        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=1,
            metadata={
                "db_role": db_role,
                "config_id": config_id,
                "config_hash": config_hash,
                "config_type": config_type,
                "active_flag": bool(activate),
            },
        )

    def activate_runtime_config(
        self,
        config_id: str,
        db_role: str,
        activated_by: str | None = None,
        reason: str | None = None,
    ) -> ServiceResult:
        """Activate a runtime config version; deactivate its siblings."""
        run_id = str(uuid.uuid4())
        try:
            cv.validate_db_role(db_role)
        except cv.ConfigValidationError as exc:
            return self._failed(run_id, str(exc), {"db_role": db_role})
        try:
            type_rows = self._query(
                db_role, _GET_RUNTIME_TYPE, [config_id, db_role]
            )
        except Exception as exc:  # noqa: BLE001
            return self._failed(
                run_id, f"{type(exc).__name__}: {exc}", {"db_role": db_role}
            )
        if not type_rows:
            return self._failed(
                run_id,
                f"runtime config {config_id!r} not found for db_role {db_role!r}",
                {"db_role": db_role, "config_id": config_id},
            )
        config_type = type_rows[0][0]

        connection = self._db.connect(db_role)
        try:
            connection.execute("BEGIN TRANSACTION")
            try:
                connection.execute(
                    _DEACTIVATE_RUNTIME_SIBLINGS, [db_role, config_type]
                )
                connection.execute(_ACTIVATE_RUNTIME, [config_id, db_role])
                connection.execute(
                    _INSERT_ACTIVATION_LOG,
                    [
                        str(uuid.uuid4()),
                        config_id,
                        db_role,
                        config_type,
                        None,
                        activated_by,
                        reason,
                    ],
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        except Exception as exc:  # noqa: BLE001
            return self._failed(
                run_id, f"{type(exc).__name__}: {exc}", {"db_role": db_role}
            )
        finally:
            connection.close()

        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=1,
            metadata={
                "db_role": db_role,
                "config_id": config_id,
                "config_type": config_type,
            },
        )

    # ------------------------------------------------------------------ #
    # Internal helpers.
    # ------------------------------------------------------------------ #
    @staticmethod
    def _failed(
        run_id: str, message: str, metadata: dict[str, Any]
    ) -> ServiceResult:
        return ServiceResult(
            status=service_result.STATUS_FAILED,
            run_id=run_id,
            rows_processed=0,
            errors=[message],
            metadata=metadata,
        )
