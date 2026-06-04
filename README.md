# Stock AI Platform — Swing Trading Stock Analyzer

Local Windows-based US daily swing trading **research-grade V1** platform.
DuckDB local files, Polars-first processing, Streamlit dashboard (added in a
later module). This repository follows the shared source-of-truth documents in
`docs/` (`MASTER_SPEC.md`, `ARCHITECTURE.md`, `DECISIONS_LOG.md`,
`TODO_ROADMAP.md`, `CODING_STANDARDS.md`).

> **Safety note.** This system is research support only. It does not provide
> guaranteed trading predictions. There is no auto-trading and no broker
> connection in V1.

## Current state: Modules 01 — 03

This repository currently implements **Module 01 (Project Skeleton)**,
**Module 02 (DuckDB Manager)**, and **Module 03 (Schema Manager)**. Module 01
establishes the project structure, configuration loading, constants, logging,
and the shared `ServiceResult` contract. Module 02 adds centralized DuckDB
connection management for the three approved database roles (`prod`, `debug`,
`simulation`). Module 03 creates the final merged DuckDB schema on those
databases. Module 04 adds the abstract provider interface (contract only).
Per the roadmap, no provider calls, trading logic, simulation logic, or
dashboard exist yet — the provider interface lands in Module 04; provider
calls land in Module 05 and beyond.

## Requirements

- Python 3.11+
- Windows local PC (paths use `pathlib`, so other OSes work for development)

## Project structure

```text
stock_ai_platform/
  app/
    config/
      settings.py      # pathlib paths + immutable strategy presets
      constants.py     # domain vocabulary + FEATURE_SCHEMA_VERSION
      env.py           # python-dotenv loading + typed getters
    database/
      duckdb_manager.py  # centralized DuckDB connection manager (Module 02)
    utils/
      service_result.py  # shared ServiceResult dataclass contract
      logging_config.py   # run_id-aware logging (timestamp | level | module | run_id | message)
  data/
    duckdb/            # prod/debug/simulation DuckDB files (created on first connect)
    logs/
    exports/
    backups/
  tests/
    test_project_skeleton.py
    test_duckdb_manager.py
  docs/                # source-of-truth specification documents
  pyproject.toml
  requirements.txt
  .env.example
  README.md
```

## Setup

1. Create and activate a virtual environment.

   ```bat
   py -3.11 -m venv .venv
   .venv\Scripts\activate
   ```

   On POSIX shells:

   ```bash
   python3.11 -m venv .venv
   source .venv/bin/activate
   ```

2. Install dependencies.

   ```bash
   pip install -r requirements.txt
   ```

   Or install the project (with dev extras) from `pyproject.toml`:

   ```bash
   pip install -e ".[dev]"
   ```

3. Create your local environment file (optional; defaults work without it).

   ```bat
   copy .env.example .env
   ```

   ```bash
   cp .env.example .env
   ```

## Configuration overview

- `app/config/constants.py` holds immutable domain constants, including
  `FEATURE_SCHEMA_VERSION = "features_v01"`, symbol types, benchmark symbols,
  market regimes, setup types, and outcome horizons.
- `app/config/settings.py` computes all filesystem paths with `pathlib` and
  exposes immutable strategy presets (`normal`, `aggressive`, `conservative`)
  drawn from `MASTER_SPEC.md`.
- `app/config/env.py` loads `.env` via `python-dotenv` and provides typed
  getters (`get_str`, `get_int`, `get_float`, `get_bool`, `get_path`).
- `app/utils/service_result.py` defines the `ServiceResult` dataclass returned
  by every service module in later phases.
- `app/utils/logging_config.py` configures logging in the required format and
  binds a `run_id` to each record.

To create the data directories on demand:

```python
from app.config import settings
settings.ensure_directories()
```

## Running the tests

From the project root (`stock_ai_platform/`):

```bash
pytest
```

For verbose output:

```bash
pytest -v
```

With coverage of the `app` package:

```bash
pytest --cov=app
```

A `conftest.py` at the project root puts the repository on `sys.path`, so the
tests run without an editable install. If you prefer, `pip install -e .` also
makes the `app` package importable.

## Module 02 — DuckDB Manager (usage)

Module 02 centralizes DuckDB access. All other code in the platform must go
through this manager and must never open arbitrary DB files directly
(`CODING_STANDARDS.md` section 8).

The manager accepts only the three approved database *roles* — `prod`,
`debug`, `simulation` — and resolves their file paths from
`app.config.settings` at call time (so tests and environment overrides take
effect). It does not create schema tables; that is Module 03.

```python
from app.database import duckdb_manager

# Open a writable production connection.
with duckdb_manager.connect_prod() as conn:
    ...

# Open the debug DB read-only (file must already exist).
with duckdb_manager.connect_debug(read_only=True) as conn:
    ...

# Simulation reading from prod safely (prod is attached READ_ONLY).
with duckdb_manager.connect_simulation_with_prod() as sim:
    sim.execute("SELECT * FROM prod.some_table")  # ok
    # sim.execute("INSERT INTO prod.some_table ...")  # fails: prod is read-only
```

Helpers:

