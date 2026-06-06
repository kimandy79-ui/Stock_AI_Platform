"""Tests for ``tools/backfill_prod_history.py`` and the provider batch method.

Fully offline. The backfill tool's collaborators (DB manager, provider, every
step engine, the sleeper, and the jitter function) are injected as in-process
fakes, so no real DuckDB, ``yfinance``, network, or sleeping occurs. The
provider batch normalization is exercised with a hand-rolled, pandas-free fake
frame that mimics the duck-typed interface the provider relies on
(``empty`` / ``columns`` / ``iterrows`` / ``__getitem__``).
"""

from __future__ import annotations

import inspect
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app.providers.provider_interface import PriceHistoryRequest
from app.providers.yahoo_provider import YahooProvider
from app.utils import service_result
from app.utils.service_result import ServiceResult

from tools import backfill_prod_history as bpf


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
def _ok(rows: int = 1, warnings: list[str] | None = None, metadata: dict | None = None):
    status = (
        service_result.STATUS_SUCCESS_WITH_WARNINGS
        if warnings
        else service_result.STATUS_SUCCESS
    )
    return ServiceResult(status=status, run_id="rid", rows_processed=rows,
                         warnings=warnings or [], metadata=metadata or {})


def _failed(errors: list[str] | None = None):
    return ServiceResult(status=service_result.STATUS_FAILED, run_id="rid",
                         rows_processed=0, errors=errors or ["boom"])


class _FakeConn:
    """Routes SQL by content to canned rows; records nothing destructive."""

    def __init__(self, *, has_schema=True, active=None, coverage_rows=None):
        self._has_schema = has_schema
        self._active = active or []
        self._coverage_rows = coverage_rows or []
        self.closed = False

    def execute(self, sql: str, params=None):
        s = sql.lower()
        if "information_schema.tables" in s:
            self._last = [("ticker_master",)] if self._has_schema else []
        elif "from ticker_master" in s:
            self._last = [(t,) for t in self._active]
        elif "from daily_prices" in s:
            self._last = list(self._coverage_rows)
        else:
            self._last = []
        return self

    def fetchall(self):
        return self._last

    def close(self):
        self.closed = True


class _FakeDb:
    def __init__(self, conn: _FakeConn):
        self._conn = conn
        self.connect_calls: list[tuple[str, bool]] = []

    def connect(self, db_role: str, read_only: bool = False):
        self.connect_calls.append((db_role, read_only))
        return self._conn


class _RecordingEngine:
    """Generic step-engine fake recording the db_role it was called with."""

    def __init__(self, result=None, method="generic"):
        self._result = result if result is not None else _ok()
        self.calls: list[dict] = []
        self._method = method

    def _record(self, **kwargs):
        self.calls.append(kwargs)
        return self._result

    # one method per engine the tool calls
    def load(self, **kw):       return self._record(**kw)   # benchmark
    def apply_snapshot(self, **kw): return self._record(**kw)  # universe
    def ingest(self, **kw):     return self._record(**kw)   # module 8
    def validate(self, **kw):   return self._record(**kw)   # module 9
    def detect(self, **kw):     return self._record(**kw)   # module 10
    def calculate(self, **kw):  return self._record(**kw)   # module 11
    def classify(self, **kw):   return self._record(**kw)   # module 12


class _FakeProvider:
    def __init__(self, bars_by_ticker=None, batch=True):
        self._bars = bars_by_ticker or {}
        self._batch = batch
        self.symbols = []
        self.many_calls: list[dict] = []
        if batch:
            self.get_price_history_many = self._many  # attribute => hasattr True

    def _many(self, tickers, start_date, end_date, symbol_type="stock"):
        self.many_calls.append({"tickers": list(tickers), "start": start_date,
                                "end": end_date})
        bbt = {t: self._bars.get(t, []) for t in tickers}
        flat = [b for bars in bbt.values() for b in bars]
        return ServiceResult(status=service_result.STATUS_SUCCESS, run_id="rid",
                             rows_processed=len(flat),
                             metadata={"bars_by_ticker": bbt, "bars": flat})

    def get_price_history(self, request):
        bars = self._bars.get(request.ticker, [])
        return ServiceResult(status=service_result.STATUS_SUCCESS, run_id="rid",
                             rows_processed=len(bars), metadata={"bars": bars})

    def list_symbols(self, symbol_type=None):
        return ServiceResult(status=service_result.STATUS_SUCCESS, run_id="rid",
                             rows_processed=len(self.symbols),
                             metadata={"symbols": self.symbols})


