"""Module 18 — Export Package Engine.

Builds reviewer-facing export ZIP packages and records a single manual review
row, for two flows:

``ExportPackageEngine.export_ticker_review``
    For one ``signal_date`` / ``setup_config_id`` and an explicit list of
    ``proposal_ids`` (``prod`` / ``debug`` roles only), assembles a ticker-review
    ZIP (``prices.csv``, ``features.csv``, ``step3.csv``, ``step4.csv``,
    ``step5.csv``, ``explanation.txt``, ``metadata.json``) under
    ``settings.EXPORTS_DIR`` and writes exactly one ``ai_reviews`` row with
    ``review_type='ticker_review'`` and the structured V1 ``prompt_text``.

``ExportPackageEngine.export_simulation_review``
    For one ``sim_run_id`` (``simulation`` role only), assembles a
    simulation-review ZIP (``configs.json``, ``performance_metrics.csv``,
    ``score_buckets.csv``, ``setup_performance.csv``, ``regime_performance.csv``,
    ``drawdowns.csv``, ``unresolved_outcomes.csv``) and writes exactly one
    ``sim_ai_reviews`` row.

Contract source of truth: ``M18_EXPORT_PACKAGE_ENGINE_SPEC.md`` (derived from
``01e_UI_AND_TESTING.md`` / ``UI/96_Export_Package_Specs.md`` for the ZIP file
manifests, ``01b_SCHEMA_AND_DATA.md`` / ``M02_SCHEMA_SPEC.md`` §3.19 / §4.9 for
the ``ai_reviews`` / ``sim_ai_reviews`` / source-table schemas,
``01a_CORE_PRINCIPLES.md`` for the ``review_type`` / ``list_membership`` enums,
``app/config/settings.py`` for ``EXPORTS_DIR``, the frozen Module 17 simulation
tables, and the Module 16 ``ServiceResult`` / db_role-guard / read→build→single
-write discipline).

Hard boundaries (Module 18): no direct ``duckdb`` import, no provider imports or
calls, no ``print()``, no DDL, and no ``ATTACH``. Every database access is routed
through the approved :mod:`app.database.duckdb_manager` (or an injected
``db_manager``). ZIPs are written only under ``settings.EXPORTS_DIR``. The only
mutated tables are ``ai_reviews`` (ticker, prod/debug) and ``sim_ai_reviews``
(simulation).
"""

from __future__ import annotations

import csv
import io
import json
import uuid
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Final, Protocol

from app.config import settings
from app.database import duckdb_manager
from app.utils import logging_config
from app.utils import service_result
from app.utils.service_result import ServiceResult

# --------------------------------------------------------------------------- #
# Roles / constants.
# --------------------------------------------------------------------------- #
DB_ROLE_PROD: Final[str] = duckdb_manager.DB_ROLE_PROD
DB_ROLE_DEBUG: Final[str] = duckdb_manager.DB_ROLE_DEBUG
DB_ROLE_SIMULATION: Final[str] = duckdb_manager.DB_ROLE_SIMULATION

TICKER_ALLOWED_ROLES: Final[tuple[str, ...]] = (DB_ROLE_PROD, DB_ROLE_DEBUG)
SIM_ALLOWED_ROLES: Final[tuple[str, ...]] = (DB_ROLE_SIMULATION,)

EXPORT_TYPE_TICKER: Final[str] = "ticker_review"
EXPORT_TYPE_SIM: Final[str] = "simulation_review"

REVIEW_TABLE_TICKER: Final[str] = "ai_reviews"
REVIEW_TABLE_SIM: Final[str] = "sim_ai_reviews"

PROVIDER_MANUAL: Final[str] = "manual"
MODEL_NONE: Final[str] = "none"
PROMPT_VERSION_V1: Final[str] = "v1"

STATUS_SUCCESS_META: Final[str] = "success"
STATUS_FAILED_META: Final[str] = "failed"

# Prices window: ±5 trading rows around the signal_date anchor (G-PRICES-WINDOW).
PRICES_WINDOW_TRADING_DAYS: Final[int] = 5

# Outcome return horizons unpivoted from the *_bd_pct outcome columns.
RETURN_HORIZON_COLUMNS: Final[tuple[tuple[int, str], ...]] = (
    (5, "return_5bd_pct"),
    (10, "return_10bd_pct"),
    (20, "return_20bd_pct"),
    (40, "return_40bd_pct"),
)

# Diversified-list outcome membership (01a list_membership enum).
DIVERSIFIED_MEMBERSHIPS: Final[tuple[str, ...]] = ("diversified_only", "both")

# Score-bucket geometry: 0–100, width 10.
SCORE_BUCKET_WIDTH: Final[int] = 10
SCORE_BUCKET_MAX: Final[int] = 100

# Exact metadata key sets (returned on every path).
TICKER_METADATA_KEYS: Final[tuple[str, ...]] = (
    "run_id",
    "export_type",
    "db_role",
    "signal_date",
    "setup_config_id",
    "proposal_ids",
    "zip_filename",
    "zip_path",
    "review_type",
    "review_table",
    "status",
    "error",
)
SIM_METADATA_KEYS: Final[tuple[str, ...]] = (
    "run_id",
    "export_type",
    "db_role",
    "sim_run_id",
    "zip_filename",
    "zip_path",
    "review_type",
    "review_table",
    "status",
    "error",
)

_LOG = logging_config.get_logger(__name__)


class _DbManagerLike(Protocol):
    """Minimal hook the engine needs from the DB manager (for injection)."""

    def connect(self, db_role: str, read_only: bool = ...) -> Any: ...


class _ValidationError(ValueError):
    """Raised internally for pre-DB validation failures."""


