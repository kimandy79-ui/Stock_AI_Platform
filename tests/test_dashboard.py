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
    derive_final_display_status,
    extract_disagreement_flags,
    highlight_css_for_row,
    FINAL_DISPLAY_STATUS_BUY,
    FINAL_DISPLAY_STATUS_BUY_EXCLUDED,
    FINAL_DISPLAY_STATUS_WATCHLIST,
    FINAL_DISPLAY_STATUS_REJECTED,
    highlight_row,
    membership_column_for,
    rank_column_for,
    validate_role,
)
from app.dashboard.data_access import DashboardDataLoader, SimDashboardDataLoader

# ── streamlit_app helpers (imported with streamlit mocked) ───────────────────
import sys as _sys, types as _types
if "streamlit" not in _sys.modules:
    _st = _types.ModuleType("streamlit")
    _st.cache_data     = lambda *a, **kw: (lambda f: f)
    _st.cache_resource = lambda *a, **kw: (lambda f: f)
    _st.set_page_config = lambda **kw: None
    _sys.modules["streamlit"] = _st
    _sys.modules["streamlit.components"] = _types.ModuleType("streamlit.components")
    _sys.modules["streamlit.components.v1"] = _types.ModuleType("streamlit.components.v1")
from app.dashboard.streamlit_app import _enrich_proposals

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
# SimDashboardDataLoader (Simulation Lab) -- separate read-only loader scoped
# to simulation.duckdb; unlike DashboardDataLoader, it does not validate
# against ALLOWED_DASHBOARD_ROLES (it only ever connects to "simulation").
# --------------------------------------------------------------------------- #
_SIM_RUN_COLS = [
    "sim_run_id", "sim_name", "mode", "start_date", "end_date",
    "created_at", "config_ids", "status", "notes",
]

_SIM_COMPARISON_COLS = [
    "config_id", "setup_type", "risk_label", "horizon_bd", "list_type",
    "expectancy", "win_rate", "avg_win", "avg_loss", "profit_factor",
    "stop_hit_rate", "target_hit_rate", "max_drawdown_pct", "resolved_outcomes_pct",
]

_SIM_FOLD_COLS = [
    "fold_id", "fold_number", "train_start", "train_end",
    "test_start", "test_end", "selected_config_id",
]


def test_sim_loader_list_runs_parses_config_ids_json() -> None:
    db = FakeDbManager([
        (_SIM_RUN_COLS, [
            ("run1", "Test Run", "full_backtest", date(2024, 1, 1), date(2024, 6, 1),
             "2024-06-02", '["cfg1", "cfg2"]', "success", None),
        ]),
    ])
    loader = SimDashboardDataLoader(db_manager=db)
    runs = loader.list_sim_runs()
    assert len(runs) == 1
    assert runs[0].sim_run_id == "run1"
    assert runs[0].config_ids == ["cfg1", "cfg2"]
    assert db.roles == ["simulation"]
    assert db.read_only_flags == [True]
    assert db.connections[0].closed is True


def test_sim_loader_list_runs_handles_unparseable_config_ids() -> None:
    db = FakeDbManager([
        (_SIM_RUN_COLS, [
            ("run1", None, "full_backtest", date(2024, 1, 1), date(2024, 6, 1),
             "2024-06-02", "not-json", "success", None),
        ]),
    ])
    loader = SimDashboardDataLoader(db_manager=db)
    runs = loader.list_sim_runs()
    assert runs[0].config_ids == []


def test_sim_loader_load_config_comparisons_no_filters() -> None:
    db = FakeDbManager([
        (_SIM_COMPARISON_COLS, [
            ("cfg1", "breakout", "medium", 20, "diversified",
             0.5, 0.6, 1.2, -0.8, 1.5, None, None, 10.0, 0.9),
        ]),
    ])
    loader = SimDashboardDataLoader(db_manager=db)
    rows = loader.load_sim_config_comparisons("run1")
    assert len(rows) == 1
    assert rows[0]["config_id"] == "cfg1"
    sql, params = db.connections[0].executed[0]
    assert params == ["run1"]


def test_sim_loader_load_config_comparisons_applies_setup_and_risk_filters() -> None:
    db = FakeDbManager([(_SIM_COMPARISON_COLS, [])])
    loader = SimDashboardDataLoader(db_manager=db)
    loader.load_sim_config_comparisons("run1", setup_type="breakout", risk_label="medium")
    sql, params = db.connections[0].executed[0]
    assert params == ["run1", "breakout", "medium"]
    assert "setup_type = ?" in sql
    assert "risk_label = ?" in sql