- `connect(db_role, read_only=False)` — generic role-based connect.
- `connect_prod`, `connect_debug`, `connect_simulation` — role-specific.
- `get_database_path(db_role)` — resolve the DB path from settings.
- `ensure_database_directory()` — idempotently create `data/duckdb/`.
- `attach_prod_read_only(connection, alias="prod")` — attach prod read-only
  using only `settings.PROD_DB_PATH` (no arbitrary path argument).
- `connect_simulation_with_prod(read_only=False, prod_alias="prod")` —
  convenience wrapper around the above two.

## Module 03 — Schema Manager (usage)

Module 03 creates the **final merged DuckDB schema** directly from
`docs/SCHEMA_SPEC.md` (Master TZ v1 + PATCH 1 + MINI-PATCH 2, already merged).
It goes through Module 02 for every connection and never opens DB files
directly, never runs `ALTER TABLE`, and never creates DuckDB `ENUM` types.

```python
from app.database import schema_manager

# Apply the production schema (20 tables, 9 indexes, 2 views) to prod.duckdb.
schema_manager.apply_prod_schema()

# debug.duckdb gets the identical production schema.
schema_manager.apply_debug_schema()

# simulation.duckdb gets the narrower sim_* schema (9 tables, 2 indexes, 0 views).
schema_manager.apply_simulation_schema()

# Generic role-based entry point (role: "prod" | "debug" | "simulation").
result = schema_manager.apply_schema("prod")
assert result.is_ok()
print(result.metadata["tables_created"], result.metadata["schema_version"])
```

All four entry points return a `ServiceResult`. Schema creation is idempotent:
calling it twice does not raise, does not duplicate tables or indexes, and does
not duplicate the single `schema_versions` seed row (one row per database,
keyed on `(schema_name, 'schema_v01')`). The database schema version
(`schema_v01`) is distinct from the per-row feature schema version
(`features_v01` from `constants.FEATURE_SCHEMA_VERSION`); Module 03 creates the
`daily_features.feature_schema_version` column but does not populate feature
rows.

## Module 04 — Provider Interface (usage)

Module 04 defines the **abstract, provider-neutral market-data contract** in
`app/providers/provider_interface.py`. It is interface-only: no `yfinance`, no
network client, no DuckDB access, and no concrete provider. The concrete
YahooProvider is Module 05.

The contract is `MarketDataProvider` (an `abc.ABC`) plus six frozen DTOs
(`PriceBar`, `PriceHistoryRequest`, `TickerInfo`, `EarningsEvent`,
`ProviderCapabilities`, `ProviderErrorDetail`) and the error vocabulary
`PROVIDER_ERROR_KINDS`. Every data-fetching method returns a `ServiceResult`
with the domain DTOs carried in `metadata` under stable keys
(`capabilities` / `bars` / `symbols` / `events`, plus `error_detail` on failure
and `provider_name` on every call).

```python
from datetime import date

from app.providers import MarketDataProvider, PriceHistoryRequest
from app.utils.service_result import ServiceResult

# A future concrete provider (Module 05) implements all four methods. This is
# illustrative only — no real network/yfinance calls live in Module 04.
class ExampleProvider(MarketDataProvider):
    def get_capabilities(self) -> ServiceResult: ...
    def get_price_history(self, request: PriceHistoryRequest) -> ServiceResult: ...
    def list_symbols(self, symbol_type: str | None = None) -> ServiceResult: ...
    def get_earnings(self, ticker: str) -> ServiceResult: ...

request = PriceHistoryRequest(
    ticker="AAPL",
    start_date=date(2024, 1, 1),
    end_date=date(2024, 1, 31),  # inclusive range; start_date <= end_date enforced
)
```

`symbol_type` validates against `constants.ALLOWED_SYMBOL_TYPES`. Empty results
are `success` + empty list + `rows_processed == 0` (not an error); error
conditions return `failed` with a `ProviderErrorDetail` whose `kind` is one of
`PROVIDER_ERROR_KINDS`. `MarketDataProvider()` cannot be instantiated directly,
and a subclass that omits a method raises `TypeError`.

## Module 05 — YahooProvider (usage)

Module 05 adds the first **concrete** provider, `YahooProvider`, in
`app/providers/yahoo_provider.py`. It implements the frozen Module 04
`MarketDataProvider` contract by fetching daily prices, ticker metadata, and
earnings dates from Yahoo via `yfinance`. All Yahoo / `yfinance` access is
confined to this single file — no other module imports `yfinance`.

A caller obtains a provider and calls it; every method returns a
`ServiceResult` with the DTOs in `metadata` (`bars` / `symbols` / `events` /
`capabilities`, plus `error_detail` on failure and `provider_name == "yahoo"`
on every call):

```python
from datetime import date

from app.providers import YahooProvider, PriceHistoryRequest

# Production: yfinance is imported lazily inside __init__ (no network at
# construction). Tests inject a deterministic fake instead — see below.
provider = YahooProvider()

request = PriceHistoryRequest(
    ticker="AAPL",
    start_date=date(2024, 1, 1),
    end_date=date(2024, 1, 31),  # inclusive on both ends
)
result = provider.get_price_history(request)
if result.is_ok():
    bars = result.metadata["bars"]  # list[PriceBar], raw + adjusted OHLC
```

