"""Module 21 -- Streamlit Dashboard data-access layer.

Read-only loaders, pure formatting helpers, and the pandas Styler applicator
for the local single-user dashboard.

Architecture rules (Module 21)
-------------------------------
- **Read layer only.**  No ``INSERT`` / ``UPDATE`` / ``DELETE`` / DDL /
  ``ATTACH`` / schema work.  The dashboard never writes to the database.
- **No heavy calculation.**  Upstream module logic (screening, scoring,
  outcome arithmetic) is never re-run here.  The only DB-side aggregation is a
  trivial ``COUNT`` / ``AVG`` over already-computed ``signal_outcomes`` rows --
  a read, not a recomputation.
- **No Streamlit import.**  This module is import-safe and fully unit-testable
  without a running Streamlit server.  Rendering and ``st.session_state`` live
  in :mod:`app.dashboard.app`.
- **Lazy duckdb_manager import.**  ``app.database.duckdb_manager`` is imported
  only inside :meth:`DashboardDataLoader.__init__` when no ``db_manager`` is
  injected.  This keeps the module importable and the pure/fake-connection test
  suite runnable without duckdb installed.
- **No provider imports, no** ``print()``.  Library-style logging only.

``SimDashboardDataLoader`` (Simulation Lab, below) is a deliberate exception
to ``DashboardDataLoader``'s ``ALLOWED_DASHBOARD_ROLES`` restriction -- it is
a separate class that only ever connects to ``simulation.duckdb``, read-only,
for viewing already-written ``sim_runs`` / ``sim_config_comparisons`` /
``sim_folds`` results. It never triggers a simulation run (that stays a CLI
concern, outside the dashboard) and does not relax
``DashboardDataLoader``'s role guard for any other tab.

Source of truth: ``01b_SCHEMA_AND_DATA.md`` (table / view columns,
``selected_proposals_current``), ``01e_UI_AND_TESTING.md`` /
``UI/95_Dashboard_Tab_Specs.md`` (Daily Proposals diversified checkbox + column
set, Pipeline Health / Outcome Tracking panels),
``02_PROJECT_IMPLEMENTATION_CONTEXT.md`` section 19 (dashboard rules: local,
single-user, read-only, no heavy calc), M20 gap G-DASHBOARD-MAT
(``dashboard_materialization`` step is a no-op in the pipeline; M21 is a
standalone viewer), and the current stable
``app/database/schema_manager.py`` (accepted ``step5_proposals`` column set).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Final, Protocol

import pandas as pd

from app.utils import logging_config

_LOG = logging_config.get_logger(__name__)

# --------------------------------------------------------------------------- #
# Roles.
# The dashboard reads only the operational databases; simulation requires an
# ATTACH which the read layer never performs.  Role strings mirror
# app.database.duckdb_manager; that module is imported lazily so the data
# layer stays importable for unit tests that supply a fake manager.
# --------------------------------------------------------------------------- #
DB_ROLE_PROD: Final[str] = "prod"
DB_ROLE_DEBUG: Final[str] = "debug"

ALLOWED_DASHBOARD_ROLES: Final[tuple[str, ...]] = (DB_ROLE_PROD, DB_ROLE_DEBUG)

# --------------------------------------------------------------------------- #
# Row limits.
# --------------------------------------------------------------------------- #
DEFAULT_RUNS_LIMIT: Final[int] = 25
DEFAULT_REPAIR_LIMIT: Final[int] = 50
DEFAULT_REVIEW_LIMIT: Final[int] = 25

# --------------------------------------------------------------------------- #
# Daily Proposals toggle.
# --------------------------------------------------------------------------- #
RAW_RANK_COLUMN: Final[str] = "raw_rank"
DIVERSIFIED_RANK_COLUMN: Final[str] = "diversified_rank"

# Ordered display columns (01e UI spec).  Single source of truth shared by
# data helpers and streamlit_app.py -- they can never fall out of sync.
DISPLAY_COLUMNS: Final[tuple[str, ...]] = (
    "raw_rank",
    "diversified_rank",
    "ticker",
    "setup_type",
    "setup_score",
    "risk_label",
    # final_display_status is the unambiguous display label (read-layer annotation).
    # disposition may say BUY even for cap-excluded rows; final_display_status
    # distinguishes "BUY" (selected_flag=True) from "BUY (excluded)" (cap-excluded).
    # The UI must prefer final_display_status over raw disposition for display.
    "final_display_status",
    "disposition",
    "entry_price_raw",
    "stop_price_raw",
    "target_price_raw",
    "estimated_rr",
    "proposal_score_raw",
    "proposal_score_final",
    "sector",
    "industry",
    "div_reason",
    "mechanical_explanation",
)

# CSS applied to rows where in_raw_top_n != in_diversified_top_n (01e spec).
DISAGREEMENT_HIGHLIGHT_CSS: Final[str] = "background-color: #fff3cd"

# --------------------------------------------------------------------------- #
# final_display_status values (read-layer annotation — no DB column).
#
# Semantics clarification (AD-22.11):
#   selected_top_n  = in_raw_top_n OR in_diversified_top_n
#                     "was ever in a top-N list (raw or diversified)"
#                     — used by outcome queue membership rule.
#   selected_flag   = in_diversified_top_n
#                     "is in the final diversified shortlist"
#                     — the definitive 'final selected' flag.
#
# A row may have disposition=BUY but selected_flag=False when the
# diversification hard cap excluded it (rejection_reason='sector_cap' or
# 'industry_cap').  Such rows must NEVER be shown as final BUY in the UI.
# final_display_status makes this unambiguous at the display layer:
#   "BUY"            — disposition=BUY AND selected_flag=True
#   "BUY (excluded)" — disposition=BUY AND selected_flag=False (cap-excluded)
#   "WATCHLIST_ONLY" — disposition=WATCHLIST_ONLY
#   "REJECTED"       — disposition=REJECTED
# --------------------------------------------------------------------------- #
FINAL_DISPLAY_STATUS_BUY: Final[str] = "BUY"
FINAL_DISPLAY_STATUS_BUY_EXCLUDED: Final[str] = "BUY (excluded)"
FINAL_DISPLAY_STATUS_WATCHLIST: Final[str] = "WATCHLIST_ONLY"
FINAL_DISPLAY_STATUS_REJECTED: Final[str] = "REJECTED"

# --------------------------------------------------------------------------- #
# Outcome status.
# --------------------------------------------------------------------------- #
RESOLVED_OUTCOME_STATUSES: Final[tuple[str, ...]] = ("complete",)


# --------------------------------------------------------------------------- #
# Protocol for injected DB manager.
# --------------------------------------------------------------------------- #
class _DbManagerLike(Protocol):
    def connect(self, db_role: str, read_only: bool = ...) -> Any: ...


class UnknownDashboardRoleError(ValueError):
    """Raised when a caller requests a db_role the dashboard may not read."""


# --------------------------------------------------------------------------- #
# Result containers.
# --------------------------------------------------------------------------- #
@dataclass
class ProposalsView:
    """Result of :meth:`DashboardDataLoader.load_daily_proposals`.

    Attributes
    ----------
    rows:
        Annotated proposal rows (``list_disagreement`` and ``div_reason``
        added), ordered by the active rank column.
    show_diversified:
        Whether the diversified shortlist was requested.
    rank_column:
        The rank column that ordered/filtered ``rows``.
    signal_date:
        The signal date the rows belong to (``None`` when no data found).
    run_id:
        The single run_id the rows were scoped to (``None`` when no data).
    setup_config_id:
        The setup config filter applied (``None`` = all configs).
    """

    rows: list[dict[str, Any]] = field(default_factory=list)
    show_diversified: bool = True
    rank_column: str = DIVERSIFIED_RANK_COLUMN
    signal_date: date | None = None
    run_id: str | None = None
    setup_config_id: str | None = None


@dataclass
class OutcomeSummary:
    """Aggregate outcome counts/averages for the Outcome Tracking panel."""

    total: int = 0
    resolved: int = 0
    unresolved: int = 0
    avg_return_5bd_pct: float | None = None
    avg_return_10bd_pct: float | None = None
    avg_return_20bd_pct: float | None = None
    avg_return_40bd_pct: float | None = None
    setup_type: str | None = None
    risk_label: str | None = None
    setup_config_id: str | None = None


# --------------------------------------------------------------------------- #
# Pure helpers (no I/O -- directly unit-testable).
# --------------------------------------------------------------------------- #
def validate_role(db_role: str) -> str:
    """Return *db_role* if the dashboard may read it, otherwise raise.

    Raises
    ------
    UnknownDashboardRoleError
        When *db_role* is not in :data:`ALLOWED_DASHBOARD_ROLES`.
    """
    if db_role not in ALLOWED_DASHBOARD_ROLES:
        raise UnknownDashboardRoleError(
            f"Dashboard cannot read db_role {db_role!r}. "
            f"Allowed: {list(ALLOWED_DASHBOARD_ROLES)}"
        )
    return db_role


def rank_column_for(show_diversified: bool) -> str:
    """Return the rank column selected by the diversified toggle.

    Checked (diversified) -> ``diversified_rank``;
    unchecked (raw)       -> ``raw_rank``.
    Per ``01e_UI_AND_TESTING.md`` Daily Proposals spec.
    """
    return DIVERSIFIED_RANK_COLUMN if show_diversified else RAW_RANK_COLUMN


def membership_column_for(show_diversified: bool) -> str:
    """Return the membership flag the toggle filters on."""
    return "in_diversified_top_n" if show_diversified else "in_raw_top_n"


def derive_div_reason(row: dict[str, Any]) -> str | None:
    """Derive the 'Div. Reason' cell from already-stored proposal fields.

    No recomputation: prefers the stored ``rejection_reason``; falls back to
    the stored ``diversity_penalty`` when non-zero.  Returns ``None`` when
    neither applies.
    """
    reason = row.get("rejection_reason")
    if reason:
        return str(reason)
    penalty = row.get("diversity_penalty")
    if penalty is not None and penalty != 0:
        return f"diversity_penalty={penalty}"
    return None


def derive_final_display_status(row: dict[str, Any]) -> str:
    """Return the unambiguous display status for one proposal row.

    This is a read-layer annotation: it does NOT add a DB column.

    Rules (implements AD-22.11 semantics):
    - BUY             : disposition=BUY  AND selected_flag=True
    - BUY (excluded)  : disposition=BUY  AND selected_flag=False
                        (hard cap excluded this row from the diversified list)
    - WATCHLIST_ONLY  : disposition=WATCHLIST_ONLY
    - REJECTED        : any other disposition

    A row must NEVER be rendered as a final BUY when selected_flag is False,
    even if its stored disposition column says "BUY".
    """
    disposition = row.get("disposition", "")
    selected = bool(row.get("selected_flag", False))
    if disposition == "BUY":
        return FINAL_DISPLAY_STATUS_BUY if selected else FINAL_DISPLAY_STATUS_BUY_EXCLUDED
    if disposition == "WATCHLIST_ONLY":
        return FINAL_DISPLAY_STATUS_WATCHLIST
    return FINAL_DISPLAY_STATUS_REJECTED


def annotate_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add ``list_disagreement``, ``div_reason``, and ``final_display_status`` to each proposal row.

    Annotations injected (all read-layer only — no DB columns written):

    ``list_disagreement``
        ``True`` when ``in_raw_top_n != in_diversified_top_n`` (the rows the
        UI highlights per 01e spec).

    ``div_reason``
        Human-readable diversification reason derived from stored fields
        (rejection_reason or diversity_penalty).

    ``final_display_status``
        Unambiguous display status — see :func:`derive_final_display_status`.
        Ensures rows with ``disposition=BUY`` but ``selected_flag=False``
        (cap-excluded) are never shown as final BUY in the UI.

    Input rows are not mutated.
    """
    annotated: list[dict[str, Any]] = []
    for row in rows:
        new_row = dict(row)
        new_row["list_disagreement"] = bool(
            row.get("in_raw_top_n") != row.get("in_diversified_top_n")
        )
        new_row["div_reason"] = derive_div_reason(row)
        new_row["final_display_status"] = derive_final_display_status(row)
        annotated.append(new_row)
    return annotated


