"""Tests for Module 17 — Simulation Engine.

The suite is split into two layers so it runs fully offline:

* **Pure / fake-connection layer** — exercises validation, ``run_id``
  mint/preserve, the walk-forward fold plan, metric maths, config selection,
  outcome construction (list membership, returns, MFE/MAE, realized-R,
  cross-fold flagging), config-comparison aggregation, and static-source scans.
  These need neither ``duckdb`` nor ``polars`` and always run.
* **Integration layer** — end-to-end runs against real tmp ``prod`` /
  ``simulation`` DuckDB files with the real Module 03 schema, the frozen
  Step 3/4/5 scoring (Polars) and a fake NYSE calendar. Skipped automatically
  when ``duckdb`` / ``polars`` are not installed.

No test touches the real prod / debug / simulation DB files; the ``tmp_db_paths``
fixture redirects every settings path into ``tmp_path``. A ``FakeCalendar``
(Mon–Fri sessions) is injected over ``simulation_engine._default_calendar`` so
``pandas_market_calendars`` is never imported.
"""

from __future__ import annotations

import ast
import uuid
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.services.simulation import simulation_engine as se
from app.services.simulation.simulation_engine import (
    SimulationEngine,
    compute_metrics,
    plan_walk_forward_folds,
    select_config_for_fold,
)
from app.utils import service_result

ENGINE_SRC = Path(se.__file__).read_text(encoding="utf-8")

CONFIG_ID = "cfg-1"
START = date(2024, 1, 2)
END = date(2024, 1, 31)


# --------------------------------------------------------------------------- #
# Fake NYSE calendar (Mon-Fri sessions; no holidays needed).
# --------------------------------------------------------------------------- #
def _weekday_sessions(start: date, count: int) -> list[date]:
    out: list[date] = []
    day = start
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


SESSIONS: list[date] = _weekday_sessions(date(2024, 1, 2), 260)


class FakeCalendar:
    def __init__(self, sessions: list[date]) -> None:
        self._sessions = sorted(sessions)
        self._index = {d: i for i, d in enumerate(self._sessions)}

    def next_trading_day(self, day: date) -> date:
        for s in self._sessions:
            if s > day:
                return s
        raise AssertionError("session list exhausted")

    def add_trading_days(self, day: date, n: int) -> date:
        return self._sessions[self._index[day] + n]

    def trading_days_between(self, start: date, end: date) -> list[date]:
        return [s for s in self._sessions if start <= s <= end]


@pytest.fixture(autouse=True)
def fake_calendar(monkeypatch: pytest.MonkeyPatch) -> FakeCalendar:
    cal = FakeCalendar(SESSIONS)
    monkeypatch.setattr(se, "_default_calendar", lambda: cal)
    return cal


def make_config(
    *,
    hard_cap: bool = False,
    top_n: int = 5,
    slippage_bps: float = 10.0,
) -> dict:
    """A complete strategy config satisfying the frozen Step 3/4/5 parsers."""
    div = {"hard_cap_enabled": hard_cap, "top_n": top_n}
    if hard_cap:
        div["max_sector_count"] = 2
        div["max_industry_count"] = 1
    else:
        div["sector_penalty"] = 0.8
        div["industry_penalty"] = 0.7
    return {
        "universe": {"min_price": 5.0, "min_avg_dollar_volume_20d": 1_000_000.0},
        "screening": {"min_rvol": 1.0},
        "scoring_weights": {
            "trend": 0.25,
            "momentum": 0.25,
            "setup": 0.2,
            "volume": 0.15,
            "market": 0.15,
        },
        "step4": {"target_R": 2.0},
        "earnings": {"avoid_within_bd": 5, "penalty_points_max": -10.0},
        "macro_event_risk": {"enabled": False, "penalty_points": -5.0},
        "diversification": div,
        "simulation": {"slippage_bps": slippage_bps},
    }


# --------------------------------------------------------------------------- #
# Fake connection for pure-method tests (no duckdb).
# --------------------------------------------------------------------------- #
class FakeCursor:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    """Routes the engine's outcome/aggregate SQL to canned data; records writes."""

    def __init__(
        self,
        opens: dict[tuple[str, date], float] | None = None,
        closes: dict[tuple[str, date], float] | None = None,
        candles: dict[str, dict[date, tuple[float, float]]] | None = None,
    ) -> None:
        self.opens = opens or {}
        self.closes = closes or {}
        self.candles = candles or {}
        self.inserts: dict[str, list[list]] = {}

    def execute(self, sql: str, params: list | None = None):
        params = params or []
        if sql.startswith("INSERT INTO "):
            table = sql.split("INSERT INTO ", 1)[1].split(" ", 1)[0]
            self.inserts.setdefault(table, []).append(params)
            return FakeCursor([])
        if "open_raw" in sql and "BETWEEN" not in sql:
            v = self.opens.get((params[0], params[1]))
            return FakeCursor([(v,)] if v is not None else [(None,)])
        if "close_adj" in sql and "BETWEEN" not in sql:
            v = self.closes.get((params[0], params[1]))
            return FakeCursor([(v,)] if v is not None else [(None,)])
        if "BETWEEN" in sql:  # window candles
            ticker, start, end = params
            rows = [
                (d, hl[0], hl[1])
                for d, hl in sorted(self.candles.get(ticker, {}).items())
                if start <= d <= end
            ]
            return FakeCursor(rows)
        return FakeCursor([])