The inclusive `[start_date, end_date]` range is honored even though yfinance
treats `end` as exclusive: the provider calls yfinance internally with
`end = end_date + 1 day`. Adjusted OHLC is derived per row from
`Adj Close / Close`; `^VIX` keeps `close_raw == close_adj`. An empty result is a
`success` with an empty list (not an error); unknown symbol / throttling /
outage map to a `failed` `ServiceResult` with a `ProviderErrorDetail` whose
`kind` is one of `PROVIDER_ERROR_KINDS`. In V1 `list_symbols()` does not scrape
Yahoo (it returns an empty success unless a static source is injected), and
`get_earnings()` is best-effort (empty success when no reliable date is found).

For **offline tests**, inject a fake `yfinance`-like dependency (and, optionally,
a static symbol source) through the constructor — no real network calls:

```python
provider = YahooProvider(yf_module=fake_yfinance)  # fake.Ticker(...).history(...)
```

## Module 06 — Universe Snapshot Engine (usage)

`UniverseSnapshotEngine.apply_snapshot` takes provider-neutral `TickerInfo`
entries (it does not fetch them), upserts them into `ticker_master`, manages the
lifecycle flags, and writes one immutable row per ticker into
`ticker_universe_snapshot` for the snapshot month. All DB access goes through
the Module 02 `duckdb_manager` (`prod` / `debug` only — never `simulation`).

```python
from datetime import date

from app.services.universe import UniverseSnapshotEngine
from app.providers import TickerInfo

engine = UniverseSnapshotEngine()
result = engine.apply_snapshot(
    [TickerInfo(ticker="AAPL", symbol_type="stock", sector="Technology")],
    as_of_date=date(2024, 3, 10),   # normalized to snapshot_month 2024-03-01
    db_role="prod",                 # "prod" | "debug"
    source="manual",               # written verbatim to the snapshot row
)
# result.rows_processed == snapshot rows written; result.metadata carries
# input_rows / valid_rows / skipped_rows / tickers_inserted / tickers_updated /
# tickers_marked_inactive / snapshot_rows / snapshot_month / db_role / source.
```

The month write is idempotent (delete-then-insert inside one transaction);
re-running a month never duplicates rows. New tickers start
`active_flag = TRUE, delisted_flag = FALSE` with `first_seen = last_seen =
snapshot_month`; tickers absent from a later input are set `active_flag = FALSE`
(but **not** `delisted_flag`, since absence alone is not delisting).
`yahoo_symbol` equals `ticker` (V1 identity) and `market_cap_bucket` is always
`NULL` in V1. See `M06_UNIVERSE_SNAPSHOT_SPEC.md`.

## Module 07 — Benchmark / Sector ETF Loader (usage)

`BenchmarkEtfLoader.load` loads benchmark, index, and sector-ETF price history
before the feature engine. It reads the symbol set from
`constants.REQUIRED_BENCHMARK_SYMBOLS`, fetches bars **only** through the
Module 04 `MarketDataProvider` interface, upserts them into `daily_prices`
keyed by `(ticker, date)`, upserts each loaded symbol into `ticker_master`
(without clobbering Module-06-owned fields), and seeds `sector_etf_map` from
`constants.SECTOR_ETF_MAP` with insert-or-ignore semantics. All DB access goes
through the Module 02 `duckdb_manager` (`prod` / `debug` only — never
`simulation`).

```python
from datetime import date

from app.providers import YahooProvider  # any MarketDataProvider
from app.services.benchmarks import BenchmarkEtfLoader

loader = BenchmarkEtfLoader()
result = loader.load(
    provider=YahooProvider(),
    start_date=date(2024, 1, 1),
    end_date=date(2024, 3, 31),
    db_role="prod",                 # "prod" | "debug"
)
# result.rows_processed == price rows written; result.metadata carries
# db_role / start_date / end_date / symbols_requested / symbols_loaded /
# symbols_skipped / price_rows_written / ticker_master_upserted /
# sector_etf_map_seeded.
```

Classification is locked: `SPY`/`QQQ` → `benchmark`, `^VIX` → `index`, sector
SPDRs → `etf`. For `^VIX`, `close_raw` mirrors `close_adj` and `volume_raw` is
`NULL`. On every written bar, `volume_adj` and `adjustment_factor` are `NULL`
(Module 10 owns adjustment), `data_quality_status` is `"ok"` (Module 09 owns
validation), and `mutation_flag` is `FALSE`. A per-symbol provider failure or
zero bars is a non-fatal warning (the symbol is skipped); all writes run inside
one transaction that rolls back on error. See
`M07_BENCHMARK_ETF_LOADER_SPEC.md`.

## Module 08 — Daily Price Ingestion (usage)