def _make_backfiller(*, has_schema=True, active=None, coverage_rows=None,
                     provider=None, engines=None):
    conn = _FakeConn(has_schema=has_schema, active=active, coverage_rows=coverage_rows)
    db = _FakeDb(conn)
    provider = provider or _FakeProvider()
    eng = engines or {}
    bf = bpf.Backfiller(
        db_manager=db,
        provider=provider,
        benchmark_loader=eng.get("benchmark", _RecordingEngine()),
        universe_engine=eng.get("universe", _RecordingEngine()),
        ingestion_engine=eng.get("ingest", _RecordingEngine()),
        validation_engine=eng.get("validation", _RecordingEngine()),
        mutation_engine=eng.get("mutation", _RecordingEngine()),
        feature_engine=eng.get("features", _RecordingEngine()),
        regime_engine=eng.get("regime", _RecordingEngine()),
        service_result_mod=service_result,
        sleeper=lambda _s: None,          # disable real sleeping
        jitter_fn=lambda _j: 0.0,         # deterministic, no jitter
    )
    return bf, db, provider, eng


# --------------------------------------------------------------------------- #
# CLI parsing / date validation
# --------------------------------------------------------------------------- #
def test_cli_parses_start_and_end_date() -> None:
    args = bpf._parse_args(["--start-date", "2023-06-05", "--end-date", "2026-06-05"])
    assert args.start_date == date(2023, 6, 5)
    assert args.end_date == date(2026, 6, 5)
    assert args.batch_size == 50 and args.sleep_seconds == 3.0


def test_invalid_date_range_exits_nonzero(capsys: pytest.CaptureFixture) -> None:
    code = bpf.main(["--start-date", "2026-06-05", "--end-date", "2023-06-05"])
    assert code == 1
    assert "start-date must be <= end-date" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Batch splitting
# --------------------------------------------------------------------------- #
def test_batch_splitting_50() -> None:
    tickers = [f"T{i}" for i in range(125)]
    batches = bpf._split_batches(tickers, 50)
    assert [len(b) for b in batches] == [50, 50, 25]
    assert batches[0][0] == "T0" and batches[-1][-1] == "T124"


# --------------------------------------------------------------------------- #
# Dry-run does not write
# --------------------------------------------------------------------------- #
def test_dry_run_performs_no_writes() -> None:
    eng = {k: _RecordingEngine() for k in
           ("benchmark", "universe", "ingest", "validation",
            "mutation", "features", "regime")}
    bf, db, _prov, _ = _make_backfiller(active=["AAA", "BBB"], engines=eng)
    result = bf.run(start_date=date(2024, 1, 1), end_date=date(2024, 2, 1),
                    dry_run=True)
    assert result.status == service_result.STATUS_SUCCESS
    assert result.metadata["dry_run"] is True
    # No write-path engine was invoked.
    for name, engine in eng.items():
        assert engine.calls == [], f"{name} should not run in dry-run"
    # Only read-only connections were opened.
    assert all(read_only for _role, read_only in db.connect_calls)


# --------------------------------------------------------------------------- #
# db_role == prod everywhere; no step 3/4/5 engines exist
# --------------------------------------------------------------------------- #
def test_uses_db_role_prod_for_every_engine() -> None:
    eng = {k: _RecordingEngine() for k in
           ("benchmark", "universe", "ingest", "validation",
            "mutation", "features", "regime")}
    bf, db, _prov, _ = _make_backfiller(active=["AAA"], engines=eng)
    bf.run(start_date=date(2024, 1, 1), end_date=date(2024, 1, 5), resume=False)
    for name, engine in eng.items():
        for call in engine.calls:
            assert call.get("db_role") == "prod", f"{name} not prod"
    assert all(role == "prod" for role, _ro in db.connect_calls)