def extract_disagreement_flags(rows: list[dict[str, Any]]) -> list[bool]:
    """Return a parallel list of disagreement flags from annotated proposal rows.

    Each flag is ``True`` when the corresponding row has
    ``in_raw_top_n != in_diversified_top_n`` (i.e. ``list_disagreement``
    injected by :func:`annotate_rows`).  streamlit_app.py uses this list as the styling
    source so it cannot accidentally reuse the stripped display DataFrame, which
    no longer contains ``list_disagreement``.
    """
    return [bool(r.get("list_disagreement", False)) for r in rows]


def highlight_css_for_row(is_disagreement: bool) -> str:
    """Return the per-cell CSS string for one row of the proposals table.

    Returns :data:`DISAGREEMENT_HIGHLIGHT_CSS` when the row disagrees between
    the raw and diversified shortlists, otherwise an empty string.
    Pure -- no pandas, no Streamlit, directly unit-testable.
    """
    return DISAGREEMENT_HIGHLIGHT_CSS if is_disagreement else ""


def build_proposals_display(
    view: ProposalsView,
) -> tuple[list[dict[str, Any]], list[bool]]:
    """Split a :class:`ProposalsView` into the display slice and flag list.

    Returns a 2-tuple:

    - ``display_rows`` -- one dict per row containing only :data:`DISPLAY_COLUMNS`
      keys.  ``list_disagreement`` is deliberately excluded from the visible
      table.
    - ``flags`` -- parallel ``list[bool]`` of disagreement flags from the
      *same* ``view.rows`` source, so streamlit_app.py cannot accidentally decouple the
      two.

    Pure -- no pandas, no Streamlit, directly unit-testable.
    """
    display_rows = [{c: r.get(c) for c in DISPLAY_COLUMNS} for r in view.rows]
    flags = extract_disagreement_flags(view.rows)
    return display_rows, flags