`DailyPriceIngestionEngine.ingest` downloads and updates daily OHLCV prices for
all active stock-universe tickers before the feature engine. It reads the active
tickers from `ticker_master` (`symbol_type = 'stock' AND active_flag = TRUE`,
never hardcoded), fetches bars **only** through the Module 04
`MarketDataProvider` interface, upserts them into `daily_prices` keyed by
`(ticker, date)`, and enqueues failed / empty-result tickers into
`data_repair_queue` (insert-or-ignore on `(ticker, repair_date, repair_reason)`).
It is the stock-universe equivalent of Module 07. `ticker_master` is read-only
here. All DB access goes through the Module 02 `duckdb_manager` (`prod` /
`debug` only — never `simulation`).

```python
from datetime import date

from app.providers import YahooProvider  # any MarketDataProvider
from app.services.ingestion import DailyPriceIngestionEngine

engine = DailyPriceIngestionEngine()
result = engine.ingest(
    provider=YahooProvider(),
    start_date=date(2024, 1, 1),
    end_date=date(2024, 3, 31),
    db_role="prod",                 # "prod" | "debug"
)
# result.rows_processed == price rows written; result.metadata carries
# db_role / start_date / end_date / tickers_requested / tickers_loaded /
# tickers_skipped / price_rows_written / repair_queue_enqueued.
```

On every written bar, `volume_adj` and `adjustment_factor` are `NULL` (Module 10
owns adjustment), `data_quality_status` is `"ok"` (Module 09 owns validation),
and `mutation_flag` is `FALSE`; missing `dividend_amount` / `split_ratio` default
to `0` / `1`. A per-ticker provider failure, exception, missing
`metadata['bars']`, or zero bars is a non-fatal warning: the ticker is enqueued
for repair (`repair_reason = "missing_price"`, `status = "pending"`) and skipped.
Module 08 only enqueues repairs — it never processes them. All writes run inside
one transaction that rolls back on error. See
`M08_DAILY_PRICE_INGESTION_SPEC.md`.

## Module 09 — Data Validator (usage)

`DataValidator.validate` validates already-ingested `daily_prices` rows for an
inclusive `[start_date, end_date]` range (pipeline step 6, after Module 08
ingestion and before Module 10 mutation detection / Module 11 features). It owns
the real `daily_prices.data_quality_status` (Module 08 wrote the placeholder
`"ok"`) and enqueues validation repairs into `data_repair_queue`. It never calls
a provider — it validates DB rows only. All DB access goes through the Module 02
`duckdb_manager` (`prod` / `debug` only — never `simulation`).

```python
from datetime import date

from app.services.validation import DataValidator

engine = DataValidator()
result = engine.validate(
    start_date=date(2024, 1, 1),
    end_date=date(2024, 3, 31),
    db_role="prod",                 # "prod" | "debug"
)
# result.rows_processed == rows validated; result.metadata carries
# db_role / start_date / end_date / rows_validated / rows_ok / rows_failed /
# status_updates_written / repair_queue_enqueued.
```

Implemented checks are structural OHLCV invariants (null OHLC, `high < low`,
open/close outside `[low, high]`, non-positive price, negative volume) over both
the raw and adjusted tuples. A failing row is escalated to
`data_quality_status = "failed"` using a strict **no-downgrade** rule (a worse
status such as `quarantined` is never lowered) and gets one
`data_repair_queue` row (`repair_reason = "bad_ohlc"`, `status = "pending"`)
deduplicated by a deterministic `repair_id` (the Module 08 pattern). Module 09
only inserts repairs — it never processes them — and never modifies price
values, OHLCV raw/adjusted columns, dividend/split fields, `adjustment_factor`,
or `mutation_flag`. Threshold- or calendar-dependent checks (missing trading
days, large jumps, stale coverage) are intentionally left as documented open
spec gaps rather than invented. All writes run inside one transaction that rolls
back on error. See `M09_DATA_VALIDATOR_SPEC.md`.

## Module 10 — Mutation Detector (usage)

`MutationDetector.detect` scans already-ingested `daily_prices` rows for an
inclusive `[start_date, end_date]` range (after Module 09 validation, before
Module 11 features). It owns `daily_prices.adjustment_factor` derivation and
`daily_prices.mutation_flag`. It never calls a provider — it operates on DB rows
only. All DB access goes through the Module 02 `duckdb_manager` (`prod` /
`debug` only — never `simulation`).

```python
from datetime import date

from app.services.mutation import MutationDetector

engine = MutationDetector()
result = engine.detect(
    start_date=date(2024, 1, 1),
    end_date=date(2024, 3, 31),
    db_role="prod",                 # "prod" | "debug"
)
# result.rows_processed == eligible rows; result.metadata carries db_role /
# start_date / end_date / rows_read / rows_processed / rows_skipped_non_ok /
# adjustment_factors_written / mutation_rows_detected / mutation_flags_written /
# tickers_with_mutation / repair_queue_enqueued / rebuild_logs_enqueued.
```