# --------------------------------------------------------------------------- #
# Pure helpers: fold plan / metrics / selection.
# --------------------------------------------------------------------------- #
def test_constants_match_duckdb_manager() -> None:
    pytest.importorskip("duckdb")
    from app.database import duckdb_manager as dbm

    assert se.DB_ROLE_SIMULATION == dbm.DB_ROLE_SIMULATION
    assert se.PROD_ALIAS == dbm.DEFAULT_PROD_ALIAS


def test_plan_walk_forward_folds_expanding_quarterly() -> None:
    folds = plan_walk_forward_folds(date(2022, 1, 3), date(2023, 12, 31))
    # First test quarter begins at the quarter after the 12-month mark.
    assert folds[0]["test_start"] == date(2023, 4, 1)
    assert folds[0]["train_start"] == date(2022, 1, 3)
    assert folds[0]["train_end"] == date(2023, 3, 31)
    # Quarters are contiguous and the last test_end is clamped to end_date.
    assert all(
        folds[i + 1]["test_start"] > folds[i]["test_end"]
        for i in range(len(folds) - 1)
    )
    assert folds[-1]["test_end"] == date(2023, 12, 31)
    # Expanding window: each train_end is the day before its test_start.
    for f in folds:
        assert f["train_end"] == f["test_start"] - timedelta(days=1)


def test_plan_walk_forward_folds_short_window_empty() -> None:
    assert plan_walk_forward_folds(date(2024, 1, 2), date(2024, 1, 31)) == []


def test_compute_metrics_resolved_only_and_denominator() -> None:
    # 2 of 4 resolved -> resolved_outcomes_pct counts unresolved in denominator.
    m = compute_metrics([0.10, None, -0.05, None])
    assert m["resolved_outcomes_pct"] == pytest.approx(0.5)
    assert m["win_rate"] == pytest.approx(0.5)  # one win, one loss of two resolved
    assert m["avg_win"] == pytest.approx(0.10)
    assert m["avg_loss"] == pytest.approx(-0.05)
    assert m["expectancy"] == pytest.approx((0.10 - 0.05) / 2)
    assert m["profit_factor"] == pytest.approx(0.10 / 0.05)
    assert m["max_drawdown_pct"] is not None and m["max_drawdown_pct"] > 0


def test_compute_metrics_all_unresolved() -> None:
    m = compute_metrics([None, None])
    assert m["resolved_outcomes_pct"] == pytest.approx(0.0)
    for k in ("expectancy", "win_rate", "avg_win", "avg_loss", "profit_factor", "max_drawdown_pct"):
        assert m[k] is None


def test_compute_metrics_empty_group() -> None:
    m = compute_metrics([])
    assert m["resolved_outcomes_pct"] is None
    assert m["expectancy"] is None


def test_select_config_threshold_and_max_expectancy() -> None:
    metrics = {
        "a": {"resolved_outcomes_pct": 0.90, "max_drawdown_pct": 10.0, "expectancy": 0.05},
        "b": {"resolved_outcomes_pct": 0.90, "max_drawdown_pct": 10.0, "expectancy": 0.08},
        "c": {"resolved_outcomes_pct": 0.50, "max_drawdown_pct": 5.0, "expectancy": 0.20},   # fails resolved
        "d": {"resolved_outcomes_pct": 0.99, "max_drawdown_pct": 40.0, "expectancy": 0.99},  # fails drawdown
    }
    assert select_config_for_fold(metrics) == "b"


def test_select_config_none_eligible() -> None:
    metrics = {"a": {"resolved_outcomes_pct": 0.10, "max_drawdown_pct": 99.0, "expectancy": 1.0}}
    assert select_config_for_fold(metrics) is None


# --------------------------------------------------------------------------- #
# Validation (pre-DB) + run_id behavior + metadata shape.
# --------------------------------------------------------------------------- #
def _run(engine: SimulationEngine, **over):
    kwargs = dict(
        sim_name="s",
        mode="research",
        start_date=START,
        end_date=END,
        config_ids=[CONFIG_ID],
        strategy_configs={CONFIG_ID: make_config()},
        db_role="simulation",
    )
    kwargs.update(over)
    return engine.run(**kwargs)


@pytest.mark.parametrize(
    "over, needle",
    [
        ({"db_role": "prod"}, "db_role"),
        ({"db_role": "debug"}, "db_role"),
        ({"mode": "bogus"}, "mode"),
        ({"config_ids": []}, "non-empty"),
        ({"config_ids": ["missing"]}, "strategy_configs"),
        ({"start_date": date(2024, 2, 1), "end_date": date(2024, 1, 1)}, "after"),
    ],
)
def test_pre_db_validation_failures(over, needle) -> None:
    # A db_manager whose use would raise proves no DB access happens pre-validation.
    class Boom:
        def connect_simulation_with_prod(self, *a, **k):
            raise AssertionError("DB accessed before validation")

    res = _run(SimulationEngine(Boom()), **over)
    assert res.status == service_result.STATUS_FAILED
    assert any(needle in e for e in res.errors)
    assert frozenset(res.metadata) == frozenset(se.RUN_METADATA_KEYS)


def test_run_id_minted_when_none() -> None:
    res = _run(SimulationEngine(), db_role="prod")  # fails validation, but mints id
    uuid.UUID(res.run_id)  # parses as a valid uuid


def test_run_id_preserved_when_supplied() -> None:
    rid = "fixed-run-id-123"
    res = _run(SimulationEngine(), db_role="prod", run_id=rid)
    assert res.run_id == rid
    assert res.metadata["run_id"] == rid