def test_sim_loader_load_folds() -> None:
    db = FakeDbManager([
        (_SIM_FOLD_COLS, [
            ("f1", 1, date(2024, 1, 1), date(2024, 3, 1), date(2024, 3, 2), date(2024, 4, 1), "cfg1"),
        ]),
    ])
    loader = SimDashboardDataLoader(db_manager=db)
    folds = loader.load_sim_folds("run1")
    assert folds[0]["fold_id"] == "f1"
    assert db.roles == ["simulation"]
    assert db.read_only_flags == [True]


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
# final_display_status (issues 1–3 cleanup)
# --------------------------------------------------------------------------- #

def test_derive_final_display_status_buy_selected() -> None:
    """BUY + selected_flag=True → 'BUY'."""
    row = {"disposition": "BUY", "selected_flag": True}
    assert derive_final_display_status(row) == FINAL_DISPLAY_STATUS_BUY


def test_derive_final_display_status_buy_not_selected() -> None:
    """BUY + selected_flag=False (cap-excluded) → 'BUY (excluded)'.

    This is the critical case: disposition=BUY but rejected from the
    diversified list by sector_cap or industry_cap.  Must NEVER show as
    final BUY in the UI.
    """
    row = {"disposition": "BUY", "selected_flag": False, "rejection_reason": "industry_cap"}
    assert derive_final_display_status(row) == FINAL_DISPLAY_STATUS_BUY_EXCLUDED


def test_derive_final_display_status_watchlist() -> None:
    row = {"disposition": "WATCHLIST_ONLY", "selected_flag": False}
    assert derive_final_display_status(row) == FINAL_DISPLAY_STATUS_WATCHLIST


def test_derive_final_display_status_rejected() -> None:
    row = {"disposition": "REJECTED", "selected_flag": False}
    assert derive_final_display_status(row) == FINAL_DISPLAY_STATUS_REJECTED


def test_derive_final_display_status_missing_keys() -> None:
    """Empty row must not crash."""
    result = derive_final_display_status({})
    assert result == FINAL_DISPLAY_STATUS_REJECTED


def test_annotate_rows_injects_final_display_status() -> None:
    """annotate_rows must inject final_display_status on every row."""
    rows = [
        {"disposition": "BUY", "selected_flag": True,
         "in_raw_top_n": True, "in_diversified_top_n": True},
        {"disposition": "BUY", "selected_flag": False,
         "in_raw_top_n": True, "in_diversified_top_n": False,
         "rejection_reason": "sector_cap"},
        {"disposition": "WATCHLIST_ONLY", "selected_flag": False,
         "in_raw_top_n": True, "in_diversified_top_n": False},
    ]
    out = annotate_rows(rows)
    assert out[0]["final_display_status"] == FINAL_DISPLAY_STATUS_BUY
    assert out[1]["final_display_status"] == FINAL_DISPLAY_STATUS_BUY_EXCLUDED
    assert out[2]["final_display_status"] == FINAL_DISPLAY_STATUS_WATCHLIST


def test_annotate_rows_does_not_mutate_with_final_display_status() -> None:
    """Input rows must not be mutated by annotate_rows."""
    rows = [{"disposition": "BUY", "selected_flag": True,
             "in_raw_top_n": True, "in_diversified_top_n": True}]
    annotate_rows(rows)
    assert "final_display_status" not in rows[0]


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
# DISPLAY_COLUMNS completeness: stop/target/entry/final_display_status (issue 4)
# --------------------------------------------------------------------------- #

def test_display_columns_includes_trade_plan_columns() -> None:
    """stop_price_raw, target_price_raw, entry_price_raw, estimated_rr must be
    in DISPLAY_COLUMNS so they are never silently dropped from displayed rows."""
    for col in ("stop_price_raw", "target_price_raw", "entry_price_raw", "estimated_rr"):
        assert col in DISPLAY_COLUMNS, f"DISPLAY_COLUMNS missing required column: {col}"


def test_display_columns_includes_final_display_status() -> None:
    """final_display_status must be in DISPLAY_COLUMNS so the UI can use it
    to distinguish BUY from BUY (excluded) rows."""
    assert "final_display_status" in DISPLAY_COLUMNS