For each eligible (`data_quality_status = "ok"`) row it derives
`adjustment_factor = close_adj / close_raw` (NULL when underivable; an existing
value may be cleared) and detects explicit splits (`split_ratio != 1`), setting
`mutation_flag = TRUE` under a strict **no-downgrade** rule. Each eligible ticker
with a detected mutation gets one `data_repair_queue` row
(`repair_reason = "mutation"`) and one `feature_rebuild_log` row, keyed on the
ticker's earliest detected mutation date and deduplicated by deterministic
`uuid5` ids (insert-or-ignore — the Module 08 pattern). Module 10 only inserts
those rows; it never processes them, never modifies price/split/dividend/status
columns, and never touches the simulation DB. Historical `close_raw/close_adj`
ratio-discontinuity detection is left as documented open spec gap `G1` (no
threshold defined). All writes run inside one transaction that rolls back on
error. See `M10_MUTATION_DETECTOR_SPEC.md`.

## Module 11 — Feature Engine (usage)

Module 11 (`app/services/features/feature_engine.py`) runs after Module 10 and
before Module 12. `FeatureEngine().calculate(start_date, end_date,
tickers=None, db_role="prod", run_id=None)` reads eligible `daily_prices` rows
(`data_quality_status = 'ok'`, plus warmup history and the mapped sector ETF
rows), computes the `daily_features` indicators with Polars strictly from the
frozen formulas (adjusted prices for price indicators, raw volume for volume
features), and upserts one row per processed ticker — anchored on that ticker's
`feature_cutoff_date` (the latest eligible in-range date, so no look-ahead) — on
`(ticker, feature_date, feature_schema_version)`. `feature_ready = TRUE` only
when every required indicator is non-null. `calculated_at` is refreshed on every
upsert; reruns are idempotent. `db_role` accepts only `prod`/`debug`. The module
writes only `daily_features` (no provider/network, no `duckdb`/`ATTACH`/DDL).
`market_regime` (Module 12) and the earnings/macro context columns are left at
documented defaults (NULL / NULL / NULL / FALSE) as open gaps. See
`M11_FEATURE_ENGINE_SPEC.md`.

## Module 12 — Market Regime Engine (usage)

Module 12 (`app/services/regime/market_regime_engine.py`) runs after Module 11
and before Module 13. `MarketRegimeEngine().classify(start_date, end_date,
db_role="prod", run_id=None)` reads eligible `daily_prices` rows
(`data_quality_status = 'ok'`, plus warmup) for `SPY`/`QQQ`/`^VIX`, computes
EMA200 per symbol with Polars (`coalesce(close_adj, close_raw)`; `^VIX` uses raw)
and as-of aligns each symbol backward onto every requested calendar date (no
look-ahead). It classifies one market-wide regime per date by consuming
`constants.MARKET_REGIME_PRIORITY` top-down — VIX gates
(`>= 30 extreme_risk`, `>= 25 high_risk`) over an SPY/QQQ trend rule
(`SPY > EMA200 bull`; `SPY < EMA200 and QQQ < EMA200 bear`; else `neutral`) —
then updates every existing `daily_features` row for the date / current
`feature_schema_version`, setting only `market_regime` and `calculated_at` in a
single transaction. SPY-absent dates are skipped; insufficient SPY EMA200 →
`neutral` (warning). `db_role` accepts only `prod`/`debug`. The module never
inserts `daily_features`, never writes other tables, and never uses
provider/`duckdb`/`ATTACH`/DDL/`print()`. This closes open gap G-REGIME. See
`M12_MARKET_REGIME_ENGINE_SPEC.md`.

## Module 13 — Step 3 Screening (usage)

Module 13 (`app/services/screening/step3_screening.py`) runs after Module 12 and
before Module 14. `Step3ScreeningEngine().screen(signal_date, strategy_config,
strategy_config_id, db_role="prod", run_id=None)` reads `daily_features_current`
for `feature_date == signal_date`, left-joins `ticker_master` (`symbol_type`) and
`daily_prices` (`close_raw`/`close_adj`/`data_quality_status` on
`date = feature_date`), then applies the Step 3 hard filters
(`feature_ready`, `symbol_type='stock'`, `close_raw >= min_price`,
`avg_dollar_volume_20d >= min_avg_dollar_volume_20d`, `rvol20 >= min_rvol`,
`data_quality_status='ok'`) and, for passing rows, the Polars-vectorized soft
score (`trend/momentum/setup/volume/market` sub-scores clamped 0–100, weighted by
the top-level `config.scoring_weights`). Every evaluated ticker — passed and
failed — is appended as one `step3_candidates` row in a single transaction:
passed rows carry a non-null `screening_score` with `hard_filter_fail_reasons=[]`;
failed rows carry `screening_score=NULL` and all collected fail labels. Empty
input returns `success` with no insert. `db_role` accepts only `prod`/`debug`
(`simulation` rejected before DB access). The module only ever **inserts** into
`step3_candidates` (no updates/deletes, no other tables, no provider/`duckdb`/
`ATTACH`/DDL/`print()`). Open gaps: the `avg_dollar_volume_20d` volume
sub-component lacks a mapping in Project Files (omitted), and the
`distance_to_ema50` taper / `breakout_proximity` mid-band are closed by
documented assumptions. See `M13_STEP3_SCREENING_SPEC.md`.

## Module 14 — Step 4 Setup Analysis (usage)