# --------------------------------------------------------------------------- #
# Outcome construction (fake connection): membership, returns, cross-fold.
# --------------------------------------------------------------------------- #
def _proposal(ticker: str, in_raw: bool, in_div: bool, pid: str | None = None) -> dict:
    return {
        "proposal_id": pid or f"p-{ticker}",
        "ticker": ticker,
        "in_raw_top_n": in_raw,
        "in_diversified_top_n": in_div,
    }


def test_build_outcomes_list_membership_and_no_outcome_outside_lists() -> None:
    cal = FakeCalendar(SESSIONS)
    sim_date = SESSIONS[0]
    entry = SESSIONS[1]
    opens = {("RAW", entry): 100.0, ("DIV", entry): 100.0, ("BOTH", entry): 100.0}
    closes = {}
    for n in (5, 10, 20, 40):
        ev = SESSIONS[1 + n]
        for tk in ("RAW", "DIV", "BOTH"):
            closes[(tk, ev)] = 110.0
    candles = {
        tk: {d: (120.0, 95.0) for d in cal.trading_days_between(entry, SESSIONS[1 + 40])}
        for tk in ("RAW", "DIV", "BOTH")
    }
    conn = FakeConn(opens, closes, candles)
    eng = SimulationEngine()

    proposals = [
        _proposal("RAW", True, False),
        _proposal("DIV", False, True),
        _proposal("BOTH", True, True),
        _proposal("NONE", False, False),  # must NOT produce an outcome
    ]
    outs = eng._build_outcomes(
        conn, cal, run_id="r", fold=None, fold_id=None, config_id=CONFIG_ID,
        sim_date=sim_date, slippage_bps=10.0, proposals=proposals,
        stop_by_ticker={"RAW": 90.0, "DIV": 90.0, "BOTH": 90.0, "NONE": 90.0},
    )
    by_ticker = {o["ticker"]: o for o in outs}
    assert set(by_ticker) == {"RAW", "DIV", "BOTH"}  # NONE excluded
    assert by_ticker["RAW"]["list_membership"] == se.LIST_RAW_ONLY
    assert by_ticker["DIV"]["list_membership"] == se.LIST_DIVERSIFIED_ONLY
    assert by_ticker["BOTH"]["list_membership"] == se.LIST_BOTH


def test_build_outcomes_entry_price_sim_returns_and_realized_r() -> None:
    cal = FakeCalendar(SESSIONS)
    sim_date = SESSIONS[0]
    entry = SESSIONS[1]
    opens = {("T", entry): 100.0}
    closes = {("T", SESSIONS[1 + n]): 110.0 for n in (5, 10, 20, 40)}
    candles = {"T": {d: (130.0, 80.0) for d in cal.trading_days_between(entry, SESSIONS[1 + 40])}}
    conn = FakeConn(opens, closes, candles)
    eng = SimulationEngine()
    out = eng._build_outcomes(
        conn, cal, run_id="r", fold=None, fold_id=None, config_id=CONFIG_ID,
        sim_date=sim_date, slippage_bps=10.0,
        proposals=[_proposal("T", True, True)], stop_by_ticker={"T": 90.0},
    )[0]
    sim_price = 100.0 * (1 + 10.0 / 10000.0)
    assert out["entry_price_raw"] == pytest.approx(100.0)
    assert out["entry_price_sim"] == pytest.approx(sim_price)
    assert out["return_40bd_pct"] == pytest.approx(110.0 / sim_price - 1.0)
    assert out["mfe_40bd_pct"] == pytest.approx(130.0 / sim_price - 1.0)
    assert out["mae_40bd_pct"] == pytest.approx(80.0 / sim_price - 1.0)
    assert out["realized_r_multiple"] == pytest.approx((110.0 - sim_price) / (sim_price - 90.0))
    assert out["outcome_status"] == se.OUTCOME_COMPLETE


def test_build_outcomes_missing_entry_price_skips_row() -> None:
    cal = FakeCalendar(SESSIONS)
    conn = FakeConn(opens={}, closes={}, candles={})  # no entry open available
    eng = SimulationEngine()
    outs = eng._build_outcomes(
        conn, cal, run_id="r", fold=None, fold_id=None, config_id=CONFIG_ID,
        sim_date=SESSIONS[0], slippage_bps=10.0,
        proposals=[_proposal("T", True, True)], stop_by_ticker={"T": 90.0},
    )
    assert outs == []


def test_build_outcomes_unresolved_partial_status() -> None:
    cal = FakeCalendar(SESSIONS)
    sim_date = SESSIONS[0]
    entry = SESSIONS[1]
    opens = {("T", entry): 100.0}
    closes = {("T", SESSIONS[1 + 5]): 105.0}  # only 5bd resolved
    candles = {"T": {}}
    conn = FakeConn(opens, closes, candles)
    eng = SimulationEngine()
    out = eng._build_outcomes(
        conn, cal, run_id="r", fold=None, fold_id=None, config_id=CONFIG_ID,
        sim_date=sim_date, slippage_bps=0.0,
        proposals=[_proposal("T", True, True)], stop_by_ticker={"T": 90.0},
    )[0]
    assert out["return_5bd_pct"] is not None
    assert out["return_40bd_pct"] is None
    assert out["outcome_status"] == se.OUTCOME_PARTIAL
    assert out["mfe_40bd_pct"] is None  # incomplete candle window -> None


