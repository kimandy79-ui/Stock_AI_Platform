"""Backfill ``prod.duckdb`` with historical market data over a date range.

The daily pipeline (Module 20 / ``tools/run_prod_pipeline.py``) is intentionally
daily-only: it passes ``start_date = end_date = run_date`` to every market-data
step. This operator tool bootstraps a *range* of history into the **prod**
database without changing that daily behavior, so a fresh deployment can be
populated with several years of prices/features/regime before the first daily
proposal run.

What it does (in order), all against ``db_role="prod"``:

1. Ensure the prod schema already exists (fail clearly otherwise).
2. Module 06 universe snapshot for ``end_date`` (``as_of_date = end_date``).
3. Module 07 benchmark / sector-ETF load for the full ``[start, end]`` range.
4. Read the active stock universe from ``ticker_master``.
5. Optional resume filtering against existing ``daily_prices`` coverage.
6. Module 08 price ingestion for the full range, processed in ticker batches
   with anti-throttling sleep/jitter and exponential-backoff retry. When the
   provider exposes a multi-ticker batch download, each batch is fetched in a
   single vendor call (still inside the provider layer).
7. Modules 09/10/11/12 (validation / mutation / features / regime) once over the
   full range (not looped per historical day).

It does **not** generate proposals: daily screening/analysis/proposal/outcome
remain the job of the normal daily pipeline for the final signal date (run
``tools/run_prod_pipeline.py`` afterwards).

Boundaries: never writes to ``debug.duckdb`` or any simulation DB/table; never
imports/calls ``yfinance`` directly (all market data flows through the provider
abstraction); adds no schema and runs no DDL; does not use Module 22 Debug Mode.

Exit code: ``0`` on ``success`` / ``success_with_warnings``, ``1`` on ``failed``.

Usage::

    python tools/backfill_prod_history.py --start-date 2023-06-05 --end-date 2026-06-05
    python tools/backfill_prod_history.py --start-date 2023-06-05 --end-date 2026-06-05 \
        --batch-size 25 --sleep-seconds 5 --jitter-seconds 3
"""

from __future__ import annotations

import argparse
import csv
import logging
import random
import time
import uuid
from datetime import date
from typing import Any, Callable

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._bootstrap import ensure_repo_root_on_path

logger = logging.getLogger("tools.backfill_prod_history")

DB_ROLE_PROD = "prod"
_PROVIDER_BARS_KEY = "bars"
_PROVIDER_BARS_BY_TICKER_KEY = "bars_by_ticker"
_RESUME_MIN_ROWS = 2

# Status constants (kept local so the tool stays importable even if the
# service_result module path changes; resolved lazily where the real object is
# built).
STATUS_SUCCESS = "success"
STATUS_SUCCESS_WITH_WARNINGS = "success_with_warnings"
STATUS_FAILED = "failed"


# --------------------------------------------------------------------------- #
# Throttling helpers (injectable so tests can disable real waiting).
# --------------------------------------------------------------------------- #
def _default_jitter(jitter_seconds: float) -> float:
    """Return a random delay in ``[0, jitter_seconds]`` (0 when disabled)."""
    if jitter_seconds <= 0:
        return 0.0
    return random.uniform(0.0, jitter_seconds)


def _split_batches(tickers: list[str], batch_size: int) -> list[list[str]]:
    """Split ``tickers`` into consecutive batches of at most ``batch_size``."""
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    return [tickers[i : i + batch_size] for i in range(0, len(tickers), batch_size)]


# --------------------------------------------------------------------------- #
# Ticker-file loader.
# --------------------------------------------------------------------------- #
# Header aliases the loader recognises (case-insensitive, stripped).
_TICKER_COL_ALIASES = frozenset({"ticker", "symbol", "tick", "tickers", "symbols"})
_NAME_COL_ALIASES = frozenset({"name", "company_name", "company", "description"})
_SECTOR_COL_ALIASES = frozenset({"sector"})
_INDUSTRY_COL_ALIASES = frozenset({"industry"})
_SYMTYPE_COL_ALIASES = frozenset({"symbol_type", "type", "symtype"})
# Values that look like a header and must be skipped if the file lacks one.
_HEADER_SENTINEL = frozenset({"ticker", "symbol", "tickers", "symbols"})