# --------------------------------------------------------------------------- #
# Small pure helpers.
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _placeholders(n: int) -> str:
    """Return ``?, ?, ...`` of length ``n`` for a parameterized ``IN`` clause."""
    return ", ".join(["?"] * n)


def _rows_to_csv_bytes(header: list[str], rows: list[list[Any]]) -> bytes:
    """Render ``header`` + ``rows`` to CSV bytes (header-only when no rows)."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for row in rows:
        writer.writerow(["" if v is None else v for v in row])
    return buf.getvalue().encode("utf-8")


def _mean(values: list[float]) -> float | None:
    clean = [v for v in values if v is not None]
    if not clean:
        return None
    return sum(clean) / len(clean)


def _score_bucket(score: float) -> tuple[int, int] | None:
    """Return the ``[low, high)`` 10-wide bucket for ``score`` in 0–100."""
    if score is None:
        return None
    if score < 0 or score > SCORE_BUCKET_MAX:
        return None
    low = int(score // SCORE_BUCKET_WIDTH) * SCORE_BUCKET_WIDTH
    if low >= SCORE_BUCKET_MAX:  # exactly 100 lands in the top bucket.
        low = SCORE_BUCKET_MAX - SCORE_BUCKET_WIDTH
    return (low, low + SCORE_BUCKET_WIDTH)


# --------------------------------------------------------------------------- #
# Export package engine.
# --------------------------------------------------------------------------- #
class ExportPackageEngine:
    """Build reviewer export ZIPs and record one manual review row.

    The optional ``db_manager`` argument exists only for test injection; when
    ``None`` the approved :mod:`app.database.duckdb_manager` is used.
    """

    def __init__(self, db_manager: _DbManagerLike | None = None) -> None:
        self._db: _DbManagerLike = (
            db_manager if db_manager is not None else duckdb_manager
        )

    # ------------------------------------------------------------------ #
    # Ticker review.
    # ------------------------------------------------------------------ #
    def export_ticker_review(
        self,
        signal_date: date,
        setup_config_id: str,
        proposal_ids: list[str],
        db_role: str = "prod",
        run_id: str | None = None,
    ) -> ServiceResult:
        """Build a ticker-review ZIP and write one ``ai_reviews`` row.

        Parameters
        ----------
        signal_date:
            Signal date scoping ``step3`` / ``step4`` / ``step5`` reads.
        setup_config_id:
            Strategy config id scoping the same reads.
        proposal_ids:
            Explicit Step 5 proposal ids to export. Must be non-empty.
        db_role:
            ``"prod"`` or ``"debug"`` only; anything else fails before DB access.
        run_id:
            A fresh ``uuid4`` is minted when ``None``; a supplied value is kept.
        """
        # --- normalize proposal_ids first so all failure paths are safe. -- #
        # Guards against None or any non-list type being passed.
        if proposal_ids is None:
            proposal_ids = []
        else:
            try:
                proposal_ids = list(proposal_ids)
            except TypeError:
                proposal_ids = []

        run_id = run_id if run_id is not None else str(uuid.uuid4())
        log = logging_config.get_logger(__name__, run_id)
        signal_iso = signal_date.isoformat()

        # --- pre-DB validation (no I/O). ---------------------------------- #
        try:
            self._validate_ticker_inputs(
                db_role, proposal_ids, setup_config_id
            )
        except _ValidationError as exc:
            log.error("ticker export validation failed: %s", exc)
            return self._ticker_failed(
                run_id, db_role, signal_iso, setup_config_id,
                proposal_ids, str(exc),
            )

        # --- read phase 1: fetch step5 with all 3 filters (read-only). ---- #
        # This is deliberately separate so we can validate the returned set
        # before reading step3/step4/features/prices.
        try:
            step5 = self._fetch_step5_proposals(
                db_role, signal_date, setup_config_id, proposal_ids
            )
        except Exception as exc:  # noqa: BLE001 - surface DB read failure
            message = f"read failed: {type(exc).__name__}: {exc}"
            log.error("ticker export %s", message)
            return self._ticker_failed(
                run_id, db_role, signal_iso, setup_config_id,
                proposal_ids, message,
            )

        # --- validate fetched set exactly matches requested set. ---------- #
        fetched_ids = {r["proposal_id"] for r in step5}
        requested_ids = set(proposal_ids)
        if fetched_ids != requested_ids:
            missing = sorted(requested_ids - fetched_ids)
            unexpected = sorted(fetched_ids - requested_ids)
            message = (
                f"proposal_id mismatch (wrong signal_date, setup_config_id, "
                f"or non-existent): missing={missing}, unexpected={unexpected}"
            )
            log.error("ticker export %s", message)
            return self._ticker_failed(
                run_id, db_role, signal_iso, setup_config_id,
                proposal_ids, message,
            )

        # step5 is guaranteed non-empty here: proposal_ids was validated non-empty
        # and the full set matches, so len(step5) == len(proposal_ids) >= 1.

        # Reorder fetched rows to match the caller's requested order so that
        # first_exported_pid, step5.csv, explanation.txt, and
        # selected_tickers_json all follow the requested (not lexicographic) order.
        _by_id = {row["proposal_id"]: row for row in step5}
        step5 = [_by_id[pid] for pid in proposal_ids]

        # --- read phase 2: remaining data for validated tickers. ---------- #
        tickers = [r["ticker"] for r in step5]
        try:
            remaining = self._read_ticker_remaining(
                db_role, signal_date, setup_config_id, tickers
            )
        except Exception as exc:  # noqa: BLE001
            message = f"read failed: {type(exc).__name__}: {exc}"
            log.error("ticker export %s", message)
            return self._ticker_failed(
                run_id, db_role, signal_iso, setup_config_id,
                proposal_ids, message,
            )

        data = {"step5": step5, **remaining}

        # --- build + write ZIP. ------------------------------------------- #
        zip_filename = f"ticker_review_{signal_iso}_{run_id[:8]}.zip"
        try:
            zip_path = self._build_ticker_zip(
                zip_filename, signal_date, setup_config_id,
                proposal_ids, run_id, data,
            )
        except Exception as exc:  # noqa: BLE001 - surface ZIP failure
            message = f"zip build failed: {type(exc).__name__}: {exc}"
            log.error("ticker export %s", message)
            return self._ticker_failed(
                run_id, db_role, signal_iso, setup_config_id,
                proposal_ids, message,
            )

        # --- write the single ai_reviews row (ZIP may remain on failure). - #
        # Use the first *exported* proposal_id (from validated step5 rows,
        # ordered by proposal_id), not proposal_ids[0] from the raw input.
        first_exported_pid = step5[0]["proposal_id"]
        exported_tickers = [r["ticker"] for r in step5]
        prompt_text = self._ticker_prompt_text_v1(
            signal_iso, setup_config_id, data
        )
        try:
            self._write_ticker_review_row(
                db_role, run_id, first_exported_pid, prompt_text, exported_tickers
            )
        except Exception as exc:  # noqa: BLE001 - DB write after ZIP (G-ZIP-CLEANUP)
            message = (
                f"review write failed: {type(exc).__name__}: {exc} "
                f"(zip retained at {zip_path})"
            )
            log.error("ticker export %s", message)
            return self._ticker_failed(
                run_id, db_role, signal_iso, setup_config_id,
                proposal_ids, message,
                zip_filename=zip_filename, zip_path=str(zip_path),
            )

        log.info("ticker export ok run_id=%s zip=%s", run_id, zip_filename)
        return self._ticker_success(
            run_id, db_role, signal_iso, setup_config_id, proposal_ids,
            zip_filename, str(zip_path), rows_processed=len(data["step5"]),
        )

    # ------------------------------------------------------------------ #
    # Simulation review.
    # ------------------------------------------------------------------ #
    def export_simulation_review(
        self,
        sim_run_id: str,
        db_role: str = "simulation",
        run_id: str | None = None,
    ) -> ServiceResult:
        """Build a simulation-review ZIP and write one ``sim_ai_reviews`` row.

        Parameters
        ----------
        sim_run_id:
            Simulation run to export. Must be non-empty.
        db_role:
            ``"simulation"`` only; anything else fails before DB access.
        run_id:
            A fresh ``uuid4`` is minted when ``None``; a supplied value is kept.
        """
        run_id = run_id if run_id is not None else str(uuid.uuid4())
        log = logging_config.get_logger(__name__, run_id)

        # --- pre-DB validation. ------------------------------------------- #
        try:
            self._validate_sim_inputs(db_role, sim_run_id)
        except _ValidationError as exc:
            log.error("simulation export validation failed: %s", exc)
            return self._sim_failed(run_id, db_role, sim_run_id, str(exc))

        # --- read phase (read-only). -------------------------------------- #
        try:
            data = self._read_sim_data(db_role, sim_run_id)
        except Exception as exc:  # noqa: BLE001 - surface DB read failure
            message = f"read failed: {type(exc).__name__}: {exc}"
            log.error("simulation export %s", message)
            return self._sim_failed(run_id, db_role, sim_run_id, message)

        zip_filename = f"simulation_review_{sim_run_id[:8]}_{run_id[:8]}.zip"
        try:
            zip_path = self._build_sim_zip(zip_filename, sim_run_id, run_id, data)
        except Exception as exc:  # noqa: BLE001 - surface ZIP failure
            message = f"zip build failed: {type(exc).__name__}: {exc}"
            log.error("simulation export %s", message)
            return self._sim_failed(run_id, db_role, sim_run_id, message)

        prompt_text = self._sim_prompt_text_v1(sim_run_id, data)
        try:
            self._write_sim_review_row(db_role, run_id, sim_run_id, prompt_text)
        except Exception as exc:  # noqa: BLE001 - DB write after ZIP (G-ZIP-CLEANUP)
            message = (
                f"review write failed: {type(exc).__name__}: {exc} "
                f"(zip retained at {zip_path})"
            )
            log.error("simulation export %s", message)
            return self._sim_failed(
                run_id, db_role, sim_run_id, message,
                zip_filename=zip_filename, zip_path=str(zip_path),
            )

        log.info("simulation export ok run_id=%s zip=%s", run_id, zip_filename)
        return self._sim_success(
            run_id, db_role, sim_run_id, zip_filename, str(zip_path),
            rows_processed=len(data["performance_metrics"]),
        )

    # ------------------------------------------------------------------ #
    # Validation (no I/O).
    # ------------------------------------------------------------------ #
    @staticmethod
    def _validate_ticker_inputs(
        db_role: str, proposal_ids: list[str], setup_config_id: str
    ) -> None:
        if db_role not in TICKER_ALLOWED_ROLES:
            raise _ValidationError(
                f"Unsupported db_role {db_role!r}; ticker review targets "
                f"{list(TICKER_ALLOWED_ROLES)}."
            )
        if not setup_config_id:
            raise _ValidationError("setup_config_id must be non-empty.")
        if not proposal_ids:
            raise _ValidationError("proposal_ids must be non-empty.")
        if len(set(proposal_ids)) != len(proposal_ids):
            dupes = sorted(
                pid for pid in set(proposal_ids) if proposal_ids.count(pid) > 1
            )
            raise _ValidationError(
                f"proposal_ids must not contain duplicates: {dupes}"
            )

    @staticmethod
    def _validate_sim_inputs(db_role: str, sim_run_id: str) -> None:
        if db_role not in SIM_ALLOWED_ROLES:
            raise _ValidationError(
                f"Unsupported db_role {db_role!r}; simulation review targets "
                f"{list(SIM_ALLOWED_ROLES)}."
            )
        if not sim_run_id:
            raise _ValidationError("sim_run_id must be non-empty.")

    # ------------------------------------------------------------------ #
    # Ticker read phase (two steps).
    # ------------------------------------------------------------------ #
    def _fetch_step5_proposals(
        self,
        db_role: str,
        signal_date: date,
        setup_config_id: str,
        proposal_ids: list[str],
    ) -> list[dict[str, Any]]:
        """Read step5 rows with all 3 exact filters; closed read-only connection."""
        connection = self._db.connect(db_role)
        try:
            return self._fetch_dicts(
                connection,
                "SELECT proposal_id, ticker, signal_date, "
                "setup_type, setup_score, risk_label, disposition, "
                "entry_price_raw, stop_price_raw, target_price_raw, estimated_rr, "
                "proposal_score_raw, proposal_score_final, raw_rank, diversified_rank, "
                "in_raw_top_n, in_diversified_top_n, "
                "mechanical_explanation "
                "FROM step5_proposals "
                f"WHERE proposal_id IN ({_placeholders(len(proposal_ids))}) "
                "AND signal_date = ? "
                "AND setup_config_id = ? "
                "ORDER BY proposal_id",
                [*proposal_ids, signal_date, setup_config_id],
            )
        finally:
            connection.close()

    def _read_ticker_remaining(
        self,
        db_role: str,
        signal_date: date,
        setup_config_id: str,
        tickers: list[str],
    ) -> dict[str, Any]:
        """Read step3/step4/features/prices for a validated, non-empty ticker list.

        ``tickers`` is guaranteed non-empty by the time this is called; the
        ticker-IN filter is always emitted, preventing leakage to unrelated rows.
        """
        connection = self._db.connect(db_role)
        try:
            step3 = self._fetch_dicts(
                connection,
                "SELECT candidate_id, ticker, signal_date, eligibility_score, "
                "passed_eligibility, routing_status, routed_setup_types "
                "FROM step3_candidates "
                "WHERE signal_date = ? "
                f"AND ticker IN ({_placeholders(len(tickers))}) "
                "ORDER BY ticker",
                [signal_date, *tickers],
            )
            step4 = self._fetch_dicts(
                connection,
                "SELECT analysis_id, ticker, signal_date, setup_type, "
                "setup_score, estimated_rr, stop_price_raw, target_price_raw "
                "FROM step4_analysis "
                "WHERE signal_date = ? AND setup_config_id = ? "
                f"AND ticker IN ({_placeholders(len(tickers))}) "
                "ORDER BY ticker",
                [signal_date, setup_config_id, *tickers],
            )
            features = self._read_features(connection, signal_date, tickers)
            prices = self._read_prices_window(connection, signal_date, tickers)
        finally:
            connection.close()

        return {
            "step3": step3,
            "step4": step4,
            "features": features,
            "prices": prices,
        }

    def _read_features(
        self, connection: Any, signal_date: date, tickers: list[str]
    ) -> dict[str, list[str] | list[list[Any]]]:
        """Read ``daily_features_current`` for the proposal tickers on date.

        Column order is discovered dynamically so the view's wide schema is
        exported verbatim without hard-coding every feature column here.
        """
        if not tickers:
            return {"header": [], "rows": []}
        cursor = connection.execute(
            "SELECT * FROM daily_features_current "
            f"WHERE feature_date = ? AND ticker IN ({_placeholders(len(tickers))}) "
            "ORDER BY ticker",
            [signal_date, *tickers],
        )
        header = [d[0] for d in cursor.description]
        rows = [list(r) for r in cursor.fetchall()]
        return {"header": header, "rows": rows}

    def _read_prices_window(
        self, connection: Any, signal_date: date, tickers: list[str]
    ) -> dict[str, list[str] | list[list[Any]]]:
        """Read daily_prices within ±5 *trading rows* of signal_date per ticker.

        G-PRICES-WINDOW: the ±5 trading-day window is approximated using the
        price rows that actually exist for each ticker (no market-calendar
        dependency). Per ticker: up to 5 rows on/before signal_date plus up to 5
        rows after it.
        """
        if not tickers:
            return {"header": [], "rows": []}
        cursor = connection.execute(
            "SELECT ticker, date, open_raw, high_raw, low_raw, close_raw, "
            "volume_raw, close_adj "
            "FROM daily_prices "
            f"WHERE ticker IN ({_placeholders(len(tickers))}) "
            "ORDER BY ticker, date",
            list(tickers),
        )
        header = [d[0] for d in cursor.description]
        all_rows = [list(r) for r in cursor.fetchall()]

        by_ticker: dict[str, list[list[Any]]] = {}
        for row in all_rows:
            by_ticker.setdefault(row[0], []).append(row)

        windowed: list[list[Any]] = []
        for ticker in sorted(by_ticker):
            rows = by_ticker[ticker]  # already date-ordered by the query
            before = [r for r in rows if r[1] <= signal_date]
            after = [r for r in rows if r[1] > signal_date]
            kept = (
                before[-(PRICES_WINDOW_TRADING_DAYS + 1):]
                + after[:PRICES_WINDOW_TRADING_DAYS]
            )
            windowed.extend(kept)
        return {"header": header, "rows": windowed}

    # ------------------------------------------------------------------ #
    # Ticker ZIP build.
    # ------------------------------------------------------------------ #
    def _build_ticker_zip(
        self,
        zip_filename: str,
        signal_date: date,
        setup_config_id: str,
        proposal_ids: list[str],
        run_id: str,
        data: dict[str, Any],
    ) -> Path:
        exports_dir = Path(settings.EXPORTS_DIR)
        exports_dir.mkdir(parents=True, exist_ok=True)
        zip_path = exports_dir / zip_filename

        metadata = {
            "run_id": run_id,
            "signal_date": signal_date.isoformat(),
            "setup_config_id": setup_config_id,
            "proposal_ids": list(proposal_ids),
            "export_timestamp": _now_iso(),
        }

        step3_rows = [
            [r["candidate_id"], r["ticker"], r["signal_date"],
             r.get("eligibility_score"), r.get("passed_eligibility"),
             r.get("routing_status"), r.get("routed_setup_types")]
            for r in data["step3"]
        ]
        step4_rows = [
            [r["analysis_id"], r["ticker"], r["signal_date"], r["setup_type"],
             r["setup_score"], r["estimated_rr"], r["stop_price_raw"],
             r["target_price_raw"]]
            for r in data["step4"]
        ]
        step5_rows = [
            [r["proposal_id"], r["ticker"], r["signal_date"],
             r.get("setup_type"), r.get("setup_score"), r.get("risk_label"),
             r.get("disposition"),
             r.get("entry_price_raw"), r.get("stop_price_raw"),
             r.get("target_price_raw"), r.get("estimated_rr"),
             r["proposal_score_raw"], r["proposal_score_final"],
             r["raw_rank"], r["diversified_rank"],
             r["in_raw_top_n"], r["in_diversified_top_n"]]
            for r in data["step5"]
        ]

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("metadata.json", json.dumps(metadata, indent=2))
            zf.writestr(
                "prices.csv",
                _rows_to_csv_bytes(data["prices"]["header"], data["prices"]["rows"])
                if data["prices"]["header"]
                else _rows_to_csv_bytes(
                    ["ticker", "date", "open_raw", "high_raw", "low_raw",
                     "close_raw", "volume_raw", "close_adj"], [],
                ),
            )
            zf.writestr(
                "features.csv",
                _rows_to_csv_bytes(
                    data["features"]["header"], data["features"]["rows"]
                )
                if data["features"]["header"]
                else _rows_to_csv_bytes(["ticker", "feature_date"], []),
            )
            zf.writestr(
                "step3.csv",
                _rows_to_csv_bytes(
                    ["candidate_id", "ticker", "signal_date", "eligibility_score",
                     "passed_eligibility", "routing_status",
                     "routed_setup_types"], step3_rows,
                ),
            )
            zf.writestr(
                "step4.csv",
                _rows_to_csv_bytes(
                    ["analysis_id", "ticker", "signal_date", "setup_type",
                     "setup_score", "estimated_rr", "stop_price_raw",
                     "target_price_raw"], step4_rows,
                ),
            )
            zf.writestr(
                "step5.csv",
                _rows_to_csv_bytes(
                    ["proposal_id", "ticker", "signal_date",
                     "setup_type", "setup_score", "risk_label", "disposition",
                     "entry_price_raw", "stop_price_raw", "target_price_raw",
                     "estimated_rr", "proposal_score_raw", "proposal_score_final",
                     "raw_rank", "diversified_rank",
                     "in_raw_top_n", "in_diversified_top_n"], step5_rows,
                ),
            )
            zf.writestr("explanation.txt", self._format_explanations(data["step5"]))
        return zip_path

    @staticmethod
    def _format_explanations(step5: list[dict[str, Any]]) -> str:
        """Format ``mechanical_explanation`` (JSON-or-text) per proposal."""
        blocks: list[str] = []
        for row in step5:
            raw = row.get("mechanical_explanation")
            header = f"[{row['ticker']} — proposal {row['proposal_id']}]"
            if raw is None:
                body = "(no mechanical_explanation recorded)"
            else:
                try:
                    parsed = json.loads(raw)
                    body = json.dumps(parsed, indent=2)
                except (TypeError, ValueError):
                    body = str(raw)
            blocks.append(f"{header}\n{body}")
        return "\n\n".join(blocks) + ("\n" if blocks else "")

    # ------------------------------------------------------------------ #
    # Ticker prompt text V1 (G-PROMPT-TEXT).
    # ------------------------------------------------------------------ #
    @staticmethod
    def _ticker_prompt_text_v1(
        signal_iso: str, setup_config_id: str, data: dict[str, Any]
    ) -> str:
        step5 = data["step5"]
        step4 = data["step4"]
        tickers = [r["ticker"] for r in step5]
        scores = [
            r["proposal_score_final"]
            for r in step5
            if r["proposal_score_final"] is not None
        ]
        top_score = max(scores) if scores else None
        setups = sorted(
            {r["setup_type"] for r in step4 if r["setup_type"] is not None}
        )
        rrs = [
            r["estimated_rr"] for r in step4 if r["estimated_rr"] is not None
        ]
        rr_range = (
            f"{min(rrs):.2f}–{max(rrs):.2f}" if rrs else "n/a"
        )
        summary = (
            f"{len(step4)} step4 row(s), {len(step5)} step5 proposal(s) "
            f"for {setup_config_id}."
        )
        return (
            f"[TICKER REVIEW — {signal_iso} — {setup_config_id}]\n"
            f"Proposals: {', '.join(tickers) if tickers else '(none)'}\n"
            f"Top proposal score: {top_score if top_score is not None else 'n/a'}\n"
            f"Setup types: {', '.join(setups) if setups else '(none)'}\n"
            f"Estimated RR range: {rr_range}\n"
            f"{summary}\n\n"
            "Assess: are these proposals worth executing today? "
            "Flag earnings risk and macro risk."
        )

    # ------------------------------------------------------------------ #
    # Ticker review row write (single row).
    # ------------------------------------------------------------------ #
    def _write_ticker_review_row(
        self,
        db_role: str,
        run_id: str,
        proposal_id: str,
        prompt_text: str,
        exported_tickers: list[str],
    ) -> None:
        connection = self._db.connect(db_role)
        try:
            connection.execute(
                "INSERT INTO ai_reviews "
                "(ai_review_id, review_type, proposal_id, sim_run_id, provider, "
                " model, prompt_version, prompt_text, selected_tickers_json, "
                " ai_response_text, human_action, created_at) "
                "VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, NULL, NULL, "
                " CAST(now() AS TIMESTAMP))",
                [
                    str(uuid.uuid4()),
                    EXPORT_TYPE_TICKER,
                    proposal_id,
                    PROVIDER_MANUAL,
                    MODEL_NONE,
                    PROMPT_VERSION_V1,
                    prompt_text,
                    json.dumps(exported_tickers),
                ],
            )
        finally:
            connection.close()

    # ------------------------------------------------------------------ #
    # Simulation read phase.
    # ------------------------------------------------------------------ #
    def _read_sim_data(self, db_role: str, sim_run_id: str) -> dict[str, Any]:
        connection = self._db.connect(db_role)
        try:
            sim_run = self._fetch_dicts(
                connection,
                "SELECT sim_run_id, sim_name, mode, start_date, end_date, "
                "status, config_ids, notes FROM sim_runs WHERE sim_run_id = ?",
                [sim_run_id],
            )
            comparisons = self._fetch_dicts(
                connection,
                "SELECT comparison_id, config_id, horizon_bd, expectancy, "
                "win_rate, avg_win, avg_loss, profit_factor, max_drawdown_pct, "
                "resolved_outcomes_pct, list_type "
                "FROM sim_config_comparisons WHERE sim_run_id = ? "
                "ORDER BY config_id, horizon_bd",
                [sim_run_id],
            )
            step3_scores = self._fetch_dicts(
                connection,
                "SELECT eligibility_score FROM sim_step3_candidates "
                "WHERE sim_run_id = ?",
                [sim_run_id],
            )
            step5_scores = self._fetch_dicts(
                connection,
                "SELECT proposal_score_final FROM sim_step5_proposals "
                "WHERE sim_run_id = ?",
                [sim_run_id],
            )
            outcomes = self._fetch_dicts(
                connection,
                "SELECT proposal_id, ticker, setup_config_id, setup_type, "
                "risk_label, signal_date, "
                "return_5bd_pct, return_10bd_pct, return_20bd_pct, "
                "return_40bd_pct, list_membership, outcome_status "
                "FROM sim_signal_outcomes WHERE sim_run_id = ? "
                "ORDER BY signal_date, ticker",
                [sim_run_id],
            )
            step4 = self._fetch_dicts(
                connection,
                "SELECT ticker, setup_config_id, signal_date, setup_type "
                "FROM sim_step4_analysis WHERE sim_run_id = ?",
                [sim_run_id],
            )
        finally:
            connection.close()

        return {
            "sim_run": sim_run[0] if sim_run else None,
            "performance_metrics": comparisons,
            "step3_scores": step3_scores,
            "step5_scores": step5_scores,
            "outcomes": outcomes,
            "step4": step4,
        }

    # ------------------------------------------------------------------ #
    # Simulation ZIP build.
    # ------------------------------------------------------------------ #
    def _build_sim_zip(
        self,
        zip_filename: str,
        sim_run_id: str,
        run_id: str,
        data: dict[str, Any],
    ) -> Path:
        exports_dir = Path(settings.EXPORTS_DIR)
        exports_dir.mkdir(parents=True, exist_ok=True)
        zip_path = exports_dir / zip_filename

        configs = self._build_configs_json(sim_run_id, run_id, data["sim_run"])
        perf_csv = self._build_performance_csv(data["performance_metrics"])
        buckets_csv = self._build_score_buckets_csv(
            data["step3_scores"], data["step5_scores"]
        )
        setup_csv = self._build_setup_performance_csv(
            data["outcomes"], data["step4"]
        )
        regime_csv = self._build_regime_performance_csv()
        drawdowns_csv = self._build_drawdowns_csv(data["outcomes"])
        unresolved_csv = self._build_unresolved_csv(data["outcomes"])

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("configs.json", json.dumps(configs, indent=2))
            zf.writestr("performance_metrics.csv", perf_csv)
            zf.writestr("score_buckets.csv", buckets_csv)
            zf.writestr("setup_performance.csv", setup_csv)
            zf.writestr("regime_performance.csv", regime_csv)
            zf.writestr("drawdowns.csv", drawdowns_csv)
            zf.writestr("unresolved_outcomes.csv", unresolved_csv)
        return zip_path

    @staticmethod
    def _build_configs_json(
        sim_run_id: str, run_id: str, sim_run: dict[str, Any] | None
    ) -> dict[str, Any]:
        """configs.json: config_ids + sim_run metadata.

        G-CONFIGS-SOURCE: full ``setup_configs`` JSON is not available in the
        simulation database and ``ATTACH`` to prod is out of scope for Module 18,
        so only ``config_ids`` and the ``sim_runs`` metadata are emitted.
        """
        config_ids: list[Any] = []
        run_meta: dict[str, Any] = {}
        if sim_run is not None:
            raw_ids = sim_run.get("config_ids")
            if isinstance(raw_ids, str):
                try:
                    config_ids = json.loads(raw_ids)
                except (TypeError, ValueError):
                    config_ids = []
            elif isinstance(raw_ids, list):
                config_ids = raw_ids
            run_meta = {
                "sim_name": sim_run.get("sim_name"),
                "mode": sim_run.get("mode"),
                "start_date": str(sim_run.get("start_date")),
                "end_date": str(sim_run.get("end_date")),
                "status": sim_run.get("status"),
            }
        return {
            "run_id": run_id,
            "sim_run_id": sim_run_id,
            "config_ids": config_ids,
            "sim_run": run_meta,
            "setup_configs": None,
            "export_timestamp": _now_iso(),
        }

    @staticmethod
    def _build_performance_csv(comparisons: list[dict[str, Any]]) -> bytes:
        header = [
            "comparison_id", "config_id", "horizon_bd", "expectancy", "win_rate",
            "avg_win", "avg_loss", "profit_factor", "max_drawdown_pct",
            "resolved_outcomes_pct", "list_type",
        ]
        rows = [[c[k] for k in header] for c in comparisons]
        return _rows_to_csv_bytes(header, rows)

    @staticmethod
    def _build_score_buckets_csv(
        step3_scores: list[dict[str, Any]], step5_scores: list[dict[str, Any]]
    ) -> bytes:
        header = ["source", "bucket_low", "bucket_high", "count"]
        counts: dict[tuple[str, int, int], int] = {}
        for source, key, rows in (
            ("step3", "eligibility_score", step3_scores),
            ("step5", "proposal_score_final", step5_scores),
        ):
            for row in rows:
                bucket = _score_bucket(row[key])
                if bucket is None:
                    continue
                ck = (source, bucket[0], bucket[1])
                counts[ck] = counts.get(ck, 0) + 1
        out = [
            [src, low, high, counts[(src, low, high)]]
            for (src, low, high) in sorted(counts)
        ]
        return _rows_to_csv_bytes(header, out)

    @staticmethod
    def _build_setup_performance_csv(
        outcomes: list[dict[str, Any]], step4: list[dict[str, Any]]
    ) -> bytes:
        header = ["setup_type", "horizon_bd", "mean_return_pct", "n"]
        # Map (config, ticker, signal_date) -> setup_type.
        setup_by_key: dict[tuple[Any, Any, Any], str] = {}
        for s in step4:
            if s["setup_type"] is None:
                continue
            setup_by_key[
                (s["setup_config_id"], s["ticker"], s["signal_date"])
            ] = s["setup_type"]

        grouped: dict[tuple[str, int], list[float]] = {}
        for o in outcomes:
            setup = setup_by_key.get(
                (o["setup_config_id"], o["ticker"], o["signal_date"])
            )
            if setup is None:
                continue
            for horizon, col in RETURN_HORIZON_COLUMNS:
                if o[col] is None:
                    continue
                grouped.setdefault((setup, horizon), []).append(o[col])

        rows = []
        for (setup, horizon) in sorted(grouped):
            vals = grouped[(setup, horizon)]
            rows.append([setup, horizon, _mean(vals), len(vals)])
        return _rows_to_csv_bytes(header, rows)

    @staticmethod
    def _build_regime_performance_csv() -> bytes:
        """regime_performance.csv (header-only).

        G-REGIME-SOURCE: ``market_regime`` is not persisted in any simulation
        table, and attaching prod read-only is out of scope for Module 18. The
        file is emitted header-only and documented as an open gap.
        """
        return _rows_to_csv_bytes(
            ["market_regime", "horizon_bd", "mean_return_pct", "n"], []
        )

    @staticmethod
    def _build_drawdowns_csv(outcomes: list[dict[str, Any]]) -> bytes:
        """Per-config 40bd equity-curve drawdowns over the diversified list."""
        header = [
            "setup_config_id", "peak_date", "trough_date", "drawdown_pct"
        ]
        by_config: dict[str, list[dict[str, Any]]] = {}
        for o in outcomes:
            if o["list_membership"] not in DIVERSIFIED_MEMBERSHIPS:
                continue
            if o["return_40bd_pct"] is None:
                continue
            by_config.setdefault(o["setup_config_id"], []).append(o)

        rows: list[list[Any]] = []
        for config_id in sorted(by_config):
            series = sorted(by_config[config_id], key=lambda r: r["signal_date"])
            equity = 1.0
            peak = 1.0
            peak_date = series[0]["signal_date"]
            worst_dd = 0.0
            worst_peak_date = peak_date
            worst_trough_date = peak_date
            for point in series:
                equity *= 1.0 + point["return_40bd_pct"]  # decimal fraction from M17
                if equity > peak:
                    peak = equity
                    peak_date = point["signal_date"]
                drawdown = (equity / peak) - 1.0
                if drawdown < worst_dd:
                    worst_dd = drawdown
                    worst_peak_date = peak_date
                    worst_trough_date = point["signal_date"]
            if worst_dd < 0.0:
                rows.append([
                    config_id,
                    str(worst_peak_date),
                    str(worst_trough_date),
                    round(worst_dd * 100.0, 6),
                ])
        return _rows_to_csv_bytes(header, rows)

    @staticmethod
    def _build_unresolved_csv(outcomes: list[dict[str, Any]]) -> bytes:
        header = [
            "proposal_id", "ticker", "setup_config_id", "signal_date",
            "outcome_status",
        ]
        rows = [
            [o["proposal_id"], o["ticker"], o["setup_config_id"],
             str(o["signal_date"]), o["outcome_status"]]
            for o in outcomes
            if o["outcome_status"] == "partial"
        ]
        return _rows_to_csv_bytes(header, rows)

    # ------------------------------------------------------------------ #
    # Simulation prompt text V1 (G-PROMPT-TEXT).
    # ------------------------------------------------------------------ #
    @staticmethod
    def _sim_prompt_text_v1(sim_run_id: str, data: dict[str, Any]) -> str:
        comparisons = data["performance_metrics"]
        best = None
        worst_dd = None
        resolved_vals: list[float] = []
        for c in comparisons:
            if c["expectancy"] is not None and (
                best is None or c["expectancy"] > best["expectancy"]
            ):
                best = c
            if c["max_drawdown_pct"] is not None and (
                worst_dd is None
                or c["max_drawdown_pct"] > worst_dd["max_drawdown_pct"]
            ):
                worst_dd = c  # largest positive magnitude = worst drawdown
            if c["resolved_outcomes_pct"] is not None:
                resolved_vals.append(c["resolved_outcomes_pct"])

        best_line = (
            f"{best['config_id']} / {best['expectancy']}" if best else "n/a"
        )
        worst_line = (
            f"{worst_dd['config_id']} / {worst_dd['max_drawdown_pct']}"
            if worst_dd
            else "n/a"
        )
        resolved_range = (
            f"{min(resolved_vals)}–{max(resolved_vals)}"
            if resolved_vals
            else "n/a"
        )
        summary = f"{len(comparisons)} config-comparison row(s)."
        return (
            f"[SIMULATION REVIEW — {sim_run_id}]\n"
            f"Best config by expectancy: {best_line}\n"
            f"Worst max drawdown: {worst_line}\n"
            f"Resolved outcomes pct range: {resolved_range}\n"
            f"{summary}\n\n"
            "Assess: which config should be selected and what risks should be "
            "monitored?"
        )

    # ------------------------------------------------------------------ #
    # Simulation review row write (single row).
    # ------------------------------------------------------------------ #
    def _write_sim_review_row(
        self, db_role: str, run_id: str, sim_run_id: str, prompt_text: str
    ) -> None:
        """Write one ``sim_ai_reviews`` row.

        G-SIM-AI-SCHEMA: the frozen ``sim_ai_reviews`` schema (M02 §4.9) has no
        ``review_type`` / ``proposal_id`` / ``selected_tickers_json`` columns, so
        only the columns that exist in the schema are populated. ``review_type``
        is still recorded in the returned ``ServiceResult`` metadata.
        """
        connection = self._db.connect(db_role)
        try:
            connection.execute(
                "INSERT INTO sim_ai_reviews "
                "(ai_review_id, sim_run_id, provider, model, prompt_version, "
                " prompt_text, ai_response_text, human_action, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, "
                " CAST(now() AS TIMESTAMP))",
                [
                    str(uuid.uuid4()),
                    sim_run_id,
                    PROVIDER_MANUAL,
                    MODEL_NONE,
                    PROMPT_VERSION_V1,
                    prompt_text,
                ],
            )
        finally:
            connection.close()

    # ------------------------------------------------------------------ #
    # Fetch helper.
    # ------------------------------------------------------------------ #
    @staticmethod
    def _fetch_dicts(
        connection: Any, sql: str, params: list[Any]
    ) -> list[dict[str, Any]]:
        cursor = connection.execute(sql, params)
        columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    # ------------------------------------------------------------------ #
    # Result builders — ticker.
    # ------------------------------------------------------------------ #
    def _ticker_success(
        self, run_id: str, db_role: str, signal_iso: str,
        setup_config_id: str, proposal_ids: list[str],
        zip_filename: str, zip_path: str, *, rows_processed: int,
    ) -> ServiceResult:
        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=rows_processed,
            metadata=self._ticker_metadata(
                run_id, db_role, signal_iso, setup_config_id, proposal_ids,
                zip_filename, zip_path, STATUS_SUCCESS_META, None,
            ),
        )

    def _ticker_failed(
        self, run_id: str, db_role: str, signal_iso: str,
        setup_config_id: str, proposal_ids: list[str], message: str,
        *, zip_filename: str | None = None, zip_path: str | None = None,
    ) -> ServiceResult:
        return ServiceResult(
            status=service_result.STATUS_FAILED,
            run_id=run_id,
            rows_processed=0,
            errors=[message],
            metadata=self._ticker_metadata(
                run_id, db_role, signal_iso, setup_config_id, proposal_ids,
                zip_filename, zip_path, STATUS_FAILED_META, message,
            ),
        )

    @staticmethod
    def _ticker_metadata(
        run_id: str, db_role: str, signal_iso: str, setup_config_id: str,
        proposal_ids: list[str], zip_filename: str | None, zip_path: str | None,
        status: str, error: str | None,
    ) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "export_type": EXPORT_TYPE_TICKER,
            "db_role": db_role,
            "signal_date": signal_iso,
            "setup_config_id": setup_config_id,
            "proposal_ids": proposal_ids,
            "zip_filename": zip_filename,
            "zip_path": zip_path,
            "review_type": EXPORT_TYPE_TICKER,
            "review_table": REVIEW_TABLE_TICKER,
            "status": status,
            "error": error,
        }

    # ------------------------------------------------------------------ #
    # Result builders — simulation.
    # ------------------------------------------------------------------ #
    def _sim_success(
        self, run_id: str, db_role: str, sim_run_id: str,
        zip_filename: str, zip_path: str, *, rows_processed: int,
    ) -> ServiceResult:
        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=rows_processed,
            metadata=self._sim_metadata(
                run_id, db_role, sim_run_id, zip_filename, zip_path,
                STATUS_SUCCESS_META, None,
            ),
        )

    def _sim_failed(
        self, run_id: str, db_role: str, sim_run_id: str, message: str,
        *, zip_filename: str | None = None, zip_path: str | None = None,
    ) -> ServiceResult:
        return ServiceResult(
            status=service_result.STATUS_FAILED,
            run_id=run_id,
            rows_processed=0,
            errors=[message],
            metadata=self._sim_metadata(
                run_id, db_role, sim_run_id, zip_filename, zip_path,
                STATUS_FAILED_META, message,
            ),
        )

    @staticmethod
    def _sim_metadata(
        run_id: str, db_role: str, sim_run_id: str, zip_filename: str | None,
        zip_path: str | None, status: str, error: str | None,
    ) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "export_type": EXPORT_TYPE_SIM,
            "db_role": db_role,
            "sim_run_id": sim_run_id,
            "zip_filename": zip_filename,
            "zip_path": zip_path,
            "review_type": EXPORT_TYPE_SIM,
            "review_table": REVIEW_TABLE_SIM,
            "status": status,
            "error": error,
        }
