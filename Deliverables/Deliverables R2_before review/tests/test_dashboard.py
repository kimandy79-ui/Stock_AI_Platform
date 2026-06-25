"""Tests for Module 21 -- Streamlit Dashboard data-access layer.

Coverage in two layers:

**Pure / fake-connection tests (no duckdb, no streamlit)**
    Exercise the role guard, diversified-toggle column selection, ``div_reason``
    derivation, ``annotate_rows`` mutation-safety, ``build_proposals_display``
    source coupling, ``highlight_css_for_row``, ``extract_disagreement_flags``,
    read-only enforcement, empty-result handling, and every loader method's
    column wiring -- all against an in-memory :class:`FakeDbManager`.  Run
    offline with no real DB file and no Streamlit server.

**Pandas Styler test (requires ``pandas``, no streamlit)**
    Verifies that :func:`highlight_row` applies the amber highlight to
    exactly the disagreeing rows when wired through a real pandas Styler.
    ``pytest.importorskip("pandas")`` keeps it skippable if pandas is absent.

**DuckDB-backed integration test (requires ``duckdb``)**
    Applies the real Module 03 schema to a ``tmp_path`` prod DB (via
    ``monkeypatch`` on ``settings.PROD_DB_PATH``), inserts synthetic proposal
    rows, and asserts the raw-vs-diversified shortlist end-to-end.
    ``pytest.importorskip("duckdb")`` skips when duckdb is absent.

No test touches a real prod / debug / simulation DB file.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from app.dashboard import data_access
from app.dashboard.data_access import (
    DISAGREEMENT_HIGHLIGHT_CSS,
    DISPLAY_COLUMNS,
    ProposalsView,
    UnknownDashboardRoleError,
    annotate_rows,
    build_proposals_display,
    derive_div_reason,
    extract_disagreement_flags,
    highlight_css_for_row,
    highlight_row,
    membership_column_for,
    rank_column_for,
    validate_role,
)
from app.dashboard.data_access import DashboardDataLoader

SIGNAL_DATE = date(2024, 3, 1)
RUN_ID = "run-abc-123"


# --------------------------------------------------------------------------- #
# Pure-Python fakes (no duckdb dependency).
# --------------------------------------------------------------------------- #
class _FakeCursor:
    def __init__(self, columns: list[str], rows: list[tuple[Any, ...]]) -> None:
        self._columns = columns
        self._rows = rows

    @property
    def description(self) -> list[tuple[str]]:
        return [(c,) for c in self._columns]

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


class _FakeConnection:
    """One preloaded response per connection (one query per open/close cycle)."""

    def __init__(self, columns: list[str], rows: list[tuple[Any, ...]]) -> None:
        self._columns = columns
        self._rows = rows
        self.executed: list[tuple[str, Any]] = []
        self.closed = False

    def execute(self, sql: str, params: Any = None) -> _FakeCursor:
        self.executed.append((sql, params))
        return _FakeCursor(self._columns, self._rows)

    def close(self) -> None:
        self.closed = True


class FakeDbManager:
    """Dispenses one preloaded :class:`_FakeConnection` per ``connect`` call."""

    def __init__(
        self,
        responses: list[tuple[list[str], list[tuple[Any, ...]]]],
    ) -> None:
        self._queue = list(responses)
        self.read_only_flags: list[bool] = []
        self.roles: list[str] = []
        self.connections: list[_FakeConnection] = []

    def connect(self, db_role: str, read_only: bool = False) -> _FakeConnection:
        self.read_only_flags.append(read_only)
        self.roles.append(db_role)
        if not self._queue:
            raise AssertionError(
                f"FakeDbManager has no more queued responses (role={db_role!r})"
            )
        columns, rows = self._queue.pop(0)
        conn = _FakeConnection(columns, rows)
        self.connections.append(conn)
        return conn


# --------------------------------------------------------------------------- #
# Role guard tests.
# --------------------------------------------------------------------------- #
def test_validate_role_allows_prod_and_debug() -> None:
    assert validate_role("prod") == "prod"
    assert validate_role("debug") == "debug"


def test_validate_role_rejects_simulation_and_unknown() -> None:
    with pytest.raises(UnknownDashboardRoleError):
        validate_role("simulation")
    with pytest.raises(UnknownDashboardRoleError):
        validate_role("arbitrary")


def test_loader_rejects_disallowed_role() -> None:
    with pytest.raises(UnknownDashboardRoleError):
        DashboardDataLoader(db_manager=FakeDbManager([]), db_role="simulation")


# --------------------------------------------------------------------------- #
# Toggle helper tests.
# --------------------------------------------------------------------------- #
def test_rank_column_follows_toggle() -> None:
    assert rank_column_for(True) == "diversified_rank"
    assert rank_column_for(False) == "raw_rank"


def test_membership_column_follows_toggle() -> None:
    assert membership_column_for(True) == "in_diversified_top_n"
    assert membership_column_for(False) == "in_raw_top_n"


# --------------------------------------------------------------------------- #
# Annotation helper tests.
# --------------------------------------------------------------------------- #
def test_derive_div_reason_prefers_rejection_reason() -> None:
    assert derive_div_reason({"rejection_reason": "sector cap"}) == "sector cap"


def test_derive_div_reason_falls_back_to_penalty() -> None:
    row = {"rejection_reason": None, "diversity_penalty": 0.25}
    assert derive_div_reason(row) == "diversity_penalty=0.25"


def test_derive_div_reason_zero_penalty_returns_none() -> None:
    assert derive_div_reason({"rejection_reason": None, "diversity_penalty": 0}) is None


def test_derive_div_reason_empty_row_returns_none() -> None:
    assert derive_div_reason({}) is None


def test_annotate_rows_adds_list_disagreement() -> None:
    rows = [
        {"in_raw_top_n": True, "in_diversified_top_n": True},
        {"in_raw_top_n": True, "in_diversified_top_n": False},
        {"in_raw_top_n": False, "in_diversified_top_n": True},
    ]
    out = annotate_rows(rows)
    assert out[0]["list_disagreement"] is False
    assert out[1]["list_disagreement"] is True
    assert out[2]["list_disagreement"] is True


def test_annotate_rows_does_not_mutate_input() -> None:
    rows = [{"in_raw_top_n": True, "in_diversified_top_n": False}]
    annotate_rows(rows)
    assert "list_disagreement" not in rows[0]


# --------------------------------------------------------------------------- #
# Highlighting helper tests.
# --------------------------------------------------------------------------- #
def test_extract_disagreement_flags_from_annotated_rows() -> None:
    rows = [
        {"list_disagreement": True},
        {"list_disagreement": False},
        {},  # missing key defaults to False
    ]
    assert extract_disagreement_flags(rows) == [True, False, False]


def test_highlight_css_for_row_true() -> None:
    assert highlight_css_for_row(True) == DISAGREEMENT_HIGHLIGHT_CSS


def test_highlight_css_for_row_false() -> None:
    assert highlight_css_for_row(False) == ""


# --------------------------------------------------------------------------- #
# build_proposals_display coupling tests.
# --------------------------------------------------------------------------- #
def test_build_proposals_display_strips_internal_fields() -> None:
    rows = [
        {
            "raw_rank": 1,
            "ticker": "AAPL",
            "in_raw_top_n": True,
            "in_diversified_top_n": False,
            "list_disagreement": True,
            "div_reason": "sector cap",
        },
    ]
    view = ProposalsView(rows=rows)
    display_rows, flags = build_proposals_display(view)

    assert len(display_rows) == 1
    assert "list_disagreement" not in display_rows[0]
    assert "in_raw_top_n" not in display_rows[0]
    assert "in_diversified_top_n" not in display_rows[0]
    # Every DISPLAY_COLUMN must be present as a key.
    for col in DISPLAY_COLUMNS:
        assert col in display_rows[0], f"missing column: {col}"


def test_build_proposals_display_flags_match_rows() -> None:
    rows = [
        {"list_disagreement": True},
        {"list_disagreement": False},
        {"list_disagreement": True},
    ]
    view = ProposalsView(rows=rows)
    _, flags = build_proposals_display(view)
    assert flags == [True, False, True]


def test_build_proposals_display_empty_view() -> None:
    view = ProposalsView(rows=[])
    display_rows, flags = build_proposals_display(view)
    assert display_rows == []
    assert flags == []


# --------------------------------------------------------------------------- #
# Loader: read-only + role propagation.
# --------------------------------------------------------------------------- #
def test_loaders_use_read_only_connections() -> None:
    fake = FakeDbManager([(["signal_date"], [(SIGNAL_DATE,)])])
    loader = DashboardDataLoader(db_manager=fake, db_role="prod")
    loader.list_signal_dates()
    assert fake.read_only_flags == [True]
    assert fake.roles == ["prod"]
    assert fake.connections[0].closed is True


def test_loader_propagates_debug_role() -> None:
    fake = FakeDbManager([(["signal_date"], [])])
    loader = DashboardDataLoader(db_manager=fake, db_role="debug")
    loader.list_signal_dates()
    assert fake.roles == ["debug"]


# --------------------------------------------------------------------------- #
# load_daily_proposals -- toggle wiring.
# --------------------------------------------------------------------------- #
_PROPOSAL_COLUMNS = [
    "raw_rank", "diversified_rank", "ticker", "strategy_config_id",
    "proposal_score_raw", "proposal_score_final", "diversity_penalty",
    "in_raw_top_n", "in_diversified_top_n", "diversification_applied",
    "rejection_reason", "mechanical_explanation",
    "setup_type", "estimated_rr", "sector", "industry",
]


def _proposal_row(**overrides: Any) -> tuple[Any, ...]:
    defaults: dict[str, Any] = {
        "raw_rank": 2, "diversified_rank": 1, "ticker": "AAPL",
        "strategy_config_id": "cfg-1",
        "proposal_score_raw": 80.0, "proposal_score_final": 78.0,
        "diversity_penalty": 0.0,
        "in_raw_top_n": False, "in_diversified_top_n": True,
        "diversification_applied": True,
        "rejection_reason": None, "mechanical_explanation": "breakout",
        "setup_type": "breakout", "estimated_rr": 2.5,
        "sector": "Tech", "industry": "Hardware",
    }
    defaults.update(overrides)
    return tuple(defaults[c] for c in _PROPOSAL_COLUMNS)


def test_diversified_toggle_uses_div_rank_and_membership() -> None:
    fake = FakeDbManager([
        (["run_id"], [(RUN_ID,)]),           # latest_run_id_for_date
        (_PROPOSAL_COLUMNS, [_proposal_row()]),  # proposals query
    ])
    loader = DashboardDataLoader(db_manager=fake, db_role="prod")
    view = loader.load_daily_proposals(
        signal_date=SIGNAL_DATE, show_diversified=True
    )

    assert view.rank_column == "diversified_rank"
    assert view.run_id == RUN_ID
    assert len(view.rows) == 1
    # row is annotated: in_raw_top_n=False, in_diversified_top_n=True -> disagreement
    assert view.rows[0]["list_disagreement"] is True
    sql_issued = fake.connections[1].executed[0][0]
    assert "in_diversified_top_n = TRUE" in sql_issued
    assert "ORDER BY p.diversified_rank" in sql_issued


def test_raw_toggle_uses_raw_rank_and_membership() -> None:
    fake = FakeDbManager([
        (["run_id"], [(RUN_ID,)]),
        (_PROPOSAL_COLUMNS, []),
    ])
    loader = DashboardDataLoader(db_manager=fake, db_role="prod")
    view = loader.load_daily_proposals(
        signal_date=SIGNAL_DATE, show_diversified=False
    )

    assert view.rank_column == "raw_rank"
    sql_issued = fake.connections[1].executed[0][0]
    assert "in_raw_top_n = TRUE" in sql_issued
    assert "ORDER BY p.raw_rank" in sql_issued


def test_load_daily_proposals_no_run_id_returns_empty_view() -> None:
    fake = FakeDbManager([(["run_id"], [])])  # no run found
    loader = DashboardDataLoader(db_manager=fake, db_role="prod")
    view = loader.load_daily_proposals(signal_date=SIGNAL_DATE)
    assert view.rows == []
    assert view.run_id is None
    assert view.signal_date == SIGNAL_DATE


def test_load_daily_proposals_no_dates_returns_empty_view() -> None:
    # latest_signal_date() returns None when no rows.
    fake = FakeDbManager([(["signal_date"], [])])
    loader = DashboardDataLoader(db_manager=fake, db_role="prod")
    view = loader.load_daily_proposals(signal_date=None)
    assert view.rows == []
    assert view.signal_date is None


# --------------------------------------------------------------------------- #
# load_outcome_summary.
# --------------------------------------------------------------------------- #
def test_load_outcome_summary_counts_and_avg() -> None:
    fake = FakeDbManager(
        [(["total", "resolved", "r5", "r10", "r20", "r40"],
          [(10, 7, 1.5, 2.0, 3.0, 4.0)])]
    )
    loader = DashboardDataLoader(db_manager=fake, db_role="prod")
    s = loader.load_outcome_summary()
    assert s.total == 10
    assert s.resolved == 7
    assert s.unresolved == 3
    assert s.avg_return_5bd_pct == 1.5
    assert s.avg_return_40bd_pct == 4.0


def test_load_outcome_summary_empty_returns_zero_summary() -> None:
    fake = FakeDbManager(
        [(["total", "resolved", "r5", "r10", "r20", "r40"],
          [(0, 0, None, None, None, None)])]
    )
    loader = DashboardDataLoader(db_manager=fake, db_role="prod")
    s = loader.load_outcome_summary()
    assert s.total == 0
    assert s.unresolved == 0


# --------------------------------------------------------------------------- #
# latest_pipeline_status.
# --------------------------------------------------------------------------- #
def test_latest_pipeline_status_returns_most_recent_row() -> None:
    cols = [
        "run_id", "run_date", "run_type", "status", "started_at",
        "completed_at", "duration_sec", "steps_completed", "error_message",
    ]
    fake = FakeDbManager(
        [(cols, [(RUN_ID, SIGNAL_DATE, "scheduled", "success",
                  None, None, 1.2, "[]", None)])]
    )
    loader = DashboardDataLoader(db_manager=fake, db_role="prod")
    row = loader.latest_pipeline_status()
    assert row is not None
    assert row["status"] == "success"
    assert row["run_id"] == RUN_ID


def test_latest_pipeline_status_none_when_no_runs() -> None:
    cols = [
        "run_id", "run_date", "run_type", "status", "started_at",
        "completed_at", "duration_sec", "steps_completed", "error_message",
    ]
    fake = FakeDbManager([(cols, [])])
    loader = DashboardDataLoader(db_manager=fake, db_role="prod")
    assert loader.latest_pipeline_status() is None


# --------------------------------------------------------------------------- #
# Pandas Styler test (importorskip pandas).
# --------------------------------------------------------------------------- #
def test_pandas_styler_highlights_only_disagreeing_rows() -> None:
    pd = pytest.importorskip("pandas")

    rows = [
        {
            "raw_rank": 2, "diversified_rank": 1, "ticker": "AAPL",
            "in_raw_top_n": False, "in_diversified_top_n": True,
            "list_disagreement": True, "div_reason": None,
        },
        {
            "raw_rank": 1, "diversified_rank": 2, "ticker": "MSFT",
            "in_raw_top_n": True, "in_diversified_top_n": True,
            "list_disagreement": False, "div_reason": None,
        },
    ]
    view = ProposalsView(rows=rows, show_diversified=True)
    display_rows, flags = build_proposals_display(view)

    df = pd.DataFrame(display_rows)
    styled = df.style.apply(highlight_row, flags=flags, axis=1)
    html = styled.to_html()

    # The amber highlight must appear (from the disagreeing row).
    assert DISAGREEMENT_HIGHLIGHT_CSS in html
    # The total cells with the highlight must be fewer than all <td> cells
    # (the non-disagreeing row contributes empty-string styles, not amber).
    assert html.count(DISAGREEMENT_HIGHLIGHT_CSS) < html.count("<td")


# --------------------------------------------------------------------------- #
# DuckDB-backed integration test (importorskip duckdb).
# --------------------------------------------------------------------------- #
def test_load_daily_proposals_against_real_schema(
    tmp_path: Any, monkeypatch: Any
) -> None:
    duckdb = pytest.importorskip("duckdb")

    from app.config import settings
    from app.database import duckdb_manager as dbm
    from app.database import schema_manager as sm

    prod_path = tmp_path / "prod.duckdb"
    monkeypatch.setattr(settings, "PROD_DB_PATH", prod_path)

    result = sm.apply_schema("prod")
    assert result.is_ok(), f"schema failed: {result.errors}"

    conn = dbm.connect_prod()
    try:
        conn.execute(
            "INSERT INTO ticker_master "
            "(ticker, symbol_type, active_flag, delisted_flag, sector, industry) "
            "VALUES ('AAPL','stock',TRUE,FALSE,'Tech','Hardware')"
        )
        conn.execute(
            "INSERT INTO step5_proposals "
            "(proposal_id, run_id, strategy_config_id, ticker, signal_date, "
            " proposal_score_raw, proposal_score_final, "
            " raw_rank, diversified_rank, "
            " in_raw_top_n, in_diversified_top_n, "
            " diversification_applied, created_at) "
            "VALUES ('p1', ?, 'cfg-1', 'AAPL', ?, "
            " 80.0, 78.0, 2, 1, FALSE, TRUE, TRUE, current_timestamp)",
            [RUN_ID, SIGNAL_DATE],
        )
    finally:
        conn.close()

    loader = DashboardDataLoader(db_role="prod")

    # Diversified (in_diversified_top_n=TRUE) -> should return the row.
    view = loader.load_daily_proposals(signal_date=SIGNAL_DATE, show_diversified=True)
    assert len(view.rows) == 1
    row = view.rows[0]
    assert row["ticker"] == "AAPL"
    assert row["sector"] == "Tech"
    assert row["diversified_rank"] == 1
    assert row["list_disagreement"] is True   # in_raw=False, in_div=True

    # Raw (in_raw_top_n=FALSE) -> should return nothing.
    raw_view = loader.load_daily_proposals(signal_date=SIGNAL_DATE, show_diversified=False)
    assert raw_view.rows == []