def _load_tickers_from_file(path: "Path") -> list[Any]:
    """Load ``TickerInfo`` entries from a CSV or plain-text ticker file.

    Supports:
    * CSV with a header row containing a ``ticker`` (or ``symbol``) column, plus
      optional ``symbol_type``, ``name``, ``industry``, ``sector`` columns — the
      format produced by the project's universe export (e.g.
      ``backfill_tickers_common_only.csv``).
    * Plain text, one ticker per line (no header, no commas).

    Normalisation: tickers are uppercased and stripped; blank rows and header
    sentinels are skipped. Only rows where ``symbol_type`` equals ``"stock"`` (or
    where the column is absent) are returned — this is a stock-backfill tool.

    Returns
    -------
    list[TickerInfo]
        Deduplicated, ordered as they appear in the file.

    Raises
    ------
    ValueError
        If the file cannot be read or produces zero valid ticker entries after
        filtering.
    """
    # Lazy import — keeps the tool importable without the app package if needed.
    ensure_repo_root_on_path()
    from app.providers.provider_interface import TickerInfo
    from app.config import constants

    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8-sig")  # utf-8-sig strips BOM
    except OSError as exc:
        raise ValueError(f"cannot read tickers file {path}: {exc}") from exc

    lines = [ln.rstrip("\r\n") for ln in raw.splitlines()]
    if not lines:
        raise ValueError(f"tickers file {path} is empty")

    # Detect whether the file uses commas (CSV) or is plain-text.
    has_comma = any("," in ln for ln in lines if ln.strip())

    entries: list[TickerInfo] = []
    seen: set[str] = set()

    if has_comma:
        rows = list(csv.reader(lines))
        if not rows:
            raise ValueError(f"tickers file {path} parsed to zero rows")

        # Detect header by checking if first row contains a ticker-column alias.
        first = [f.strip().lower() for f in rows[0]]
        has_header = any(f in _TICKER_COL_ALIASES for f in first)

        if has_header:
            header = first
            data_rows = rows[1:]
        else:
            header = []
            data_rows = rows

        # Build column-index map from header (or assume col-0 = ticker).
        def _col(aliases: frozenset[str]) -> int | None:
            for i, h in enumerate(header):
                if h in aliases:
                    return i
            return None

        i_ticker   = _col(_TICKER_COL_ALIASES) if header else 0
        i_name     = _col(_NAME_COL_ALIASES)
        i_sector   = _col(_SECTOR_COL_ALIASES)
        i_industry = _col(_INDUSTRY_COL_ALIASES)
        i_symtype  = _col(_SYMTYPE_COL_ALIASES)

        if i_ticker is None:
            raise ValueError(
                f"tickers file {path}: no 'ticker' or 'symbol' column found "
                f"in header {rows[0]}"
            )

        def _cell(row: list[str], idx: int | None) -> str | None:
            if idx is None or idx >= len(row):
                return None
            return row[idx].strip() or None

        for row in data_rows:
            if not row:
                continue
            ticker_raw = _cell(row, i_ticker)
            if not ticker_raw:
                continue
            ticker = ticker_raw.upper().strip()
            if not ticker or ticker.lower() in _HEADER_SENTINEL:
                continue  # stray header row
            sym_type = _cell(row, i_symtype) or constants.SYMBOL_TYPE_STOCK
            if sym_type not in constants.ALLOWED_SYMBOL_TYPES:
                sym_type = constants.SYMBOL_TYPE_STOCK
            if sym_type != constants.SYMBOL_TYPE_STOCK:
                continue  # this tool backfills stocks only
            if ticker in seen:
                continue
            seen.add(ticker)
            # Map industry column → sector (the CSV uses "industry" for what is
            # effectively the sector grouping; put it in both fields).
            industry_val = _cell(row, i_industry)
            sector_val = _cell(row, i_sector) or industry_val
            entries.append(
                TickerInfo(
                    ticker=ticker,
                    symbol_type=constants.SYMBOL_TYPE_STOCK,
                    company_name=_cell(row, i_name),
                    sector=sector_val,
                    industry=industry_val,
                )
            )
    else:
        # Plain text: one ticker per line.
        for ln in lines:
            ticker = ln.strip().upper()
            if not ticker or ticker.lower() in _HEADER_SENTINEL:
                continue
            if ticker in seen:
                continue
            seen.add(ticker)
            entries.append(
                TickerInfo(
                    ticker=ticker,
                    symbol_type=constants.SYMBOL_TYPE_STOCK,
                )
            )

    if not entries:
        raise ValueError(
            f"tickers file {path} produced zero valid stock ticker entries"
        )
    return entries