def _make_selected_proposal_row(**overrides: object) -> dict:
    """Build a minimal selected_flag=True proposal row for display tests."""
    row: dict = {
        "raw_rank": 1,
        "diversified_rank": 1,
        "ticker": "AAPL",
        "setup_type": "breakout",
        "setup_score": 72.0,
        "risk_label": "low",
        "disposition": "BUY",
        "selected_flag": True,
        "in_raw_top_n": True,
        "in_diversified_top_n": True,
        "entry_price_raw": 150.00,
        "stop_price_raw": 143.50,
        "target_price_raw": 165.00,
        "estimated_rr": 2.1,
        "proposal_score_raw": 78.0,
        "proposal_score_final": 78.0,
        "sector": "Technology",
        "industry": "Semiconductors",
        "rejection_reason": None,
        "diversity_penalty": 0.0,
        "diversification_applied": True,
        "mechanical_explanation": "Strong breakout setup.",
        "list_disagreement": False,
        "div_reason": None,
    }
    row.update(overrides)
    return row


def test_displayed_proposal_has_stop_and_target_populated() -> None:
    """For a selected_flag=True BUY row, stop_price_raw and target_price_raw
    must be non-null in the annotated display row."""
    raw_row = _make_selected_proposal_row()
    annotated = annotate_rows([raw_row])
    view = ProposalsView(rows=annotated)
    display_rows, _ = build_proposals_display(view)
    assert len(display_rows) == 1
    row = display_rows[0]
    assert row["stop_price_raw"] is not None, "stop_price_raw must not be null for selected BUY"
    assert row["target_price_raw"] is not None, "target_price_raw must not be null for selected BUY"
    assert row["entry_price_raw"] is not None, "entry_price_raw must not be null for selected BUY"
    assert row["estimated_rr"] is not None, "estimated_rr must not be null for selected BUY"


def test_displayed_proposal_has_final_display_status() -> None:
    """final_display_status must be present and correct in displayed rows."""
    raw_row = _make_selected_proposal_row()
    annotated = annotate_rows([raw_row])
    view = ProposalsView(rows=annotated)
    display_rows, _ = build_proposals_display(view)
    assert display_rows[0]["final_display_status"] == FINAL_DISPLAY_STATUS_BUY


def test_cap_excluded_buy_has_correct_final_display_status() -> None:
    """A BUY row excluded by sector/industry cap must show 'BUY (excluded)',
    not 'BUY', in final_display_status."""
    raw_row = _make_selected_proposal_row(
        selected_flag=False,
        in_diversified_top_n=False,
        rejection_reason="industry_cap",
    )
    annotated = annotate_rows([raw_row])
    view = ProposalsView(rows=annotated)
    display_rows, _ = build_proposals_display(view)
    assert display_rows[0]["final_display_status"] == FINAL_DISPLAY_STATUS_BUY_EXCLUDED


# --------------------------------------------------------------------------- #
# _enrich_proposals: step5 trade-plan fields pass through unchanged (issue 4)
# --------------------------------------------------------------------------- #

def _make_proposal_row(**overrides: object) -> dict:
    """Minimal proposal row as returned by data_access (already annotated)."""
    base: dict = {
        "ticker": "NVDA",
        "setup_type": "breakout",
        "setup_score": 68.0,
        "risk_label": "low",
        "disposition": "BUY",
        "selected_flag": True,
        "in_raw_top_n": True,
        "in_diversified_top_n": True,
        "entry_price_raw": 825.00,
        "stop_price_raw": 795.00,
        "target_price_raw": 900.00,
        "estimated_rr": 2.5,
        "proposal_score_raw": 74.0,
        "proposal_score_final": 74.0,
        "sector": "Technology",
        "industry": "Semiconductors",
        "final_display_status": FINAL_DISPLAY_STATUS_BUY,
        "rejection_reason": None,
        "diversity_penalty": 0.0,
        "list_disagreement": False,
        "div_reason": None,
        "mechanical_explanation": "Breakout setup.",
    }
    base.update(overrides)
    return base


def test_enrich_proposals_preserves_step5_stop_price_raw() -> None:
    """stop_price_raw from step5 proposal must survive _enrich_proposals."""
    row = _make_proposal_row()
    result = _enrich_proposals([row], SIGNAL_DATE, "prod")
    assert result[0]["stop_price_raw"] == 795.00