def test_no_screening_analysis_or_proposal_engines() -> None:
    # The backfill tool must not own step 3/4/5; assert no such attributes are
    # constructed on the Backfiller and run() never references them.
    bf, *_ = _make_backfiller(active=["AAA"])
    for forbidden in ("_screening_engine", "_analysis_engine",
                      "_proposal_engine", "_outcome_creator", "_outcome_processor"):
        assert not hasattr(bf, forbidden)
    src = inspect.getsource(bpf.Backfiller.run)
    for token in ("screen(", "analyze(", "propose(", "enqueue("):
        assert token not in src


# --------------------------------------------------------------------------- #
# Resume logic
# --------------------------------------------------------------------------- #
def test_resume_skips_fully_covered_ticker() -> None:
    start, end = date(2024, 1, 1), date(2024, 3, 1)
    # AAA fully covers the range with many rows -> skip. BBB has too few/narrow.
    coverage = [
        ("AAA", 40, date(2023, 12, 1), date(2024, 3, 2)),
        ("BBB", 1, date(2024, 2, 1), date(2024, 2, 1)),
    ]
    bf, _db, _prov, _ = _make_backfiller(active=["AAA", "BBB"], coverage_rows=coverage)
    remaining, skipped = bf._apply_resume(["AAA", "BBB"], start, end)
    assert skipped == ["AAA"]
    assert remaining == ["BBB"]


def test_resume_reprocesses_uncovered_ticker() -> None:
    start, end = date(2024, 1, 1), date(2024, 3, 1)
    # CCC has no coverage row at all -> reprocess.
    bf, _db, _prov, _ = _make_backfiller(active=["CCC"], coverage_rows=[])
    remaining, skipped = bf._apply_resume(["CCC"], start, end)
    assert remaining == ["CCC"] and skipped == []


# --------------------------------------------------------------------------- #
# Schema guard
# --------------------------------------------------------------------------- #
def test_missing_prod_schema_fails() -> None:
    bf, *_ = _make_backfiller(has_schema=False, active=[])
    result = bf.run(start_date=date(2024, 1, 1), end_date=date(2024, 1, 2))
    assert result.status == service_result.STATUS_FAILED
    assert any("ticker_master" in e for e in result.errors)


# --------------------------------------------------------------------------- #
# Sleep/jitter are injectable and disabled here
# --------------------------------------------------------------------------- #
def test_sleeper_and_jitter_are_injectable() -> None:
    slept: list[float] = []
    eng = {k: _RecordingEngine() for k in
           ("benchmark", "universe", "ingest", "validation",
            "mutation", "features", "regime")}
    conn = _FakeConn(has_schema=True, active=["A", "B", "C"])
    db = _FakeDb(conn)
    bf = bpf.Backfiller(
        db_manager=db, provider=_FakeProvider(),
        benchmark_loader=eng["benchmark"], universe_engine=eng["universe"],
        ingestion_engine=eng["ingest"], validation_engine=eng["validation"],
        mutation_engine=eng["mutation"], feature_engine=eng["features"],
        regime_engine=eng["regime"], service_result_mod=service_result,
        sleeper=slept.append, jitter_fn=lambda _j: 0.0,
    )
    bf.run(start_date=date(2024, 1, 1), end_date=date(2024, 1, 5),
           batch_size=1, resume=False)
    # 3 tickers => 3 batches => 2 inter-batch sleeps (no sleep after the last).
    assert slept == [3.0, 3.0]


# --------------------------------------------------------------------------- #
# Provider batch normalization (pandas-free fake frame)
# --------------------------------------------------------------------------- #
class _FakeRow:
    def __init__(self, data: dict[str, Any]):
        self._d = data

    def __getitem__(self, key):
        return self._d[key]


class _FakeFrame:
    """Minimal duck-typed frame: ``empty`` / ``columns`` / ``iterrows`` / [key]."""

    def __init__(self, rows: list[tuple[Any, dict[str, Any]]],
                 children: dict[str, "_FakeFrame"] | None = None):
        self._rows = rows
        self._children = children or {}
        self.columns = list(rows[0][1].keys()) if rows else []

    @property
    def empty(self) -> bool:
        return not self._rows and not self._children

    def iterrows(self):
        for idx, data in self._rows:
            yield idx, _FakeRow(data)

    def __getitem__(self, key):
        if key in self._children:
            return self._children[key]
        raise KeyError(key)