Module 14 (`app/services/analysis/step4_analysis_engine.py`) runs after Module 13.
`Step4AnalysisEngine().analyze(signal_date, strategy_config, strategy_config_id,
db_role="prod", run_id=None)` reads the qualifying `step3_candidates`
(`signal_date`, `strategy_config_id`, `passed_hard_filters = TRUE`), joins
`daily_features_current` and `daily_prices` on `(ticker, signal_date)`, derives
`recent_20d_low_raw` from the last ≤20 `daily_prices.low_raw` rows, and reads the
prior ≤10 feature rows for the `trend_resume` history check. For each analyzable
candidate it classifies the setup (`high_tight_flag` → `breakout` →
`volatility_squeeze` → `trend_pullback` → `trend_resume` → `unknown`, first match
wins), computes the four 0–100 component scores and the clamped `setup_score`
(with earnings/macro penalties), and derives `entry_proxy_raw = close_raw`, an
ATR/recent-low `stop_price_raw` (clamped to `entry * 0.95` when inputs are
missing/invalid), `target_price_raw`, and `estimated_rr`. Each analyzable row is
written to `step4_analysis` with a fresh `uuid4` `analysis_id`, the preserved
`candidate_id`, and a sorted-key `explanation_json`, in a single transaction.
Empty qualifying input returns `success` with zero counts and no insert; a
candidate with no current feature row or no usable `close_raw` is skipped as not
analyzable (counted, not written). `db_role` accepts only `prod`/`debug`
(`simulation` rejected before DB access); config is validated before DB access.
The module only ever **inserts** into `step4_analysis` (no updates/deletes, no
other tables, no provider/`duckdb`/`ATTACH`/DDL/`print()`). Open assumptions:
`G-ATR-CONTRACTION`, `G-TREND-RESUME-HISTORY`, `G-SCORING-SUBCOMPONENT-WEIGHTS`,
`G-MISSING-ATR-OR-PRICE`. See `M14_STEP4_ANALYSIS_SPEC.md`.

## Module 15 — Step 5 Proposal Engine (usage)

Module 15 (`app/services/proposal/step5_proposal_engine.py`) runs after Module 14
and before Module 16. `Step5ProposalEngine().propose(signal_date, strategy_config,
strategy_config_id, db_role="prod", run_id=None)` reads the `step4_analysis` rows
for `(signal_date, strategy_config_id)`, `LEFT JOIN`ing `step3_candidates` (on
`candidate_id`) for `screening_score` and `ticker_master` (on `ticker`) for
`sector`/`industry`. Each analyzable analysis (non-NULL `setup_score` and
`screening_score`) gets a `proposal_score_raw = 0.40*setup_score +
0.25*screening_score + 0.20*rr_score + 0.15*timing_score` (RR tiers
100/80/60/0 at the 3.0/2.2/1.8 boundaries; NULL `timing_score` → 50; NULL
`estimated_rr` → `rr_score=0` and lowest in tie-breaks; scores clamped to
`[0,100]`). Raw ranking is `proposal_score_raw` DESC, `estimated_rr` DESC (NULL
lowest), `ticker` ASC, giving `raw_rank` / `in_raw_top_n`. Diversification is
either hard-cap (`hard_cap_enabled=True`: over-cap candidates are still inserted
with `diversified_rank=NULL`, `proposal_score_final=proposal_score_raw`, no soft
penalty, `rejection_reason` `sector_cap`/`industry_cap` with sector taking
priority when both are full) or soft-penalty (`hard_cap_enabled=False`:
`proposal_score_final = raw * sector_penalty**prior_sector * industry_penalty**
prior_industry`, then re-ranked by final DESC, ticker ASC, no rejections). Shared
semantics: `selected_flag = in_diversified_top_n`, `selected_top_n = in_raw_top_n
OR in_diversified_top_n`. One row per analyzable analysis (incl. rejected) is
appended to `step5_proposals` in a single transaction with a fresh `uuid4`
`proposal_id`. `db_role` accepts only `prod`/`debug` (`simulation` rejected before
DB access); config is validated before DB access; empty input returns `success`
with zero counts. Reruns are append-only (a new `run_id` adds rows). The module
only ever **inserts** into `step5_proposals` (no updates/deletes, no other tables,
no provider/`duckdb`/`ATTACH`/DDL/`print()`). Open gaps: `G-SOFT-PENALTY-PRIOR-
COUNT` (prompt key names `sector_penalty`/`industry_penalty` vs the `*_factor`
names in the example config blocks), `G-UNKNOWN-BUCKET`. See
`M15_STEP5_PROPOSAL_ENGINE_SPEC.md`.

## Module 16 — Outcome Queue (usage)

Module 16 (`app/services/outcomes/outcome_queue.py`) runs after Module 15 and
turns Step 5 proposals into realized `signal_outcomes`. It has two services.