def test_build_outcomes_cross_fold_flag_on_spill() -> None:
    cal = FakeCalendar(SESSIONS)
    sim_date = SESSIONS[0]
    entry = SESSIONS[1]
    eval_40 = SESSIONS[1 + 40]
    opens = {("T", entry): 100.0}
    closes = {("T", SESSIONS[1 + n]): 101.0 for n in (5, 10, 20, 40)}
    candles = {"T": {d: (101.0, 99.0) for d in cal.trading_days_between(entry, eval_40)}}
    conn = FakeConn(opens, closes, candles)
    eng = SimulationEngine()

    # Fold whose test period ends before the 40bd eval -> cross-fold spill.
    fold_spill = {"fold_number": 1, "test_start": sim_date, "test_end": eval_40 - timedelta(days=1)}
    out = eng._build_outcomes(
        conn, cal, run_id="r", fold=fold_spill, fold_id="f1", config_id=CONFIG_ID,
        sim_date=sim_date, slippage_bps=0.0,
        proposals=[_proposal("T", True, True)], stop_by_ticker={"T": 90.0},
    )[0]
    assert out["cross_fold_outcome"] is True

    # Fold whose test period fully contains the eval -> no spill.
    fold_ok = {"fold_number": 1, "test_start": sim_date, "test_end": eval_40 + timedelta(days=1)}
    out2 = eng._build_outcomes(
        conn, cal, run_id="r", fold=fold_ok, fold_id="f1", config_id=CONFIG_ID,
        sim_date=sim_date, slippage_bps=0.0,
        proposals=[_proposal("T", True, True)], stop_by_ticker={"T": 90.0},
    )[0]
    assert out2["cross_fold_outcome"] is False


# --------------------------------------------------------------------------- #
# Config comparisons (fake connection): raw/diversified rows + metrics.
# --------------------------------------------------------------------------- #
def _outcome(config_id: str, signal: date, membership: str, r: float | None, cross: bool = False) -> dict:
    return {
        "strategy_config_id": config_id,
        "signal_date": signal,
        "list_membership": membership,
        "cross_fold_outcome": cross,
        "return_5bd_pct": r,
        "return_10bd_pct": r,
        "return_20bd_pct": r,
        "return_40bd_pct": r,
    }


def test_write_comparisons_raw_and_diversified_rows_and_metrics() -> None:
    eng = SimulationEngine()
    conn = FakeConn()
    outcomes = [
        _outcome(CONFIG_ID, SESSIONS[0], se.LIST_BOTH, 0.10),
        _outcome(CONFIG_ID, SESSIONS[1], se.LIST_RAW_ONLY, -0.05),
        _outcome(CONFIG_ID, SESSIONS[2], se.LIST_DIVERSIFIED_ONLY, 0.20),
    ]
    written = eng._write_comparisons(conn, "r", se.MODE_RESEARCH, [CONFIG_ID], outcomes)
    # 1 config * 4 horizons * 2 list_types
    assert written == 8
    rows = conn.inserts["sim_config_comparisons"]
    assert len(rows) == 8
    # Group rows by (list_type, horizon). Params order ends with list_type.
    raw_rows = [r for r in rows if r[-1] == se.LIST_TYPE_RAW]
    div_rows = [r for r in rows if r[-1] == se.LIST_TYPE_DIVERSIFIED]
    assert len(raw_rows) == 4 and len(div_rows) == 4
    # Raw list = raw_only + both => returns {0.10, -0.05}; win_rate index in params:
    # [comp_id, run, config, horizon, expectancy, win_rate, avg_win, avg_loss,
    #  profit_factor, max_dd, resolved_pct, list_type]
    raw5 = next(r for r in raw_rows if r[3] == 5)
    assert raw5[5] == pytest.approx(0.5)            # win_rate
    assert raw5[10] == pytest.approx(1.0)           # resolved_outcomes_pct
    div5 = next(r for r in div_rows if r[3] == 5)   # div = diversified_only + both => {0.20, 0.10}
    assert div5[5] == pytest.approx(1.0)            # both positive
    assert div5[4] == pytest.approx(0.15)           # expectancy mean


def test_write_comparisons_walk_forward_excludes_cross_fold() -> None:
    eng = SimulationEngine()
    conn = FakeConn()
    outcomes = [
        _outcome(CONFIG_ID, SESSIONS[0], se.LIST_BOTH, 0.10, cross=False),
        _outcome(CONFIG_ID, SESSIONS[1], se.LIST_BOTH, 0.50, cross=True),  # excluded
    ]
    eng._write_comparisons(conn, "r", se.MODE_WALK_FORWARD, [CONFIG_ID], outcomes)
    raw5 = next(r for r in conn.inserts["sim_config_comparisons"]
                if r[-1] == se.LIST_TYPE_RAW and r[3] == 5)
    assert raw5[4] == pytest.approx(0.10)  # only the non-cross-fold outcome counts


# --------------------------------------------------------------------------- #
# Walk-forward fold writing + per-fold training metrics / selection.
# --------------------------------------------------------------------------- #
def test_write_folds_selects_config_and_inserts_rows() -> None:
    eng = SimulationEngine()
    conn = FakeConn()
    folds = [{"fold_number": 1, "train_start": date(2024, 1, 1), "train_end": date(2024, 6, 30),
              "test_start": date(2024, 7, 1), "test_end": date(2024, 9, 30)}]
    fold_ids = {1: "fold-uuid-1"}
    # 'good' config: many resolved positive train outcomes; 'bad': mostly unresolved.
    outcomes: list[dict] = []
    for i in range(20):
        outcomes.append(_outcome("good", date(2024, 1, 2) + timedelta(days=i), se.LIST_BOTH, 0.02))
    for i in range(20):
        outcomes.append(_outcome("bad", date(2024, 1, 2) + timedelta(days=i), se.LIST_BOTH, None))
    eng._write_folds(conn, "r", folds, fold_ids, outcomes)
    row = conn.inserts["sim_folds"][0]
    # params: [fold_id, run, fold_number, train_start, train_end, test_start, test_end, selected]
    assert row[0] == "fold-uuid-1"
    assert row[2] == 1
    assert row[7] == "good"  # 'bad' fails the resolved-outcomes threshold