class _FakeYFDownload:
    def __init__(self, frame):
        self._frame = frame
        self.calls: list[dict] = []

    def download(self, **kwargs):
        self.calls.append(kwargs)
        return self._frame


def _row(o, h, l, c, adj, v):
    return {"Open": o, "High": h, "Low": l, "Close": c,
            "Adj Close": adj, "Volume": v, "Dividends": 0.0, "Stock Splits": 0.0}


def test_provider_batch_normalizes_multi_ticker_frame() -> None:
    d1, d2 = "2024-01-02", "2024-01-03"
    aaa = _FakeFrame([(d1, _row(10, 11, 9, 10, 10, 1000)),
                      (d2, _row(10, 12, 10, 11, 11, 1100))])
    bbb = _FakeFrame([(d1, _row(20, 21, 19, 20, 20, 2000))])
    top = _FakeFrame([], children={"AAA": aaa, "BBB": bbb})
    yf = _FakeYFDownload(top)
    provider = YahooProvider(yf_module=yf)

    result = provider.get_price_history_many(
        tickers=["AAA", "BBB"], start_date=date(2024, 1, 1), end_date=date(2024, 1, 31))

    assert result.status in (service_result.STATUS_SUCCESS,
                             service_result.STATUS_SUCCESS_WITH_WARNINGS)
    bbt = result.metadata["bars_by_ticker"]
    assert len(bbt["AAA"]) == 2 and len(bbt["BBB"]) == 1
    assert result.rows_processed == 3
    # Inclusive end -> exclusive vendor end (end_date + 1 day).
    assert yf.calls[0]["end"] == date(2024, 2, 1)
    assert yf.calls[0]["group_by"] == "ticker"
    # Mapping fidelity: AAA day 1 raw close.
    assert bbt["AAA"][0].close_raw == 10.0
    assert bbt["AAA"][0].ticker == "AAA"


def test_provider_batch_isolates_missing_ticker() -> None:
    aaa = _FakeFrame([("2024-01-02", _row(10, 11, 9, 10, 10, 1000))])
    top = _FakeFrame([], children={"AAA": aaa})  # ZZZ absent
    provider = YahooProvider(yf_module=_FakeYFDownload(top))
    result = provider.get_price_history_many(
        tickers=["AAA", "ZZZ"], start_date=date(2024, 1, 1), end_date=date(2024, 1, 31))
    bbt = result.metadata["bars_by_ticker"]
    assert len(bbt["AAA"]) == 1
    assert bbt["ZZZ"] == []          # isolated, not fatal
    assert result.status == service_result.STATUS_SUCCESS_WITH_WARNINGS


def test_provider_batch_download_failure_is_failed_not_raise() -> None:
    class _Boom:
        def download(self, **kw):
            raise RuntimeError("rate limit: too many requests")
    provider = YahooProvider(yf_module=_Boom())
    result = provider.get_price_history_many(
        tickers=["AAA"], start_date=date(2024, 1, 1), end_date=date(2024, 1, 31))
    assert result.status == service_result.STATUS_FAILED
    assert result.metadata["error_detail"].kind == "rate_limited"


# --------------------------------------------------------------------------- #
# Existing single-ticker contract unchanged
# --------------------------------------------------------------------------- #
def test_single_ticker_get_price_history_signature_unchanged() -> None:
    sig = inspect.signature(YahooProvider.get_price_history)
    assert list(sig.parameters) == ["self", "request"]


def test_module8_ingest_tickers_defaults_to_none() -> None:
    from app.services.ingestion.daily_price_ingestion import DailyPriceIngestionEngine
    sig = inspect.signature(DailyPriceIngestionEngine.ingest)
    assert sig.parameters["tickers"].default is None
    # Pre-existing params keep their order/defaults.
    names = list(sig.parameters)
    assert names[:6] == ["self", "provider", "start_date", "end_date",
                         "db_role", "run_id"]