`OutcomeQueueCreator().enqueue(signal_date, strategy_config_id, strategy_config,
db_role="prod", run_id=None)` reads every `step5_proposals` row for the given
`signal_date` / `strategy_config_id` that is in the raw OR diversified Top-N
(`in_raw_top_n OR in_diversified_top_n`, AD-22.13) and inserts one
`outcome_tracking_queue` row per horizon in `OUTCOME_HORIZONS_BD` (`[5,10,20,40]`).
`tracking_id = uuid5(NAMESPACE_URL, "outcome_tracking_queue:{proposal_id}:{horizon_bd}")`
makes inserts idempotent (`ON CONFLICT DO NOTHING`); reruns add zero rows.
`entry_date = next NYSE trading day after signal_date`, `eval_date = horizon_bd`
NYSE sessions after `entry_date` (via `app/utils/trading_calendar.py`). Rows start
`pending`.

`OutcomeQueueProcessor().process(run_date, strategy_config, db_role="prod",
run_id=None)` processes every `pending` queue row with `eval_date <= run_date`.
Per row it reads the entry-date `open_raw`, derives `entry_price_sim = open_raw *
(1 + slippage_bps/10000)` (AD-22.14), computes horizon returns for horizons
`<= horizon_bd`, 40bd MFE/MAE (40bd rows only), `realized_r_multiple` (NULL when
`stop_price_raw` missing or the denominator `<= 0`), and a tri-state
`earnings_within_window`, then upserts one `signal_outcomes` row
(`outcome_id = uuid5(NAMESPACE_URL, "signal_outcomes:{proposal_id}:{horizon_bd}")`)
and marks the queue row `done`. A missing entry price increments `repair_attempts`
(no outcome written) and flips the row to `unresolvable` on the 3rd attempt.
`db_role` accepts only `prod`/`debug` (`simulation` rejected before DB access);
config (`simulation.slippage_bps`) is validated before DB access; reads are
read-only and all writes run in one rolled-back-on-failure transaction. The
services write only `outcome_tracking_queue` / `signal_outcomes` (no other tables,
no provider/`duckdb`/`ATTACH`/DDL/`print()`). See `M16_OUTCOME_QUEUE_SPEC.md`.

## Module 17 — Simulation Engine (usage)

Module 17 (`app/services/simulation/simulation_engine.py`) replays the frozen
Step 3/4/5 pipeline and realized outcomes over a historical date range into the
`sim_*` tables of `simulation.duckdb`, reading production data **only** through a
read-only prod attach.

`SimulationEngine().run(sim_name, mode, start_date, end_date, config_ids,
strategy_configs, db_role="simulation", run_id=None)` returns a `ServiceResult`.
`run_id` is minted (`uuid4`) when `None` and otherwise preserved. All
preconditions are validated before any DB access: `db_role == "simulation"`; a
valid `mode` (`research` / `walk_forward` / `config_comparison`); non-empty
`config_ids`; every `config_id` present in `strategy_configs`; and
`start_date <= end_date`.

For each NYSE session in the window the engine screens `prod.daily_features`
(current schema version, `feature_cutoff_date <= sim_date`) joined to
`prod.ticker_master` / `prod.daily_prices` (price `date <= sim_date`), enforcing
**no look-ahead in SQL**, then reuses the frozen Module 13–15 scoring/ranking
code to write `sim_step3_candidates`, `sim_step4_analysis` and
`sim_step5_proposals`. Each raw- or diversified-Top-N proposal gets one
`sim_signal_outcomes` row computed with the Module 16 / FORMULAS §64 rules using
`entry_price_sim` as the denominator and a `list_membership` of `raw_only`,
`diversified_only` or `both`. `sim_config_comparisons` then gets one row per
`(config_id, horizon_bd, list_type)` for both `raw` and `diversified`
(expectancy, win_rate, avg_win, avg_loss, profit_factor, max_drawdown_pct,
resolved_outcomes_pct on resolved outcomes only). In `walk_forward` mode the
engine plans expanding-window calendar-quarter folds (12-month minimum initial
train), selects the best config per fold by max train expectancy subject to
`resolved_outcomes_pct >= 0.85` and `max_drawdown_pct <= 25`, flags
`cross_fold_outcome` for 40bd realizations that spill past a fold's test period
(excluded from fold metrics), and records fold metadata in `sim_folds`. `sim_runs`
moves `pending` → `running` → `success`; on failure partial writes are rolled
back and the row is marked `failed` with the error in `notes`. The engine writes
only `sim_*` tables and never opens prod directly, imports `duckdb`, calls
providers, runs DDL, or uses `print()`. See `M17_SIMULATION_ENGINE_SPEC.md`.

## Module 18 — Export Package Engine (usage)