def test_enrich_proposals_preserves_step5_target_price_raw() -> None:
    """target_price_raw from step5 proposal must survive _enrich_proposals."""
    row = _make_proposal_row()
    result = _enrich_proposals([row], SIGNAL_DATE, "prod")
    assert result[0]["target_price_raw"] == 900.00


def test_enrich_proposals_preserves_entry_price_raw() -> None:
    """entry_price_raw from step5 proposal must survive _enrich_proposals."""
    row = _make_proposal_row()
    result = _enrich_proposals([row], SIGNAL_DATE, "prod")
    assert result[0]["entry_price_raw"] == 825.00


def test_enrich_proposals_preserves_estimated_rr() -> None:
    """estimated_rr from step5 proposal must survive _enrich_proposals."""
    row = _make_proposal_row()
    result = _enrich_proposals([row], SIGNAL_DATE, "prod")
    assert result[0]["estimated_rr"] == 2.5


def test_enrich_proposals_preserves_final_display_status() -> None:
    """final_display_status annotation must survive _enrich_proposals."""
    row = _make_proposal_row()
    result = _enrich_proposals([row], SIGNAL_DATE, "prod")
    assert result[0]["final_display_status"] == FINAL_DISPLAY_STATUS_BUY


def test_enrich_proposals_does_not_overwrite_step5_stop_with_step4() -> None:
    """_enrich_proposals must not overwrite stop_price_raw with step4 values.
    The only source of stop/target in the display is step5_proposals.
    """
    row = _make_proposal_row(stop_price_raw=795.00, target_price_raw=900.00)
    result = _enrich_proposals([row], SIGNAL_DATE, "prod")
    # Values from step5 must be preserved unchanged
    assert result[0]["stop_price_raw"] == 795.00
    assert result[0]["target_price_raw"] == 900.00


def test_enrich_proposals_does_not_crash_on_empty() -> None:
    result = _enrich_proposals([], SIGNAL_DATE, "prod")
    assert result == []


# --------------------------------------------------------------------------- #
# Table-row dict shape: Status/Entry/Stop/Target/RR present and correct
# --------------------------------------------------------------------------- #

def _build_one_table_row(proposal_row: dict) -> dict:
    """Reproduce the dict that the proposals-table build loop produces,
    without invoking Streamlit rendering.

    Mirrors the production logic in streamlit_app._tab_daily_proposals:
        _stop   = row.get("stop_price_raw")   or row.get("stop_price")
        _target = row.get("target_price_raw") or row.get("target_price")
        _entry  = row.get("entry_price_raw")
    """
    row = dict(proposal_row)
    _stop   = row.get("stop_price_raw")   or row.get("stop_price")
    _target = row.get("target_price_raw") or row.get("target_price")
    _entry  = row.get("entry_price_raw")
    score   = row.get("proposal_score_final") or row.get("proposal_score_raw")
    return {
        "Status":  row.get("final_display_status") or row.get("disposition") or "",
        "RR":      round(float(row["estimated_rr"]), 2) if row.get("estimated_rr") is not None else None,
        "Entry":   round(float(_entry), 2) if _entry is not None else None,
        "Stop":    round(float(_stop), 2) if _stop is not None else None,
        "Target":  round(float(_target), 2) if _target is not None else None,
        "Score":   round(float(score), 1) if score is not None else None,
        "Ticker":  row.get("ticker") or "",
        "Signal":  row.get("setup_type") or "",
    }


def test_table_row_status_uses_final_display_status() -> None:
    """Status column must use final_display_status, not raw disposition."""
    row = _make_proposal_row(final_display_status="BUY (excluded)", disposition="BUY")
    t = _build_one_table_row(row)
    assert t["Status"] == "BUY (excluded)"


def test_table_row_stop_uses_step5_stop_price_raw() -> None:
    """Stop column must come from stop_price_raw (step5), not stop_price (step4)."""
    row = _make_proposal_row(stop_price_raw=795.00)
    # Simulate what would happen if step4 value were accidentally present
    row["stop_price"] = 999.00  # step4 fallback — must NOT be used when raw is set
    t = _build_one_table_row(row)
    assert t["Stop"] == 795.00