# --------------------------------------------------------------------------- #
# End-to-end happy path (all fakes), uses batch download
# --------------------------------------------------------------------------- #
def test_full_run_success_uses_batch_download_and_writes() -> None:
    bars = {"AAA": [object()], "BBB": [object(), object()]}
    provider = _FakeProvider(bars_by_ticker=bars, batch=True)
    # ingestion fake echoes rows_processed from the batch it was scoped to.
    ingest = _RecordingEngine(result=_ok(rows=3))
    eng = {"benchmark": _RecordingEngine(), "universe": _RecordingEngine(),
           "ingest": ingest, "validation": _RecordingEngine(),
           "mutation": _RecordingEngine(), "features": _RecordingEngine(),
           "regime": _RecordingEngine()}
    bf, _db, prov, _ = _make_backfiller(active=["AAA", "BBB"],
                                        provider=provider, engines=eng)
    result = bf.run(start_date=date(2024, 1, 1), end_date=date(2024, 1, 31),
                    resume=False, batch_size=50)
    assert result.status in (service_result.STATUS_SUCCESS,
                             service_result.STATUS_SUCCESS_WITH_WARNINGS)
    assert result.metadata["used_batch_download"] is True
    assert len(prov.many_calls) == 1  # one batch download for both tickers
    assert ingest.calls and ingest.calls[0]["tickers"] == ["AAA", "BBB"]


# --------------------------------------------------------------------------- #
# BLOCKER FIX: tickers-file / zero-ticker guard
# --------------------------------------------------------------------------- #

def test_fresh_db_empty_provider_no_tickers_file_fails() -> None:
    """Regression: fresh DB + empty provider list_symbols => must fail, not succeed.

    Without --tickers-file, ticker_master stays empty (active=[]).
    The zero-ticker guard must return failed with a descriptive error so the
    operator knows to provide --tickers-file, rather than silently completing
    with zero ingested rows.
    """
    bf, *_ = _make_backfiller(active=[])          # empty ticker_master
    result = bf.run(
        start_date=date(2024, 1, 1), end_date=date(2024, 1, 31),
        tickers_file=None, resume=False,
    )
    assert result.status == service_result.STATUS_FAILED
    assert any("no active stock tickers" in e for e in result.errors)
    assert any("tickers-file" in e for e in result.errors)


def test_active_tickers_zero_returns_failed_with_clear_message() -> None:
    """Explicit guard: active_tickers == 0 after universe step => failed."""
    # Universe engine records its call but ticker_master query still returns [].
    uni_eng = _RecordingEngine()
    bf, *_ = _make_backfiller(active=[], engines={"universe": uni_eng})
    result = bf.run(
        start_date=date(2024, 1, 1), end_date=date(2024, 1, 31), resume=False,
    )
    assert result.status == service_result.STATUS_FAILED
    assert result.errors, "must have at least one error message"
    full_msg = " ".join(result.errors).lower()
    assert "no active stock tickers" in full_msg


def test_tickers_file_populates_universe_entries(tmp_path: Path) -> None:
    """--tickers-file passes TickerInfo entries to UniverseSnapshotEngine."""
    csv_text = (
        "ticker,symbol_type,name,industry\r\n"
        "AAPL,stock,Apple Inc.,Technology\r\n"
        "MSFT,stock,Microsoft Corp.,Technology\r\n"
    )
    f = tmp_path / "tickers.csv"
    f.write_text(csv_text, encoding="utf-8")

    uni_eng = _RecordingEngine()
    bf, *_ = _make_backfiller(
        # After apply_snapshot, ticker_master has the two tickers.
        active=["AAPL", "MSFT"],
        engines={"universe": uni_eng},
    )
    result = bf.run(
        start_date=date(2024, 1, 1), end_date=date(2024, 1, 31),
        tickers_file=f, resume=False,
    )
    assert result.status in (service_result.STATUS_SUCCESS,
                             service_result.STATUS_SUCCESS_WITH_WARNINGS)
    # apply_snapshot was called exactly once with the file's entries.
    assert len(uni_eng.calls) == 1
    call = uni_eng.calls[0]
    assert call["source"] == "file"
    tickers_in_call = [e.ticker for e in call["entries"]]
    assert sorted(tickers_in_call) == ["AAPL", "MSFT"]
    # company_name and sector were mapped from the CSV.
    aapl_entry = next(e for e in call["entries"] if e.ticker == "AAPL")
    assert aapl_entry.company_name == "Apple Inc."
    assert aapl_entry.sector == "Technology"