def test_fold_train_metrics_excludes_cross_fold_and_out_of_window() -> None:
    eng = SimulationEngine()
    fold = {"fold_number": 1, "train_start": date(2024, 1, 1), "train_end": date(2024, 3, 31),
            "test_start": date(2024, 4, 1), "test_end": date(2024, 6, 30)}
    outcomes = [
        _outcome("c", date(2024, 2, 1), se.LIST_BOTH, 0.10),          # in window, counts
        _outcome("c", date(2024, 2, 2), se.LIST_BOTH, 0.20, cross=True),  # cross-fold, excluded
        _outcome("c", date(2024, 5, 1), se.LIST_BOTH, 0.30),          # outside train window
        _outcome("c", date(2024, 2, 3), se.LIST_RAW_ONLY, 0.40),      # raw-only, not diversified list
    ]
    metrics = eng._fold_train_metrics(fold, outcomes)
    assert set(metrics) == {"c"}
    assert metrics["c"]["resolved_outcomes_pct"] == pytest.approx(1.0)
    assert metrics["c"]["expectancy"] == pytest.approx(0.10)  # only the single in-window diversified row


# --------------------------------------------------------------------------- #
# Static source scans (no execution required).
# --------------------------------------------------------------------------- #
PROD_PREFIX = se.PROD_ALIAS + "."


def _sql_literals() -> list[str]:
    """All resolved SQL strings (``_SELECT`` / ``_INSERT`` / ``_UPDATE`` consts).

    The constants are f-strings, so they are read from the imported module's
    namespace (already rendered) rather than from the AST.
    """
    out: list[str] = []
    for name, value in vars(se).items():
        if not isinstance(value, str):
            continue
        if name.startswith(("_SELECT", "_INSERT", "_UPDATE")):
            out.append(value)
    return out


def test_no_direct_duckdb_import() -> None:
    tree = ast.parse(ENGINE_SRC)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(a.name != "duckdb" for a in node.names)
        if isinstance(node, ast.ImportFrom):
            assert node.module != "duckdb"


def test_no_provider_imports() -> None:
    tree = ast.parse(ENGINE_SRC)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert "providers" not in node.module
        if isinstance(node, ast.Import):
            assert all("providers" not in a.name for a in node.names)


def test_no_print_calls() -> None:
    tree = ast.parse(ENGINE_SRC)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id != "print"


def test_no_production_ddl_or_attach_in_sql() -> None:
    for sql in _sql_literals():
        up = sql.upper()
        for banned in ("CREATE TABLE", "DROP TABLE", "ALTER TABLE", "CREATE INDEX", "ATTACH "):
            assert banned not in up, sql


def test_writes_only_sim_tables() -> None:
    for sql in _sql_literals():
        up = sql.strip().upper()
        if up.startswith("INSERT INTO "):
            target = up.split("INSERT INTO ", 1)[1].split(" ", 1)[0].split("\n", 1)[0]
            assert target.startswith("SIM_"), sql
        if up.startswith("UPDATE "):
            target = up.split("UPDATE ", 1)[1].split(" ", 1)[0]
            assert target.startswith("SIM_"), sql


def test_prod_reads_are_alias_qualified() -> None:
    # Every reference to a production table in SQL is qualified with the prod
    # alias (the f-strings render ``{PROD_ALIAS}.<table>`` -> ``prod.<table>``).
    for sql in _sql_literals():
        for tbl in ("daily_features", "daily_prices", "ticker_master"):
            idx = 0
            while True:
                idx = sql.find(tbl, idx)
                if idx == -1:
                    break
                prefix = sql[max(0, idx - len(PROD_PREFIX)):idx]
                assert prefix.endswith(PROD_PREFIX), f"unqualified {tbl} in: {sql}"
                idx += len(tbl)


# --------------------------------------------------------------------------- #
# SQL parameter-binding regression (offline — no duckdb / polars needed).
# --------------------------------------------------------------------------- #
def test_sql_placeholder_counts() -> None:
    """_SELECT_FEATURES_PRICES and _SELECT_PRIOR_10 each have exactly 4 ?."""
    assert se._SELECT_FEATURES_PRICES.count("?") == 4, (
        "_SELECT_FEATURES_PRICES must have 4 placeholders "
        f"(got {se._SELECT_FEATURES_PRICES.count('?')})"
    )
    assert se._SELECT_PRIOR_10.count("?") == 4, (
        "_SELECT_PRIOR_10 must have 4 placeholders "
        f"(got {se._SELECT_PRIOR_10.count('?')})"
    )