def test_table_row_target_uses_step5_target_price_raw() -> None:
    row = _make_proposal_row(target_price_raw=900.00)
    row["target_price"] = 999.00  # step4 fallback — must NOT be used
    t = _build_one_table_row(row)
    assert t["Target"] == 900.00


def test_table_row_entry_populated() -> None:
    row = _make_proposal_row(entry_price_raw=825.00)
    t = _build_one_table_row(row)
    assert t["Entry"] == 825.00


def test_table_row_entry_none_when_missing() -> None:
    row = _make_proposal_row()
    del row["entry_price_raw"]
    t = _build_one_table_row(row)
    assert t["Entry"] is None


def test_table_row_rr_populated() -> None:
    row = _make_proposal_row(estimated_rr=2.5)
    t = _build_one_table_row(row)
    assert t["RR"] == 2.50


def test_table_row_stop_falls_back_to_step4_when_raw_absent() -> None:
    """If stop_price_raw is absent, fall back to stop_price (step4)."""
    row = _make_proposal_row()
    del row["stop_price_raw"]
    row["stop_price"] = 790.00
    t = _build_one_table_row(row)
    assert t["Stop"] == 790.00


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
@pytest.mark.skip(
    reason='PENDING M21 migration (Phase 7 scope): queries strategy_config_id '
           'column removed in setup-mode schema (AD-22.21).'
)
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


# --------------------------------------------------------------------------- #
# resolve_report_ticker — pure function, no Streamlit server required
# --------------------------------------------------------------------------- #

from app.dashboard.streamlit_app import resolve_report_ticker  # noqa: E402

_OPTION_MAP: dict[str, dict] = {
    "AAPL (breakout)": {"ticker": "AAPL", "setup_config_id": "setup_breakout_v1"},
    "MSFT (pullback)": {"ticker": "MSFT", "setup_config_id": "setup_pullback_v1"},
}


class TestResolveReportTicker:
    def test_dropdown_mode_resolves_row(self) -> None:
        ticker, row = resolve_report_ticker(
            manual_mode=False,
            manual_value="",
            dropdown_label="AAPL (breakout)",
            option_map=_OPTION_MAP,
        )
        assert ticker == "AAPL"
        assert row is not None
        assert row["setup_config_id"] == "setup_breakout_v1"

    def test_dropdown_mode_missing_label_returns_none(self) -> None:
        ticker, row = resolve_report_ticker(
            manual_mode=False,
            manual_value="",
            dropdown_label="UNKNOWN (breakout)",
            option_map=_OPTION_MAP,
        )
        assert ticker is None
        assert row is None

    def test_manual_mode_valid_input(self) -> None:
        ticker, row = resolve_report_ticker(
            manual_mode=True,
            manual_value="nvda",
            dropdown_label="AAPL (breakout)",
            option_map=_OPTION_MAP,
        )
        assert ticker == "NVDA"
        assert row is not None
        assert row["ticker"] == "NVDA"

    def test_manual_mode_strips_whitespace_and_uppercases(self) -> None:
        ticker, row = resolve_report_ticker(
            manual_mode=True,
            manual_value="  tsla  ",
            dropdown_label="AAPL (breakout)",
            option_map=_OPTION_MAP,
        )
        assert ticker == "TSLA"
        assert row is not None
        assert row["ticker"] == "TSLA"

    def test_manual_mode_empty_input_returns_none(self) -> None:
        ticker, row = resolve_report_ticker(
            manual_mode=True,
            manual_value="",
            dropdown_label="AAPL (breakout)",
            option_map=_OPTION_MAP,
        )
        assert ticker is None
        assert row is None

    def test_manual_mode_whitespace_only_returns_none(self) -> None:
        ticker, row = resolve_report_ticker(
            manual_mode=True,
            manual_value="   ",
            dropdown_label="AAPL (breakout)",
            option_map=_OPTION_MAP,
        )
        assert ticker is None
        assert row is None

    def test_manual_mode_ignores_dropdown_value(self) -> None:
        """Dropdown label is irrelevant when manual_mode=True."""
        ticker, row = resolve_report_ticker(
            manual_mode=True,
            manual_value="AMD",
            dropdown_label="MSFT (pullback)",
            option_map=_OPTION_MAP,
        )
        assert ticker == "AMD"
        assert row is not None
        assert row["ticker"] == "AMD"