def highlight_row(row: pd.Series, flags: list[bool]) -> list[str]:
    """Apply per-cell CSS to one row of the proposals pandas Styler.

    Called via ``df.style.apply(highlight_row, flags=flags, axis=1)``.
    ``row.name`` is the integer positional RangeIndex; ``flags[row.name]``
    is the precomputed disagreement flag for that position.
    Pure pandas -- no Streamlit -- directly unit-testable.
    """
    css = highlight_css_for_row(flags[int(row.name)])
    return [css] * len(row)


# --------------------------------------------------------------------------- #
# Internal query builders.
# rank_column / membership_column come from the validated allow-list above
# (never user free-text), so embedding them in the SQL string is safe.
# --------------------------------------------------------------------------- #
def _proposals_sql(
    *,
    rank_column: str,
    membership_column: str,
    with_setup_config: bool,
) -> str:
    setup_config_clause = "AND p.setup_config_id = ? " if with_setup_config else ""
    return (
        "SELECT "
        "p.raw_rank, p.diversified_rank, p.ticker, "
        "p.setup_type, p.setup_score, p.risk_label, p.disposition, "
        "p.entry_price_raw, p.stop_price_raw, p.target_price_raw, "
        "p.estimated_rr, p.proposal_score_raw, p.proposal_score_final, "
        "p.diversity_penalty, p.in_raw_top_n, p.in_diversified_top_n, "
        "p.selected_flag, p.selected_top_n, "
        "p.diversification_applied, p.rejection_reason, p.mechanical_explanation, "
        "m.sector, m.industry "
        "FROM step5_proposals p "
        "LEFT JOIN ticker_master m ON m.ticker = p.ticker "
        "WHERE p.run_id = ? AND p.signal_date = ? "
        f"AND p.{membership_column} = TRUE "
        f"{setup_config_clause}"
        f"ORDER BY p.{rank_column} ASC NULLS LAST, p.ticker ASC"
    )