def test_step4_sql_param_binding_offline() -> None:
    """_read_step4_inputs passes the correct number and order of params to DB."""
    # Inject a fake step4 module (no duckdb/polars import) and a recording conn.
    class _FakeStep4Mod:
        @staticmethod
        def _trend_resume_history_ok(rows: list) -> bool:
            return False

    class _RecordConn:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def execute(self, sql: str, params=None):
            self.calls.append({"sql": sql, "params": list(params) if params is not None else None})
            return FakeCursor([])

    eng = SimulationEngine()
    eng._engines_cache = {  # type: ignore[assignment]
        "step4": _FakeStep4Mod(),
        "step3": None, "eng3": None, "eng4": None, "eng5": None, "oq": None,
    }
    conn = _RecordConn()
    ticker = "XYZ"
    sim_date = SESSIONS[5]
    feat_version = "v1"

    eng._read_step4_inputs(conn, [{"ticker": ticker}], sim_date, feat_version)

    # Match calls by exact SQL content (most reliable; avoids ambiguous substring matches).
    fp_calls = [c for c in conn.calls if c["sql"].strip() == se._SELECT_FEATURES_PRICES.strip()]
    prior_calls = [c for c in conn.calls if c["sql"].strip() == se._SELECT_PRIOR_10.strip()]

    assert len(fp_calls) == 1, f"expected 1 features-prices call, got {len(fp_calls)}"
    assert fp_calls[0]["params"] == [sim_date, sim_date, sim_date, feat_version], (
        f"Wrong _SELECT_FEATURES_PRICES params: {fp_calls[0]['params']}"
    )
    assert len(prior_calls) == 1, f"expected 1 prior-10 call, got {len(prior_calls)}"
    assert prior_calls[0]["params"] == [sim_date, feat_version, ticker, sim_date], (
        f"Wrong _SELECT_PRIOR_10 params: {prior_calls[0]['params']}"
    )


# --------------------------------------------------------------------------- #
# Failure before transaction starts (offline — no duckdb needed).
# --------------------------------------------------------------------------- #
class _FailBeforeConn:
    """Accepts sim_runs INSERT/UPDATE but raises on the feature-version query."""

    def __init__(self) -> None:
        self.executed: list[str] = []
        self.closed = False

    def execute(self, sql: str, params=None):
        self.executed.append(sql.strip()[:60])
        if "MAX(feature_schema_version)" in sql:
            raise RuntimeError("feature-version-boom")
        return FakeCursor([])

    def close(self) -> None:
        self.closed = True


class _FakeMgr:
    def __init__(self, conn) -> None:
        self._conn = conn

    def connect_simulation_with_prod(self, **kwargs):
        return self._conn


def test_failure_before_transaction_returns_service_result() -> None:
    """A failure in _feature_version returns failed ServiceResult; no raw exception."""
    conn = _FailBeforeConn()
    rid = "pre-tx-fail-rid"
    res = SimulationEngine(_FakeMgr(conn)).run(
        sim_name="s", mode="research",
        start_date=SESSIONS[0], end_date=SESSIONS[0],
        config_ids=[CONFIG_ID], strategy_configs={CONFIG_ID: make_config()},
        run_id=rid,
    )
    assert res.status == service_result.STATUS_FAILED
    assert res.run_id == rid
    assert any("feature-version-boom" in e for e in res.errors)
    assert frozenset(res.metadata) == frozenset(se.RUN_METADATA_KEYS)
    # sim_runs pending INSERT was sent before the failure.
    assert any("INSERT INTO sim_runs" in s for s in conn.executed)
    # status was updated to 'failed' after the rollback-skip (tx not started).
    assert any("UPDATE sim_runs" in s for s in conn.executed)
    # connection must always be closed regardless of failure mode.
    assert conn.closed


# --------------------------------------------------------------------------- #
# Duplicate supplied run_id (offline — PK conflict on sim_runs INSERT).
# --------------------------------------------------------------------------- #
class _DupRunConn:
    """Always raises on sim_runs INSERT (simulates PK conflict)."""

    def __init__(self) -> None:
        self.closed = False

    def execute(self, sql: str, params=None):
        if "INSERT INTO sim_runs" in sql:
            raise RuntimeError("UNIQUE constraint failed: sim_runs.sim_run_id")
        return FakeCursor([])

    def close(self) -> None:
        self.closed = True


def test_duplicate_run_id_returns_failed_service_result() -> None:
    """A PK conflict on sim_runs INSERT returns failed ServiceResult with run_id preserved."""
    rid = "dup-run-id-fixed"
    res = SimulationEngine(_FakeMgr(_DupRunConn())).run(
        sim_name="s", mode="research",
        start_date=SESSIONS[0], end_date=SESSIONS[0],
        config_ids=[CONFIG_ID], strategy_configs={CONFIG_ID: make_config()},
        run_id=rid,
    )
    assert res.status == service_result.STATUS_FAILED
    assert res.run_id == rid
    assert any("constraint" in e.lower() or "unique" in e.lower() or "sim_run" in e.lower()
               for e in res.errors)
    assert frozenset(res.metadata) == frozenset(se.RUN_METADATA_KEYS)


# --------------------------------------------------------------------------- #
# Integration layer (real DuckDB + Polars + Module 03 schema).
# Gated with skipif so the offline tests above always run.
# --------------------------------------------------------------------------- #
import importlib.util  # noqa: E402

_HAS_DB = bool(
    importlib.util.find_spec("duckdb") and importlib.util.find_spec("polars")
)
_needs_db = pytest.mark.skipif(not _HAS_DB, reason="requires duckdb + polars")