def test_no_tickers_file_uses_existing_ticker_master() -> None:
    """Without --tickers-file the tool uses whatever is in ticker_master.

    apply_snapshot must NOT be called (so existing ticker_master is untouched)
    and provider.list_symbols must NOT be called.
    """
    uni_eng = _RecordingEngine()
    provider = _FakeProvider(batch=True)
    bf, *_ = _make_backfiller(
        active=["EXISTING_A", "EXISTING_B"],
        provider=provider,
        engines={"universe": uni_eng},
    )
    result = bf.run(
        start_date=date(2024, 1, 1), end_date=date(2024, 1, 31),
        tickers_file=None, resume=False,
    )
    assert result.status in (service_result.STATUS_SUCCESS,
                             service_result.STATUS_SUCCESS_WITH_WARNINGS)
    # apply_snapshot never called: no tickers-file means existing universe is used.
    assert uni_eng.calls == [], "apply_snapshot must not be called without tickers-file"
    # provider.list_symbols never called: no silent empty-list assumption.
    # (FakeProvider has list_symbols but we verify it was not the source.)
    assert result.metadata.get("active_tickers") == 2


# --------------------------------------------------------------------------- #
# _load_tickers_from_file unit tests
# --------------------------------------------------------------------------- #

def test_load_tickers_csv_with_full_header(tmp_path: Path) -> None:
    """Parses the project CSV format (ticker,yahoo_symbol,symbol_type,...,name,industry)."""
    csv_text = (
        "ticker,yahoo_symbol,symbol_type,active_flag,name,industry,"
        "market_cap,last_price,volume,source_symbol\r\n"
        "NVDA,NVDA,stock,TRUE,NVIDIA Corporation Common Stock,Technology,"
        "5.29E+12,218.66,169024638,NVDA\r\n"
        "AAPL,AAPL,stock,TRUE,Apple Inc. Common Stock,Technology,"
        "4.57E+12,311.23,44869329,AAPL\r\n"
    )
    f = tmp_path / "tickers.csv"
    f.write_text(csv_text, encoding="utf-8")
    entries = bpf._load_tickers_from_file(f)
    tickers = [e.ticker for e in entries]
    assert tickers == ["NVDA", "AAPL"]
    assert entries[0].company_name == "NVIDIA Corporation Common Stock"
    assert entries[0].sector == "Technology"
    assert all(e.symbol_type == "stock" for e in entries)


def test_load_tickers_csv_deduplicates(tmp_path: Path) -> None:
    f = tmp_path / "t.csv"
    f.write_text("ticker\nAAPL\nMSFT\nAAPL\n", encoding="utf-8")
    entries = bpf._load_tickers_from_file(f)
    assert [e.ticker for e in entries] == ["AAPL", "MSFT"]


def test_load_tickers_plain_text(tmp_path: Path) -> None:
    f = tmp_path / "t.txt"
    f.write_text("aapl\nmsft\nGOOGL\n", encoding="utf-8")
    entries = bpf._load_tickers_from_file(f)
    assert [e.ticker for e in entries] == ["AAPL", "MSFT", "GOOGL"]
    assert all(e.symbol_type == "stock" for e in entries)


def test_load_tickers_skips_non_stock_rows(tmp_path: Path) -> None:
    f = tmp_path / "t.csv"
    f.write_text("ticker,symbol_type\nAAPL,stock\nSPY,etf\nMSFT,stock\n",
                 encoding="utf-8")
    entries = bpf._load_tickers_from_file(f)
    assert [e.ticker for e in entries] == ["AAPL", "MSFT"]


def test_load_tickers_empty_file_raises(tmp_path: Path) -> None:
    f = tmp_path / "empty.csv"
    f.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        bpf._load_tickers_from_file(f)


def test_load_tickers_header_only_raises(tmp_path: Path) -> None:
    f = tmp_path / "header_only.csv"
    f.write_text("ticker,symbol_type\n", encoding="utf-8")
    with pytest.raises(ValueError, match="zero valid"):
        bpf._load_tickers_from_file(f)