# --------------------------------------------------------------------------- #
# Loader.
# --------------------------------------------------------------------------- #
class DashboardDataLoader:
    """Read-only data loader for the Streamlit dashboard.

    The optional ``db_manager`` argument exists only for test injection; when
    ``None``, the approved :mod:`app.database.duckdb_manager` is imported
    lazily and used.  ``db_role`` is validated against
    :data:`ALLOWED_DASHBOARD_ROLES`.

    Every connection is opened with ``read_only=True`` and closed in a
    ``finally`` block.  No SQL here is DDL, ATTACH, or a write statement.
    """

    def __init__(
        self,
        db_manager: _DbManagerLike | None = None,
        db_role: str = DB_ROLE_PROD,
    ) -> None:
        if db_manager is not None:
            self._db: _DbManagerLike = db_manager
        else:
            # Lazy import so the module is importable without duckdb when a
            # fake manager is injected (unit tests).
            from app.database import duckdb_manager  # noqa: PLC0415

            self._db = duckdb_manager
        self._db_role: str = validate_role(db_role)

    @property
    def db_role(self) -> str:
        return self._db_role

    # ------------------------------------------------------------------ #
    # Internal fetch helpers (always read-only).
    # ------------------------------------------------------------------ #
    def _fetch_dicts(self, sql: str, params: list[Any]) -> list[dict[str, Any]]:
        conn = self._db.connect(self._db_role, read_only=True)
        try:
            cursor = conn.execute(sql, params)
            columns = [d[0] for d in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        finally:
            conn.close()

    def _fetch_one(self, sql: str, params: list[Any]) -> tuple[Any, ...] | None:
        conn = self._db.connect(self._db_role, read_only=True)
        try:
            return conn.execute(sql, params).fetchone()
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    # Pickers (feed the UI selectors).
    # ------------------------------------------------------------------ #
    def list_signal_dates(self, limit: int = 60) -> list[date]:
        """Distinct ``step5_proposals`` signal dates, newest first."""
        rows = self._fetch_dicts(
            "SELECT DISTINCT signal_date FROM step5_proposals "
            "ORDER BY signal_date DESC LIMIT ?",
            [int(limit)],
        )
        return [r["signal_date"] for r in rows]

    def list_setup_configs(self) -> list[str]:
        """Distinct setup config ids present in ``step5_proposals``."""
        rows = self._fetch_dicts(
            "SELECT DISTINCT setup_config_id FROM step5_proposals "
            "ORDER BY setup_config_id ASC",
            [],
        )
        return [r["setup_config_id"] for r in rows]

    def latest_signal_date(self) -> date | None:
        """Most recent signal date with proposals, or ``None``."""
        row = self._fetch_one(
            "SELECT MAX(signal_date) FROM step5_proposals", []
        )
        return row[0] if row is not None else None

    def latest_run_id_for_date(
        self, signal_date: date, setup_config_id: str | None = None
    ) -> str | None:
        """Most recent run_id that has proposals for *signal_date*.

        Scoping to one run_id keeps the table deterministic when more than one
        run wrote proposals for the same date.
        """
        params: list[Any] = [signal_date]
        setup_config_clause = ""
        if setup_config_id is not None:
            setup_config_clause = "AND setup_config_id = ? "
            params.append(setup_config_id)
        row = self._fetch_one(
            "SELECT run_id FROM step5_proposals "
            "WHERE signal_date = ? "
            f"{setup_config_clause}"
            "ORDER BY created_at DESC LIMIT 1",
            params,
        )
        return row[0] if row is not None else None

    # ------------------------------------------------------------------ #
    # Daily Proposals (raw vs. diversified shortlist).
    # ------------------------------------------------------------------ #
    def load_daily_proposals(
        self,
        signal_date: date | None = None,
        setup_config_id: str | None = None,
        show_diversified: bool = True,
    ) -> ProposalsView:
        """Load the proposal shortlist for one signal date and run.

        Honors the diversified checkbox (``01e_UI_AND_TESTING.md``):

        - ``show_diversified=True``  -> ``in_diversified_top_n = TRUE``,
          ordered by ``diversified_rank ASC``.
        - ``show_diversified=False`` -> ``in_raw_top_n = TRUE``,
          ordered by ``raw_rank ASC``.

        Rows are annotated with ``list_disagreement`` / ``div_reason`` via
        :func:`annotate_rows`.  Proposals are scoped to the most recent run_id
        for the chosen date.  ``step4_analysis`` (setup_type, estimated_rr)
        and ``ticker_master`` (sector, industry) are joined with ``LEFT JOIN``
        so missing join rows never drop a proposal.
        """
        rank_col = rank_column_for(show_diversified)
        membership_col = membership_column_for(show_diversified)

        resolved_date = signal_date or self.latest_signal_date()
        if resolved_date is None:
            return ProposalsView(
                rows=[],
                show_diversified=show_diversified,
                rank_column=rank_col,
                signal_date=None,
                run_id=None,
                setup_config_id=setup_config_id,
            )

        run_id = self.latest_run_id_for_date(resolved_date, setup_config_id)
        if run_id is None:
            return ProposalsView(
                rows=[],
                show_diversified=show_diversified,
                rank_column=rank_col,
                signal_date=resolved_date,
                run_id=None,
                setup_config_id=setup_config_id,
            )

        params: list[Any] = [run_id, resolved_date]
        if setup_config_id is not None:
            params.append(setup_config_id)

        sql = _proposals_sql(
            rank_column=rank_col,
            membership_column=membership_col,
            with_setup_config=setup_config_id is not None,
        )
        rows = annotate_rows(self._fetch_dicts(sql, params))

        return ProposalsView(
            rows=rows,
            show_diversified=show_diversified,
            rank_column=rank_col,
            signal_date=resolved_date,
            run_id=run_id,
            setup_config_id=setup_config_id,
        )

    # ------------------------------------------------------------------ #
    # Pipeline Health panels.
    # ------------------------------------------------------------------ #
    def load_pipeline_runs(
        self, limit: int = DEFAULT_RUNS_LIMIT
    ) -> list[dict[str, Any]]:
        """Recent ``pipeline_runs`` rows, newest first."""
        return self._fetch_dicts(
            "SELECT run_id, run_date, run_type, status, started_at, "
            "completed_at, duration_sec, steps_completed, error_message "
            "FROM pipeline_runs ORDER BY started_at DESC LIMIT ?",
            [int(limit)],
        )

    def load_repair_queue(
        self, limit: int = DEFAULT_REPAIR_LIMIT
    ) -> list[dict[str, Any]]:
        """Recent ``data_repair_queue`` rows, newest first."""
        return self._fetch_dicts(
            "SELECT repair_id, ticker, repair_date, repair_reason, attempts, "
            "max_attempts, status, created_at "
            "FROM data_repair_queue ORDER BY created_at DESC LIMIT ?",
            [int(limit)],
        )

    def latest_pipeline_status(self) -> dict[str, Any] | None:
        """The single most recent ``pipeline_runs`` row, or ``None``."""
        rows = self.load_pipeline_runs(limit=1)
        return rows[0] if rows else None

    # ------------------------------------------------------------------ #
    # Outcome Tracking (trivial COUNT/AVG read -- no recomputation).
    # ------------------------------------------------------------------ #
    def load_outcome_summary(
        self, setup_config_id: str | None = None
    ) -> OutcomeSummary:
        """Aggregate counts/averages over already-computed ``signal_outcomes``.

        The only SQL aggregation here is ``COUNT`` / ``AVG`` -- a read of
        precomputed rows, not a recomputation of Module 16/17 logic.
        Placeholder order: resolved-status list (for the ``IN`` clause in
        SELECT) then the optional strategy filter (for WHERE).
        """
        resolved_list = ", ".join("?" for _ in RESOLVED_OUTCOME_STATUSES)
        params: list[Any] = list(RESOLVED_OUTCOME_STATUSES)
        where = ""
        if setup_config_id is not None:
            where = "WHERE setup_config_id = ? "
            params.append(setup_config_id)
        row = self._fetch_one(
            "SELECT COUNT(*) AS total, "
            f"SUM(CASE WHEN outcome_status IN ({resolved_list}) "
            "  THEN 1 ELSE 0 END) AS resolved, "
            "AVG(return_5bd_pct) AS r5, "
            "AVG(return_10bd_pct) AS r10, "
            "AVG(return_20bd_pct) AS r20, "
            "AVG(return_40bd_pct) AS r40 "
            "FROM signal_outcomes "
            f"{where}",
            params,
        )
        if row is None:
            return OutcomeSummary(setup_config_id=setup_config_id)
        total = int(row[0] or 0)
        resolved = int(row[1] or 0)
        return OutcomeSummary(
            total=total,
            resolved=resolved,
            unresolved=max(total - resolved, 0),
            avg_return_5bd_pct=row[2],
            avg_return_10bd_pct=row[3],
            avg_return_20bd_pct=row[4],
            avg_return_40bd_pct=row[5],
            setup_config_id=setup_config_id,
        )

    # ------------------------------------------------------------------ #
    # AI Review summary.
    # ------------------------------------------------------------------ #
    def load_ai_reviews(
        self, limit: int = DEFAULT_REVIEW_LIMIT
    ) -> list[dict[str, Any]]:
        """Recent ``ai_reviews`` metadata rows, newest first."""
        return self._fetch_dicts(
            "SELECT ai_review_id, review_type, proposal_id, provider, model, "
            "prompt_version, human_action, created_at "
            "FROM ai_reviews ORDER BY created_at DESC LIMIT ?",
            [int(limit)],
        )

    # ------------------------------------------------------------------ #
    # Proposal id lookup (used by action_service for CSV / ZIP export).
    # ------------------------------------------------------------------ #
    def load_proposal_ids(
        self,
        run_id: str,
        signal_date: date,
        tickers: list[str],
        setup_config_id: str | None = None,
    ) -> list[str]:
        """Return ``proposal_id`` values for the given tickers within a run.

        Read-only; used by the Streamlit layer to resolve checkbox-selected
        tickers into stable proposal ids before calling the action service.
        """
        if not tickers:
            return []
        placeholders = ", ".join(["?"] * len(tickers))
        params: list[Any] = [run_id, signal_date] + list(tickers)
        setup_config_clause = ""
        if setup_config_id is not None:
            setup_config_clause = "AND setup_config_id = ? "
            params.append(setup_config_id)
        rows = self._fetch_dicts(
            "SELECT proposal_id FROM step5_proposals "
            "WHERE run_id = ? AND signal_date = ? "
            f"AND ticker IN ({placeholders}) "
            f"{setup_config_clause}"
            "ORDER BY raw_rank ASC NULLS LAST",
            params,
        )
        return [r["proposal_id"] for r in rows]


# --------------------------------------------------------------------------- #
# Simulation Lab — separate read-only loader.
#
# DashboardDataLoader (above) intentionally rejects db_role="simulation" (see
# UnknownDashboardRoleError / ALLOWED_DASHBOARD_ROLES) -- that restriction is
# load-bearing for every other tab and stays untouched. Simulation Lab reads
# are scoped to this standalone class instead, connecting directly to
# simulation.duckdb (no ATTACH; sim_runs/sim_config_comparisons/sim_folds are
# self-contained tables already written by SimulationEngine). Same read-only /
# closed-in-finally discipline as DashboardDataLoader.
# --------------------------------------------------------------------------- #
DB_ROLE_SIMULATION: Final[str] = "simulation"


@dataclass
class SimRunSummary:
    """One ``sim_runs`` row, with ``config_ids`` parsed from JSON."""

    sim_run_id: str
    sim_name: str | None
    mode: str = ""
    start_date: date | None = None
    end_date: date | None = None
    created_at: Any = None
    config_ids: list[str] = field(default_factory=list)
    status: str = ""
    notes: str | None = None


class SimDashboardDataLoader:
    """Read-only loader for the Simulation Lab page (``simulation.duckdb`` only).

    Deliberately not a subclass of :class:`DashboardDataLoader` and not gated
    by :data:`ALLOWED_DASHBOARD_ROLES` -- this is the one dashboard surface
    that *is* allowed to read the simulation database, per
    ``M21_STREAMLIT_DASHBOARD_SPEC.md``'s read-only carve-out for viewing
    already-written simulation results (as opposed to triggering runs, which
    stays outside the dashboard entirely -- see ``tools/run_simulation.py``
    follow-up note).
    """

    def __init__(self, db_manager: _DbManagerLike | None = None) -> None:
        if db_manager is not None:
            self._db: _DbManagerLike = db_manager
        else:
            from app.database import duckdb_manager  # noqa: PLC0415

            self._db = db_manager if db_manager is not None else duckdb_manager

    def _fetch_dicts(self, sql: str, params: list[Any]) -> list[dict[str, Any]]:
        conn = self._db.connect(DB_ROLE_SIMULATION, read_only=True)
        try:
            cursor = conn.execute(sql, params)
            columns = [d[0] for d in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        finally:
            conn.close()

    def list_sim_runs(self, limit: int = DEFAULT_RUNS_LIMIT) -> list[SimRunSummary]:
        """Recent ``sim_runs`` rows, newest first."""
        rows = self._fetch_dicts(
            "SELECT sim_run_id, sim_name, mode, start_date, end_date, "
            "created_at, config_ids, status, notes "
            "FROM sim_runs ORDER BY created_at DESC LIMIT ?",
            [int(limit)],
        )
        summaries: list[SimRunSummary] = []
        for r in rows:
            config_ids = r.get("config_ids")
            if isinstance(config_ids, str):
                try:
                    config_ids = json.loads(config_ids)
                except (ValueError, TypeError):
                    config_ids = []
            summaries.append(
                SimRunSummary(
                    sim_run_id=r["sim_run_id"],
                    sim_name=r.get("sim_name"),
                    mode=r.get("mode") or "",
                    start_date=r.get("start_date"),
                    end_date=r.get("end_date"),
                    created_at=r.get("created_at"),
                    config_ids=list(config_ids or []),
                    status=r.get("status") or "",
                    notes=r.get("notes"),
                )
            )
        return summaries

    def load_sim_config_comparisons(
        self,
        sim_run_id: str,
        setup_type: str | None = None,
        risk_label: str | None = None,
    ) -> list[dict[str, Any]]:
        """``sim_config_comparisons`` rows for one run, optionally filtered.

        Columns per ``01e_UI_AND_TESTING.md``'s Simulation Lab spec:
        config_id, setup_type, risk_label, expectancy, win_rate,
        profit_factor, stop_hit_rate, target_hit_rate, max_drawdown_pct,
        resolved_outcomes_pct. Note: ``stop_hit_rate``/``target_hit_rate``
        are declared in the schema but not currently populated by
        ``SimulationEngine``'s insert (pre-existing gap, out of scope here)
        -- they will read back as ``NULL`` until that's fixed.
        """
        params: list[Any] = [sim_run_id]
        clauses = ""
        if setup_type is not None:
            clauses += "AND setup_type = ? "
            params.append(setup_type)
        if risk_label is not None:
            clauses += "AND risk_label = ? "
            params.append(risk_label)
        return self._fetch_dicts(
            "SELECT config_id, setup_type, risk_label, horizon_bd, list_type, "
            "expectancy, win_rate, avg_win, avg_loss, profit_factor, "
            "stop_hit_rate, target_hit_rate, max_drawdown_pct, "
            "resolved_outcomes_pct "
            "FROM sim_config_comparisons WHERE sim_run_id = ? "
            f"{clauses}"
            "ORDER BY setup_type ASC, risk_label ASC NULLS LAST, config_id ASC",
            params,
        )

    def load_sim_folds(self, sim_run_id: str) -> list[dict[str, Any]]:
        """Walk-forward fold boundaries for one run, ordered by fold_number."""
        return self._fetch_dicts(
            "SELECT fold_id, fold_number, train_start, train_end, "
            "test_start, test_end, selected_config_id "
            "FROM sim_folds WHERE sim_run_id = ? ORDER BY fold_number ASC",
            [sim_run_id],
        )