@pytest.fixture
def tmp_db_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    from app.config import settings
    from app.database import schema_manager as sm

    duckdb_dir = tmp_path / "duckdb"
    prod = duckdb_dir / "prod.duckdb"
    debug = duckdb_dir / "debug.duckdb"
    simulation = duckdb_dir / "simulation.duckdb"
    monkeypatch.setattr(settings, "DUCKDB_DIR", duckdb_dir, raising=True)
    monkeypatch.setattr(settings, "PROD_DB_PATH", prod, raising=True)
    monkeypatch.setattr(settings, "DEBUG_DB_PATH", debug, raising=True)
    monkeypatch.setattr(settings, "SIMULATION_DB_PATH", simulation, raising=True)
    assert sm.apply_prod_schema().status == service_result.STATUS_SUCCESS
    assert sm.apply_simulation_schema().status == service_result.STATUS_SUCCESS
    return {"prod": prod, "simulation": simulation}


def _conn(path: Path):
    import duckdb

    return duckdb.connect(database=str(path))


def _seed_prod_happy_path(
    prod: Path,
    *,
    ticker: str = "AAA",
    sim_date: date,
    feature_cutoff: date | None = None,
    price_date: date | None = None,
) -> None:
    """Seed a single ticker that passes Step 3 filters on ``sim_date``.

    Features are seeded for every session up to ``sim_date`` (for prior-10 /
    screening); prices are seeded for the full forward window so outcomes can
    resolve. ``feature_cutoff`` / ``price_date`` overrides let no-look-ahead
    tests poison the inputs.
    """
    cutoff = feature_cutoff if feature_cutoff is not None else None
    cal = FakeCalendar(SESSIONS)
    sim_idx = cal._index[sim_date]
    conn = _conn(prod)
    try:
        conn.execute(
            "INSERT INTO ticker_master (ticker, symbol_type, sector, industry, active_flag) "
            "VALUES (?, 'stock', 'Tech', 'Software', TRUE)",
            [ticker],
        )
        # Features for sessions [0 .. sim_idx].
        for i in range(sim_idx + 1):
            fdate = SESSIONS[i]
            fcut = cutoff if (fdate == sim_date and cutoff is not None) else fdate
            conn.execute(
                "INSERT INTO daily_features "
                "(ticker, feature_date, feature_cutoff_date, feature_schema_version, "
                " feature_ready, ema20, ema50, ema200, ema_alignment_score, "
                " distance_to_ema50_pct, rsi14, roc20, atr14, rvol20, "
                " avg_dollar_volume_20d, breakout_proximity, "
                " pullback_from_recent_high_pct, consolidation_score, "
                " sector_relative_strength, market_regime, days_to_earnings_bd, "
                " macro_event_risk_flag, calculated_at) "
                "VALUES (?, ?, ?, 'v1', TRUE, 100, 95, 90, 80, 0.02, 58, 0.10, 2.0, "
                " 2.0, 5000000, 0.0, -0.05, 80, 0.06, 'bull', 30, FALSE, "
                " CAST(now() AS TIMESTAMP))",
                [ticker, fdate, fcut],
            )
        # Prices: sessions [0 .. sim_idx + 60] so outcome evals resolve.
        for i in range(sim_idx + 61):
            pdate = SESSIONS[i]
            if price_date is not None and pdate == sim_date:
                pdate = price_date  # poison the sim_date price (no-look-ahead test)
            conn.execute(
                "INSERT INTO daily_prices "
                "(ticker, date, open_raw, high_raw, low_raw, close_raw, volume_raw, "
                " open_adj, high_adj, low_adj, close_adj, volume_adj, source_provider, "
                " data_quality_status, mutation_flag, created_at) "
                "VALUES (?, ?, 100, 105, 95, 100, 1000, 100, 105, 95, 102, 1000, "
                " 'test', 'ok', FALSE, CAST(now() AS TIMESTAMP))",
                [ticker, pdate],
            )
    finally:
        conn.close()


@_needs_db
def test_integration_research_full_pipeline(tmp_db_paths) -> None:
    sim_date = SESSIONS[40]  # leave history for prior-10
    _seed_prod_happy_path(tmp_db_paths["prod"], sim_date=sim_date)
    eng = SimulationEngine()
    res = eng.run(
        sim_name="research-1", mode="research",
        start_date=sim_date, end_date=sim_date,
        config_ids=[CONFIG_ID], strategy_configs={CONFIG_ID: make_config()},
    )
    assert res.status == service_result.STATUS_SUCCESS, res.errors

    conn = _conn(tmp_db_paths["simulation"])
    try:
        run_row = conn.execute(
            "SELECT status, mode, notes FROM sim_runs WHERE sim_run_id = ?", [res.run_id]
        ).fetchone()
        assert run_row == ("success", "research", None)
        assert conn.execute("SELECT COUNT(*) FROM sim_step3_candidates").fetchone()[0] >= 1
        assert conn.execute("SELECT COUNT(*) FROM sim_step4_analysis").fetchone()[0] >= 1
        assert conn.execute("SELECT COUNT(*) FROM sim_step5_proposals").fetchone()[0] >= 1
        outc = conn.execute(
            "SELECT list_membership, entry_price_sim, outcome_status "
            "FROM sim_signal_outcomes"
        ).fetchall()
        assert len(outc) >= 1
        assert all(o[0] in (se.LIST_RAW_ONLY, se.LIST_DIVERSIFIED_ONLY, se.LIST_BOTH) for o in outc)
        # 1 config * 4 horizons * 2 list types.
        assert conn.execute("SELECT COUNT(*) FROM sim_config_comparisons").fetchone()[0] == 8
    finally:
        conn.close()