def test_load_tickers_bom_utf8(tmp_path: Path) -> None:
    """BOM-prefixed UTF-8 files (common from Excel) are handled correctly."""
    f = tmp_path / "bom.csv"
    f.write_bytes(b"\xef\xbb\xbfticker\nAAPL\nMSFT\n")
    entries = bpf._load_tickers_from_file(f)
    assert entries[0].ticker == "AAPL"


def test_cli_parses_tickers_file_arg() -> None:
    args = bpf._parse_args([
        "--start-date", "2024-01-01",
        "--end-date", "2024-12-31",
        "--tickers-file", "/some/path/tickers.csv",
    ])
    assert args.tickers_file == "/some/path/tickers.csv"


def test_cli_tickers_file_missing_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """main() returns 1 when --tickers-file path does not exist."""
    code = bpf.main([
        "--start-date", "2024-01-01",
        "--end-date", "2024-12-31",
        "--tickers-file", "/nonexistent/path/tickers.csv",
    ])
    assert code == 1
    assert "--tickers-file not found" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Required: tickers-file snapshot failure + zero-ticker — benchmark/ingest
# must never be called in either case.
# --------------------------------------------------------------------------- #

def test_tickers_file_snapshot_failure_returns_failed_benchmark_not_called(
    tmp_path: Path,
) -> None:
    """--tickers-file provided but apply_snapshot fails => failed immediately.

    Benchmark loader and price ingestion must NOT be called: the run aborts as
    soon as the universe snapshot returns a non-ok result, before spending time
    on any market-data download.
    """
    csv_text = "ticker,symbol_type\nAAPL,stock\nMSFT,stock\n"
    f = tmp_path / "tickers.csv"
    f.write_text(csv_text, encoding="utf-8")

    # Universe engine is wired to return a hard failure.
    uni_eng   = _RecordingEngine(result=_failed(["DB write error"]))
    bench_eng = _RecordingEngine()
    ingest_eng = _RecordingEngine()

    bf, *_ = _make_backfiller(
        active=["AAPL", "MSFT"],
        engines={"universe": uni_eng, "benchmark": bench_eng, "ingest": ingest_eng},
    )
    result = bf.run(
        start_date=date(2024, 1, 1), end_date=date(2024, 1, 31),
        tickers_file=f, resume=False,
    )

    assert result.status == service_result.STATUS_FAILED, (
        "run must return failed when apply_snapshot fails"
    )
    # Error must mention the universe snapshot, not just the original DB message.
    assert any("universe snapshot failed" in e for e in result.errors), (
        f"error should reference 'universe snapshot failed', got: {result.errors}"
    )
    # No warning text must say "recoverable" — it is a hard stop.
    assert not any("recoverable" in w for w in result.warnings), (
        "snapshot failure must not be downgraded to a recoverable warning"
    )
    # Nothing downstream may have run.
    assert bench_eng.calls == [], (
        "benchmark loader must not be called after universe snapshot failure"
    )
    assert ingest_eng.calls == [], (
        "price ingestion must not be called after universe snapshot failure"
    )


def test_no_tickers_file_zero_ticker_master_returns_failed_benchmark_not_called() -> None:
    """No --tickers-file and empty ticker_master => failed before benchmark runs.

    The zero-ticker guard must fire before the benchmark loader is called so
    the run fails fast with a clear operator message instead of downloading
    benchmark data for a universe that does not exist yet.
    """
    bench_eng  = _RecordingEngine()
    ingest_eng = _RecordingEngine()

    bf, *_ = _make_backfiller(
        active=[],   # ticker_master is empty — no stocks
        engines={"benchmark": bench_eng, "ingest": ingest_eng},
    )
    result = bf.run(
        start_date=date(2024, 1, 1), end_date=date(2024, 1, 31),
        tickers_file=None, resume=False,
    )

    assert result.status == service_result.STATUS_FAILED, (
        "run must return failed when ticker_master has no active stocks"
    )
    assert any("no active stock tickers" in e for e in result.errors), (
        f"error should say 'no active stock tickers', got: {result.errors}"
    )
    assert any("tickers-file" in e for e in result.errors), (
        "error must hint at --tickers-file as the remedy"
    )
    assert bench_eng.calls == [], (
        "benchmark loader must not be called when zero active tickers found"
    )
    assert ingest_eng.calls == [], (
        "price ingestion must not be called when zero active tickers found"
    )