# --------------------------------------------------------------------------- #
# Scoped batch-prefetch provider wrapper.
# --------------------------------------------------------------------------- #
class _ScopedBatchProvider:
    """Provider wrapper that serves one batch from a single prefetch.

    The wrapped real provider is used to download a whole ticker batch in one
    call via ``get_price_history_many`` (when available); the per-ticker
    ``get_price_history`` then replays cached bars with no further network
    access. This lets the frozen Module 08 fetch loop (which calls
    ``get_price_history`` per ticker) benefit from a single multi-ticker vendor
    download per batch, while keeping Module 08's upsert / repair-queue / skip
    semantics intact. It does not use the debug DB or debug run semantics.

    When the wrapped provider has no batch method, this falls back to delegating
    each ``get_price_history`` straight through (the existing per-ticker path).
    """

    def __init__(self, provider: Any, service_result_mod: Any) -> None:
        self._provider = provider
        self._sr = service_result_mod
        self._has_batch = hasattr(provider, "get_price_history_many")
        self._cache: dict[str, list[Any]] = {}
        self._failed_detail: str | None = None
        self.batch_calls = 0

    @property
    def used_batch_download(self) -> bool:
        """Whether a real multi-ticker download path is in use."""
        return self._has_batch

    def prime(self, tickers: list[str], start_date: date, end_date: date) -> Any:
        """Prefetch a batch. Returns the provider ServiceResult (or ``None``)."""
        self._cache = {}
        self._failed_detail = None
        if not self._has_batch:
            return None  # fall back to per-ticker delegation
        self.batch_calls += 1
        result = self._provider.get_price_history_many(
            tickers=list(tickers),
            start_date=start_date,
            end_date=end_date,
        )
        if getattr(result, "status", None) == STATUS_FAILED:
            # Whole-batch transport failure: remember so each per-ticker call
            # reports failed and Module 08 routes the ticker to the repair queue.
            errors = getattr(result, "errors", None) or ["batch download failed"]
            self._failed_detail = "; ".join(errors)
            return result
        self._cache = dict(result.metadata.get(_PROVIDER_BARS_BY_TICKER_KEY, {}))
        return result

    # ---- MarketDataProvider-compatible surface used by Module 08 ---------- #
    def get_price_history(self, request: Any) -> Any:
        """Return cached bars for ``request.ticker`` (no network)."""
        if not self._has_batch:
            return self._provider.get_price_history(request)

        ticker = request.ticker
        if self._failed_detail is not None:
            return self._sr.ServiceResult(
                status=STATUS_FAILED,
                run_id=str(uuid.uuid4()),
                rows_processed=0,
                errors=[f"batch download failed: {self._failed_detail}"],
                metadata={_PROVIDER_BARS_KEY: []},
            )
        bars = self._cache.get(ticker, [])
        status = STATUS_SUCCESS if bars else STATUS_SUCCESS_WITH_WARNINGS
        warnings = [] if bars else [f"No cached bars for {ticker} in batch range."]
        return self._sr.ServiceResult(
            status=status,
            run_id=str(uuid.uuid4()),
            rows_processed=len(bars),
            warnings=warnings,
            metadata={_PROVIDER_BARS_KEY: bars},
        )

    # Pass-throughs (Module 08 only needs get_price_history, but keep the
    # wrapper a faithful provider stand-in).
    def get_capabilities(self) -> Any:
        return self._provider.get_capabilities()

    def list_symbols(self, symbol_type: str | None = None) -> Any:
        return self._provider.list_symbols(symbol_type=symbol_type)

    def get_earnings(self, ticker: str) -> Any:
        return self._provider.get_earnings(ticker)