@_needs_db
def test_integration_no_look_ahead_future_cutoff_excluded(tmp_db_paths) -> None:
    sim_date = SESSIONS[40]
    future_cutoff = SESSIONS[45]  # cutoff AFTER sim_date -> must be excluded
    _seed_prod_happy_path(tmp_db_paths["prod"], sim_date=sim_date, feature_cutoff=future_cutoff)
    res = SimulationEngine().run(
        sim_name="nla", mode="research", start_date=sim_date, end_date=sim_date,
        config_ids=[CONFIG_ID], strategy_configs={CONFIG_ID: make_config()},
    )
    assert res.status == service_result.STATUS_SUCCESS
    conn = _conn(tmp_db_paths["simulation"])
    try:
        # The only feature row for sim_date has a future cutoff -> no candidates.
        assert conn.execute(
            "SELECT COUNT(*) FROM sim_step3_candidates WHERE signal_date = ?", [sim_date]
        ).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM sim_signal_outcomes").fetchone()[0] == 0
    finally:
        conn.close()


@_needs_db
def test_integration_future_price_fails_price_filter(tmp_db_paths) -> None:
    sim_date = SESSIONS[40]
    # Move the sim_date price to a far-future (unseeded) date so the screening
    # price join misses without colliding with another seeded price row.
    _seed_prod_happy_path(tmp_db_paths["prod"], sim_date=sim_date, price_date=SESSIONS[200])
    res = SimulationEngine().run(
        sim_name="nla2", mode="research", start_date=sim_date, end_date=sim_date,
        config_ids=[CONFIG_ID], strategy_configs={CONFIG_ID: make_config()},
    )
    assert res.status == service_result.STATUS_SUCCESS
    conn = _conn(tmp_db_paths["simulation"])
    try:
        row = conn.execute(
            "SELECT passed_hard_filters FROM sim_step3_candidates WHERE signal_date = ?",
            [sim_date],
        ).fetchone()
        assert row is not None and row[0] is False  # missing same-day price -> fails
        assert conn.execute("SELECT COUNT(*) FROM sim_signal_outcomes").fetchone()[0] == 0
    finally:
        conn.close()


@_needs_db
def test_integration_write_failure_rolls_back_and_marks_failed(tmp_db_paths) -> None:
    from app.database import duckdb_manager as dbm

    sim_date = SESSIONS[40]
    _seed_prod_happy_path(tmp_db_paths["prod"], sim_date=sim_date)

    class FailingConnection:
        """Clean proxy that raises on sim_signal_outcomes inserts only."""

        def __init__(self, inner) -> None:
            self.inner = inner

        def execute(self, sql: str, params=None):
            if "INSERT INTO sim_signal_outcomes" in sql:
                raise RuntimeError("boom-outcome")
            return self.inner.execute(sql) if params is None else self.inner.execute(sql, params)

        def close(self) -> None:
            self.inner.close()

    class FailingManager:
        def connect_simulation_with_prod(self, *a, **k):
            return FailingConnection(dbm.connect_simulation_with_prod(*a, **k))

    res = SimulationEngine(FailingManager()).run(
        sim_name="fail", mode="research", start_date=sim_date, end_date=sim_date,
        config_ids=[CONFIG_ID], strategy_configs={CONFIG_ID: make_config()},
    )
    assert res.status == service_result.STATUS_FAILED
    assert any("boom-outcome" in e for e in res.errors)

    conn = _conn(tmp_db_paths["simulation"])
    try:
        row = conn.execute(
            "SELECT status, notes FROM sim_runs WHERE sim_run_id = ?", [res.run_id]
        ).fetchone()
        assert row is not None, "sim_runs row must exist even after failure"
        status, notes = row
        assert status == "failed"
        assert notes is not None and "boom-outcome" in notes
        # Partial sim writes inside the transaction must be rolled back.
        assert conn.execute("SELECT COUNT(*) FROM sim_step3_candidates").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM sim_signal_outcomes").fetchone()[0] == 0
    finally:
        conn.close()


@_needs_db
def test_integration_duplicate_run_id_returns_failed_service_result(tmp_db_paths) -> None:
    """Second call with same explicit run_id returns failed ServiceResult; no raw exception."""
    sim_date = SESSIONS[40]
    _seed_prod_happy_path(tmp_db_paths["prod"], sim_date=sim_date)
    rid = "dup-integration-rid"

    first = SimulationEngine().run(
        sim_name="dup-first", mode="research",
        start_date=sim_date, end_date=sim_date,
        config_ids=[CONFIG_ID], strategy_configs={CONFIG_ID: make_config()},
        run_id=rid,
    )
    # First run may succeed or fail depending on data, but must return ServiceResult.
    assert first.run_id == rid

    second = SimulationEngine().run(
        sim_name="dup-second", mode="research",
        start_date=sim_date, end_date=sim_date,
        config_ids=[CONFIG_ID], strategy_configs={CONFIG_ID: make_config()},
        run_id=rid,
    )
    assert second.status == service_result.STATUS_FAILED
    assert second.run_id == rid
    assert frozenset(second.metadata) == frozenset(se.RUN_METADATA_KEYS)


@_needs_db
def test_integration_walk_forward_smoke(tmp_db_paths) -> None:
    # Short window -> no folds planned, but the run still succeeds end-to-end.
    sim_date = SESSIONS[40]
    _seed_prod_happy_path(tmp_db_paths["prod"], sim_date=sim_date)
    res = SimulationEngine().run(
        sim_name="wf", mode="walk_forward", start_date=sim_date, end_date=SESSIONS[42],
        config_ids=[CONFIG_ID], strategy_configs={CONFIG_ID: make_config()},
    )
    assert res.status == service_result.STATUS_SUCCESS
    assert res.metadata["mode"] == "walk_forward"