Module 18 (`app/services/export/export_package_engine.py`) builds reviewer ZIP
packages and records one manual review row.
`ExportPackageEngine(db_manager=None).export_ticker_review(signal_date,
strategy_config_id, proposal_ids, db_role="prod")` reads `step3_candidates`,
`step4_analysis`, `step5_proposals`, `daily_features_current` and `daily_prices`
for the named proposals, writes `ticker_review_{signal_date}_{run_id[:8]}.zip`
(`metadata.json`, `prices.csv`, `features.csv`, `step3.csv`, `step4.csv`,
`step5.csv`, `explanation.txt`) under `settings.EXPORTS_DIR`, and inserts one
`ai_reviews` row (`review_type='ticker_review'`, `provider='manual'`,
`prompt_version='v1'`). `export_simulation_review(sim_run_id,
db_role="simulation")` reads the `sim_*` tables and writes
`simulation_review_{sim_run_id[:8]}_{run_id[:8]}.zip` (`configs.json`,
`performance_metrics.csv`, `score_buckets.csv`, `setup_performance.csv`,
`regime_performance.csv`, `drawdowns.csv`, `unresolved_outcomes.csv`) plus one
`sim_ai_reviews` row. Every call returns a `ServiceResult`. The engine routes all
DB access through the injected `db_manager`, writes ZIPs only under
`EXPORTS_DIR`, mutates only `ai_reviews` / `sim_ai_reviews`, and never imports
`duckdb`, calls providers, runs DDL/`ATTACH`, or uses `print()`. Open gaps
(`G-PROMPT-TEXT`, `G-ZIP-CLEANUP`, `G-PRICES-WINDOW`, `G-REGIME-SOURCE`,
`G-CONFIGS-SOURCE`, `G-SIM-AI-SCHEMA`) are documented in
`M18_EXPORT_PACKAGE_ENGINE_SPEC.md`.

## Module 19 — AI Review Engine (usage)

Module 19 (`app/services/ai_review/ai_review_engine.py`) sends a manual review
row recorded by Module 18 to an AI provider, stores the AI response on the same
row, and later records the reviewer's qualitative action. It is an UPDATE-only
overlay on `ai_reviews` (ticker, `prod` / `debug`) and `sim_ai_reviews`
(simulation). `AiReviewEngine(db_manager=None, ai_client=None).send_ticker_review(
ai_review_id, db_role="prod")` reads the row read-only, refuses if
`ai_response_text` is already set (G-FORCE-RESEND), calls
`ai_client.send(prompt_text, provider, model)`, and writes only
`ai_reviews.ai_response_text`. `send_simulation_review(ai_review_id,
db_role="simulation")` does the same against `sim_ai_reviews`.
`record_human_action(ai_review_id, human_action, db_role="prod")` writes only
`ai_reviews.human_action` for one of `ignored` / `accepted` / `overrode` /
`deferred`, and is blocked until an AI response exists. Every call returns a
`ServiceResult`. The injectable `ai_client` follows `AiClientProtocol`; the
`DefaultAiClient` resolves provider/model from the row and the API key
(`ANTHROPIC_API_KEY` / `OPENAI_API_KEY`) lazily at call time, imports no SDK at
import time, and adds no SDK dependency (G-API-KEY-ENV, G-SDK-DEP). The engine
issues UPDATE only (no INSERT/DELETE), mutates only
`ai_response_text` / `human_action`, and never imports `duckdb`, calls
market-data providers, runs DDL/`ATTACH`, or uses `print()`. Open gaps
(`G-FORCE-RESEND`, `G-RESPONSE-ORPHAN`, `G-API-KEY-ENV`, `G-SDK-DEP`) are
documented in `M19_AI_REVIEW_ENGINE_SPEC.md`.

## Module 20 — Pipeline Orchestrator (usage)

Module 20 (`app/services/pipeline/pipeline_orchestrator.py`) coordinates one
daily run end to end as a control-plane layer: it owns the `daily_pipeline`
lock, the `pipeline_runs` lifecycle row, and the ordering of the frozen step
engines, but performs no domain logic itself. `PipelineOrchestrator(...)` takes
an injectable `db_manager`, `provider`, and every step engine (defaults are
built in `__init__` only: `db_manager` → the approved `duckdb_manager`,
`provider` → `YahooProvider()`, each engine → its class with
`db_manager=self._db`). `run(run_date, run_type="scheduled", db_role="prod",
force_rerun=False, resume_from=None, strategy_configs=None, run_id=None)` mints a
UUID4 `run_id` when none is given, validates inputs **before** any DB access,
acquires the lock (overriding a stale one past the 300 s threshold; refusing if
an active run holds it), guards against an already-succeeded `run_date` (unless
`force_rerun`), inserts a `running` row, then drives the 13 ordered steps —
benchmark/universe/price ingestion, validation, mutation detection, feature
calculation, the step-major strategy block (each logical step runs for all strategy
configs before the next: `step3_screening` for all configs → record;
`step4_analysis` for all configs → record; … `outcome_processing` for
all configs → record), dashboard materialization
(V1 no-op), and backup. Critical-step failures abort and write
`status='failed'`; recoverable failures degrade to `success_with_warnings` and
continue. The lock is always released in `finally`, with a heartbeat after each
completed step. Every call returns a `ServiceResult` whose `metadata` carries
exactly `run_id`, `run_date`, `run_type`, `db_role`, `steps_completed`,
`failed_step`, `error`, `duration_sec`, and `status`. All SQL is parameterized
against only `pipeline_runs` / `pipeline_locks`; the module never imports
`duckdb`, runs DDL/`ATTACH`, writes the simulation DB, or uses `print()`. Open
gaps (`G-STRATEGY-CONFIGS`, `G-HEARTBEAT-THREADING`, `G-DASHBOARD-MAT`,
`G-UNIVERSE-PROVIDER`) are documented in `M20_PIPELINE_ORCHESTRATOR_SPEC.md`.