# --------------------------------------------------------------------------- #
# Backfiller.
# --------------------------------------------------------------------------- #
class Backfiller:
    """Coordinate a historical prod backfill (control plane only).

    All collaborators are injectable so the whole flow is testable offline with
    fakes; ``None`` builds the real default (mirroring how Module 20 constructs
    its engines in ``__init__`` only).
    """

    def __init__(
        self,
        db_manager: Any | None = None,
        provider: Any | None = None,
        benchmark_loader: Any | None = None,
        universe_engine: Any | None = None,
        ingestion_engine: Any | None = None,
        validation_engine: Any | None = None,
        mutation_engine: Any | None = None,
        feature_engine: Any | None = None,
        regime_engine: Any | None = None,
        service_result_mod: Any | None = None,
        sleeper: Callable[[float], None] | None = None,
        jitter_fn: Callable[[float], float] | None = None,
        daily_pipeline_runner: Callable[[date], Any] | None = None,
    ) -> None:
        ensure_repo_root_on_path()
        if db_manager is None:
            from app.database import duckdb_manager

            db_manager = duckdb_manager
        self._db = db_manager

        if service_result_mod is None:
            from app.utils import service_result as service_result_mod
        self._sr = service_result_mod

        if provider is None:
            from app.providers.yahoo_provider import YahooProvider

            provider = YahooProvider()
        self._provider = provider

        if benchmark_loader is None:
            from app.services.benchmarks.benchmark_etf_loader import BenchmarkEtfLoader

            benchmark_loader = BenchmarkEtfLoader(db_manager=self._db)
        self._benchmark_loader = benchmark_loader

        if universe_engine is None:
            from app.services.universe.universe_snapshot import UniverseSnapshotEngine

            universe_engine = UniverseSnapshotEngine(db_manager=self._db)
        self._universe_engine = universe_engine

        if ingestion_engine is None:
            from app.services.ingestion.daily_price_ingestion import (
                DailyPriceIngestionEngine,
            )

            ingestion_engine = DailyPriceIngestionEngine(db_manager=self._db)
        self._ingestion_engine = ingestion_engine

        if validation_engine is None:
            from app.services.validation.data_validator import DataValidator

            validation_engine = DataValidator(db_manager=self._db)
        self._validation_engine = validation_engine

        if mutation_engine is None:
            from app.services.mutation.mutation_detector import MutationDetector

            mutation_engine = MutationDetector(db_manager=self._db)
        self._mutation_engine = mutation_engine

        if feature_engine is None:
            from app.services.features.feature_engine import FeatureEngine

            feature_engine = FeatureEngine(db_manager=self._db)
        self._feature_engine = feature_engine

        if regime_engine is None:
            from app.services.regime.market_regime_engine import MarketRegimeEngine

            regime_engine = MarketRegimeEngine(db_manager=self._db)
        self._regime_engine = regime_engine

        self._sleep = sleeper if sleeper is not None else time.sleep
        self._jitter = jitter_fn if jitter_fn is not None else _default_jitter
        self._daily_runner = daily_pipeline_runner

    # ------------------------------------------------------------------ #
    # Public entry point.
    # ------------------------------------------------------------------ #
    def run(
        self,
        *,
        start_date: date,
        end_date: date,
        batch_size: int = 50,
        sleep_seconds: float = 3.0,
        jitter_seconds: float = 2.0,
        max_retries: int = 3,
        retry_base_sleep: float = 10.0,
        resume: bool = True,
        dry_run: bool = False,
        tickers_file: "Path | None" = None,
        run_validation: bool = True,
        run_mutation: bool = True,
        run_features: bool = True,
        run_regime: bool = True,
        run_daily_pipeline_after: bool = False,
    ) -> Any:
        run_id = str(uuid.uuid4())
        log = logging.getLogger("tools.backfill_prod_history")
        warnings: list[str] = []

        if start_date > end_date:
            return self._result(
                STATUS_FAILED,
                run_id,
                errors=["start-date must be <= end-date"],
                metadata={"start_date": start_date.isoformat(),
                          "end_date": end_date.isoformat()},
            )

        print(
            f"Starting prod historical backfill: start_date={start_date} "
            f"end_date={end_date} batch_size={batch_size} dry_run={dry_run}"
        )

        # 1. Schema must already exist (no DDL here).
        schema_err = self._ensure_prod_schema_exists()
        if schema_err is not None:
            return self._result(STATUS_FAILED, run_id, errors=[schema_err])

        # 2. Universe snapshot for end_date.
        #    Source of truth is the --tickers-file when provided; otherwise we
        #    leave ticker_master untouched and rely on whatever is already there.
        #    We NEVER call provider.list_symbols() here — YahooProvider V1 returns
        #    an empty list without an injected symbol_source, which would silently
        #    produce a zero-ticker run.
        if not dry_run:
            if tickers_file is not None:
                try:
                    file_entries = _load_tickers_from_file(tickers_file)
                except ValueError as exc:
                    return self._result(
                        STATUS_FAILED, run_id,
                        errors=[f"tickers-file error: {exc}"],
                        warnings=warnings,
                    )
                uni = self._universe_engine.apply_snapshot(
                    entries=file_entries,
                    as_of_date=end_date,
                    db_role=DB_ROLE_PROD,
                    source="file",
                    run_id=run_id,
                )
                if not self._ok(uni):
                    # apply_snapshot failure with an explicit tickers-file is a
                    # hard error: the universe cannot be considered reliable and
                    # proceeding would ingest against stale / wrong ticker_master
                    # data. Fail immediately so the operator knows to fix the
                    # file or the DB before retrying.
                    return self._result(
                        STATUS_FAILED,
                        run_id,
                        errors=[
                            "universe snapshot failed (tickers-file provided, "
                            f"cannot continue): {self._errs(uni)}"
                        ],
                        warnings=warnings,
                    )
                print(
                    f"Universe snapshot completed from file: "
                    f"tickers={len(file_entries)} "
                    f"rows={getattr(uni, 'rows_processed', 0)}"
                )
                warnings.extend(self._prefixed("universe", uni))
            else:
                print(
                    "No --tickers-file provided; skipping universe snapshot "
                    "(using existing ticker_master)."
                )
        else:
            print("Universe snapshot skipped (dry-run).")

        # 3. Active stock universe (read-only) — checked BEFORE benchmark so a
        #    zero-ticker DB is caught early without wasting a benchmark download.
        try:
            active = self._select_active_stocks()
        except Exception as exc:  # noqa: BLE001
            return self._result(
                STATUS_FAILED,
                run_id,
                errors=[f"active-ticker read failed: {type(exc).__name__}: {exc}"],
                warnings=warnings,
            )
        print(f"Active stock tickers found: {len(active)}")

        # GUARD: zero-ticker universe is always a misconfiguration — fail loudly
        # before spending time on the benchmark download so the operator gets a
        # clear, immediate signal to supply --tickers-file or pre-populate the DB.
        if len(active) == 0:
            return self._result(
                STATUS_FAILED,
                run_id,
                errors=[
                    "no active stock tickers found in ticker_master; "
                    "provide --tickers-file to populate the universe, "
                    "or run the daily pipeline first"
                ],
                warnings=warnings,
            )

        # 4. Benchmark / sector-ETF backfill for the full range (critical).
        #    Placed after the active-ticker guard so it only runs when the
        #    universe is confirmed non-empty.
        if not dry_run:
            bench = self._benchmark_loader.load(
                provider=self._provider,
                start_date=start_date,
                end_date=end_date,
                db_role=DB_ROLE_PROD,
                run_id=run_id,
            )
            if not self._ok(bench):
                return self._result(
                    STATUS_FAILED,
                    run_id,
                    errors=[f"benchmark backfill failed: {self._errs(bench)}"],
                    warnings=warnings,
                )
            print(
                "Benchmark backfill completed: "
                f"rows={getattr(bench, 'rows_processed', 0)}"
            )
            warnings.extend(self._prefixed("benchmark", bench))
        else:
            print("Benchmark backfill skipped (dry-run).")

        # 5. Resume filtering.
        if resume and active:
            remaining, skipped = self._apply_resume(active, start_date, end_date)
            print(f"Resume enabled: skipped={len(skipped)}, remaining={len(remaining)}")
        else:
            remaining, skipped = active, []
            if not resume:
                print("Resume disabled: processing all active tickers.")

        # 6. Price backfill in batches (dry-run stops here with a plan).
        batches = _split_batches(remaining, batch_size) if remaining else []
        if dry_run:
            print(
                f"DRY-RUN plan: tickers_remaining={len(remaining)} "
                f"batches={len(batches)} batch_size={batch_size} "
                f"(no writes performed)."
            )
            return self._result(
                STATUS_SUCCESS,
                run_id,
                warnings=warnings,
                metadata={
                    "dry_run": True,
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                    "active_tickers": len(active),
                    "tickers_remaining": len(remaining),
                    "tickers_skipped": len(skipped),
                    "batches": len(batches),
                    "batch_size": batch_size,
                },
            )

        price_rows = 0
        batch_failures = 0
        scoped = _ScopedBatchProvider(self._provider, self._sr)
        for index, batch in enumerate(batches, start=1):
            ok, rows, batch_warnings = self._ingest_batch_with_retry(
                scoped=scoped,
                batch=batch,
                start_date=start_date,
                end_date=end_date,
                run_id=run_id,
                max_retries=max_retries,
                retry_base_sleep=retry_base_sleep,
                jitter_seconds=jitter_seconds,
                log=log,
            )
            price_rows += rows
            warnings.extend(batch_warnings)
            status_word = "ok" if ok else "failed"
            if not ok:
                batch_failures += 1
            print(
                f"Price backfill batch {index}/{len(batches)} "
                f"tickers={len(batch)} status={status_word} rows={rows}"
            )
            if index < len(batches):
                delay = sleep_seconds + self._jitter(jitter_seconds)
                print(f"Sleeping {delay:.1f}s before next batch...")
                self._sleep(delay)

        if batches and batch_failures == len(batches):
            return self._result(
                STATUS_FAILED,
                run_id,
                errors=["all price-backfill batches failed"],
                warnings=warnings,
                metadata={"price_rows_written": price_rows},
            )

        # 7. Range-wide validation / mutation / features / regime.
        if run_validation:
            res = self._validation_engine.validate(
                start_date=start_date, end_date=end_date,
                db_role=DB_ROLE_PROD, run_id=run_id,
            )
            if not self._ok(res):
                return self._result(
                    STATUS_FAILED, run_id,
                    errors=[f"validation failed: {self._errs(res)}"],
                    warnings=warnings,
                )
            print(f"Validation completed: rows={getattr(res, 'rows_processed', 0)}")
            warnings.extend(self._prefixed("validation", res))

        if run_mutation:
            res = self._mutation_engine.detect(
                start_date=start_date, end_date=end_date,
                db_role=DB_ROLE_PROD, run_id=run_id,
            )
            if not self._ok(res):
                warnings.append(f"mutation detection failed (recoverable): {self._errs(res)}")
            else:
                print(f"Mutation detection completed: rows={getattr(res, 'rows_processed', 0)}")
                warnings.extend(self._prefixed("mutation", res))

        if run_features:
            res = self._feature_engine.calculate(
                start_date=start_date, end_date=end_date, tickers=None,
                db_role=DB_ROLE_PROD, run_id=run_id,
            )
            if not self._ok(res):
                return self._result(
                    STATUS_FAILED, run_id,
                    errors=[f"feature calculation failed: {self._errs(res)}"],
                    warnings=warnings,
                )
            print(f"Feature calculation completed: rows={getattr(res, 'rows_processed', 0)}")
            warnings.extend(self._prefixed("features", res))

        if run_regime:
            res = self._regime_engine.classify(
                start_date=start_date, end_date=end_date,
                db_role=DB_ROLE_PROD, run_id=run_id,
            )
            if not self._ok(res):
                warnings.append(f"market regime failed (recoverable): {self._errs(res)}")
            else:
                print(f"Market regime completed: rows={getattr(res, 'rows_processed', 0)}")
                warnings.extend(self._prefixed("regime", res))

        # Optional: hand off to the normal daily pipeline for the final date.
        if run_daily_pipeline_after:
            print(f"Running normal daily pipeline for {end_date} after backfill...")
            daily = self._run_daily_pipeline(end_date)
            if not self._ok(daily):
                warnings.append(f"post-backfill daily pipeline failed: {self._errs(daily)}")

        status = STATUS_SUCCESS_WITH_WARNINGS if warnings else STATUS_SUCCESS
        return self._result(
            status,
            run_id,
            warnings=warnings,
            metadata={
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "active_tickers": len(active),
                "tickers_skipped": len(skipped),
                "batches": len(batches),
                "batch_failures": batch_failures,
                "price_rows_written": price_rows,
                "used_batch_download": scoped.used_batch_download,
            },
        )

    # ------------------------------------------------------------------ #
    # Batch ingest with retry / backoff.
    # ------------------------------------------------------------------ #
    def _ingest_batch_with_retry(
        self,
        *,
        scoped: _ScopedBatchProvider,
        batch: list[str],
        start_date: date,
        end_date: date,
        run_id: str,
        max_retries: int,
        retry_base_sleep: float,
        jitter_seconds: float,
        log: Any,
    ) -> tuple[bool, int, list[str]]:
        """Prefetch + ingest one batch, retrying transient provider failures.

        Backoff after attempt ``n`` (0-indexed) is
        ``retry_base_sleep * 2**n + jitter`` (e.g. ~10–12s, ~20–22s, ~40–42s for
        the default base of 10s). Returns ``(ok, price_rows_written, warnings)``.
        """
        warnings: list[str] = []
        attempt = 0
        while True:
            prime_result = scoped.prime(batch, start_date, end_date)
            transient = (
                prime_result is not None
                and getattr(prime_result, "status", None) == STATUS_FAILED
            )
            if not transient:
                result = self._ingestion_engine.ingest(
                    provider=scoped,
                    start_date=start_date,
                    end_date=end_date,
                    db_role=DB_ROLE_PROD,
                    run_id=run_id,
                    tickers=batch,
                )
                rows = getattr(result, "rows_processed", 0)
                warnings.extend(getattr(result, "warnings", []) or [])
                if self._ok(result):
                    return True, rows, warnings
                # Module 08 only fails on a guard or a DB write error — not a
                # transient vendor blip — so do not retry; report and continue.
                warnings.append(f"batch ingest failed: {self._errs(result)}")
                return False, rows, warnings

            # Transient provider/throttling failure: back off and retry.
            if attempt >= max_retries:
                warnings.append(
                    f"batch of {len(batch)} ticker(s) still failing after "
                    f"{max_retries} retries: {self._errs(prime_result)}; "
                    "relying on repair queue."
                )
                # One final ingest so failed tickers land in the repair queue.
                result = self._ingestion_engine.ingest(
                    provider=scoped, start_date=start_date, end_date=end_date,
                    db_role=DB_ROLE_PROD, run_id=run_id, tickers=batch,
                )
                warnings.extend(getattr(result, "warnings", []) or [])
                return False, getattr(result, "rows_processed", 0), warnings

            delay = retry_base_sleep * (2 ** attempt) + self._jitter(jitter_seconds)
            log.warning(
                "batch download failed (attempt %d/%d); backing off %.1fs",
                attempt + 1, max_retries, delay,
            )
            print(f"Batch download failed; retrying in {delay:.1f}s...")
            self._sleep(delay)
            attempt += 1

    # ------------------------------------------------------------------ #
    # DB helpers (read-only; through the approved manager only).
    # ------------------------------------------------------------------ #
    def _ensure_prod_schema_exists(self) -> str | None:
        """Return an error string if the prod schema is missing, else ``None``."""
        try:
            conn = self._db.connect(DB_ROLE_PROD, read_only=True)
        except Exception as exc:  # noqa: BLE001 - missing DB file, etc.
            return (
                "prod database is not initialized "
                f"({type(exc).__name__}: {exc}). Run tools/init_prod_db.py first."
            )
        try:
            rows = conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'main' AND table_name = 'ticker_master'"
            ).fetchall()
        except Exception as exc:  # noqa: BLE001
            return f"could not inspect prod schema: {type(exc).__name__}: {exc}"
        finally:
            conn.close()
        if not rows:
            return (
                "prod schema missing required table 'ticker_master'. "
                "Run tools/init_prod_db.py first."
            )
        return None

    def _select_active_stocks(self) -> list[str]:
        conn = self._db.connect(DB_ROLE_PROD, read_only=True)
        try:
            rows = conn.execute(
                "SELECT ticker FROM ticker_master "
                "WHERE symbol_type = 'stock' AND active_flag = TRUE "
                "ORDER BY ticker"
            ).fetchall()
        finally:
            conn.close()
        return [row[0] for row in rows]

    def _apply_resume(
        self, tickers: list[str], start_date: date, end_date: date
    ) -> tuple[list[str], list[str]]:
        """Return ``(remaining, skipped)`` based on existing ``daily_prices``.

        A ticker is skipped only when its stored coverage already spans the full
        requested range (``min_date <= start`` and ``max_date >= end``) and has a
        non-trivial row count. Anything uncertain is reprocessed (idempotent
        upserts make this safe).
        """
        coverage = self._coverage(tickers, start_date, end_date)
        remaining: list[str] = []
        skipped: list[str] = []
        for ticker in tickers:
            rows, min_date, max_date = coverage.get(ticker, (0, None, None))
            if (
                rows >= _RESUME_MIN_ROWS
                and min_date is not None
                and max_date is not None
                and min_date <= start_date
                and max_date >= end_date
            ):
                skipped.append(ticker)
            else:
                remaining.append(ticker)
        return remaining, skipped

    def _coverage(
        self, tickers: list[str], start_date: date, end_date: date
    ) -> dict[str, tuple[int, Any, Any]]:
        """Read per-ticker ``daily_prices`` coverage for the range (read-only)."""
        if not tickers:
            return {}
        placeholders = ", ".join(["?"] * len(tickers))
        sql = (
            "SELECT ticker, COUNT(*) AS rows, MIN(date) AS min_date, "
            "MAX(date) AS max_date FROM daily_prices "
            f"WHERE ticker IN ({placeholders}) AND date >= ? AND date <= ? "
            "GROUP BY ticker"
        )
        params = [*tickers, start_date, end_date]
        conn = self._db.connect(DB_ROLE_PROD, read_only=True)
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        out: dict[str, tuple[int, Any, Any]] = {}
        for row in rows:
            out[row[0]] = (int(row[1]), row[2], row[3])
        return out

    def _run_daily_pipeline(self, end_date: date) -> Any:
        if self._daily_runner is not None:
            return self._daily_runner(end_date)
        from app.services.pipeline.pipeline_orchestrator import PipelineOrchestrator

        return PipelineOrchestrator().run(
            run_date=end_date,
            run_type="force_rerun",
            db_role=DB_ROLE_PROD,
            force_rerun=True,
        )

    # ------------------------------------------------------------------ #
    # ServiceResult helpers.
    # ------------------------------------------------------------------ #
    @staticmethod
    def _ok(result: Any) -> bool:
        if hasattr(result, "is_ok"):
            return bool(result.is_ok())
        return getattr(result, "status", STATUS_FAILED) in (
            STATUS_SUCCESS, STATUS_SUCCESS_WITH_WARNINGS,
        )

    @staticmethod
    def _errs(result: Any) -> str:
        return "; ".join(getattr(result, "errors", []) or []) or "unknown error"

    @staticmethod
    def _prefixed(prefix: str, result: Any) -> list[str]:
        return [f"{prefix}: {w}" for w in (getattr(result, "warnings", []) or [])]

    def _result(
        self,
        status: str,
        run_id: str,
        *,
        warnings: list[str] | None = None,
        errors: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        return self._sr.ServiceResult(
            status=status,
            run_id=run_id,
            rows_processed=(metadata or {}).get("price_rows_written", 0),
            warnings=warnings or [],
            errors=errors or [],
            metadata=metadata or {},
        )


# --------------------------------------------------------------------------- #
# CLI.
# --------------------------------------------------------------------------- #
def _build_backfiller() -> Backfiller:
    """Construct the real Backfiller (isolated so tests can monkeypatch it)."""
    ensure_repo_root_on_path()
    return Backfiller()


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="backfill_prod_history",
        description="Backfill prod.duckdb with historical market data over a range.",
    )
    parser.add_argument("--start-date", dest="start_date", type=date.fromisoformat,
                        required=True, help="Inclusive range start (YYYY-MM-DD).")
    parser.add_argument("--end-date", dest="end_date", type=date.fromisoformat,
                        required=True, help="Inclusive range end (YYYY-MM-DD).")
    parser.add_argument(
        "--tickers-file", dest="tickers_file", default=None,
        metavar="PATH",
        help=(
            "Path to CSV or plain-text file listing stock tickers to backfill. "
            "When provided the tool loads the universe from this file and upserts "
            "it into ticker_master via Module 06 before ingesting prices. "
            "When omitted the tool uses whatever is already in ticker_master; "
            "if ticker_master is empty the run fails with a clear error."
        ),
    )
    parser.add_argument("--batch-size", type=int, default=50,
                        help="Tickers per price batch (default: 50).")
    parser.add_argument("--sleep-seconds", type=float, default=3.0,
                        help="Base sleep between batches (default: 3.0).")
    parser.add_argument("--jitter-seconds", type=float, default=2.0,
                        help="Max added random jitter (default: 2.0).")
    parser.add_argument("--max-retries", type=int, default=3,
                        help="Max retries per failing batch (default: 3).")
    parser.add_argument("--retry-base-sleep", type=float, default=10.0,
                        help="Base backoff seconds for retries (default: 10.0).")
    parser.add_argument("--resume", dest="resume", action="store_true", default=True,
                        help="Skip tickers already fully covered (default: on).")
    parser.add_argument("--no-resume", dest="resume", action="store_false",
                        help="Reprocess all active tickers.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Plan only; perform no writes.")
    parser.add_argument("--run-validation", dest="run_validation",
                        action="store_true", default=True)
    parser.add_argument("--no-run-validation", dest="run_validation",
                        action="store_false")
    parser.add_argument("--run-mutation", dest="run_mutation",
                        action="store_true", default=True)
    parser.add_argument("--no-run-mutation", dest="run_mutation", action="store_false")
    parser.add_argument("--run-features", dest="run_features",
                        action="store_true", default=True)
    parser.add_argument("--no-run-features", dest="run_features", action="store_false")
    parser.add_argument("--run-regime", dest="run_regime",
                        action="store_true", default=True)
    parser.add_argument("--no-run-regime", dest="run_regime", action="store_false")
    parser.add_argument("--run-daily-pipeline-after", dest="run_daily_pipeline_after",
                        action="store_true",
                        help="Run the normal daily pipeline for end_date afterward.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.start_date > args.end_date:
        print("ERROR: start-date must be <= end-date")
        return 1

    tickers_file: Path | None = None
    if args.tickers_file is not None:
        tickers_file = Path(args.tickers_file).resolve()
        if not tickers_file.exists():
            print(f"ERROR: --tickers-file not found: {tickers_file}")
            return 1

    try:
        backfiller = _build_backfiller()
        result = backfiller.run(
            start_date=args.start_date,
            end_date=args.end_date,
            batch_size=args.batch_size,
            sleep_seconds=args.sleep_seconds,
            jitter_seconds=args.jitter_seconds,
            max_retries=args.max_retries,
            retry_base_sleep=args.retry_base_sleep,
            resume=args.resume,
            dry_run=args.dry_run,
            tickers_file=tickers_file,
            run_validation=args.run_validation,
            run_mutation=args.run_mutation,
            run_features=args.run_features,
            run_regime=args.run_regime,
            run_daily_pipeline_after=args.run_daily_pipeline_after,
        )
    except Exception as exc:  # noqa: BLE001 - operator script reports any failure
        print(f"FAILURE: prod historical backfill raised an exception: {exc}")
        logger.exception("prod historical backfill failed")
        return 1

    status = getattr(result, "status", STATUS_FAILED)
    metadata = getattr(result, "metadata", {}) or {}
    is_ok = status in (STATUS_SUCCESS, STATUS_SUCCESS_WITH_WARNINGS)

    if is_ok:
        if status == STATUS_SUCCESS_WITH_WARNINGS:
            print(
                "SUCCESS_WITH_WARNINGS: prod historical backfill completed "
                f"(price_rows_written={metadata.get('price_rows_written', 0)})."
            )
            for warn in getattr(result, "warnings", []) or []:
                print(f"  warning: {warn}")
        else:
            print(
                "SUCCESS: prod historical backfill completed "
                f"(price_rows_written={metadata.get('price_rows_written', 0)})."
            )
        return 0

    print(f"FAILURE: prod historical backfill status={status}; "
          f"errors={getattr(result, 'errors', [])}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
