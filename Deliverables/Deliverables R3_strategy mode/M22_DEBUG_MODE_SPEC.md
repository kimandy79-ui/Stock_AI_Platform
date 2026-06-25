# M22 — Debug Mode Spec

Module 22 is a thin **control plane** for fast, local debug/testing runs against
`debug.duckdb` only. It delegates all domain logic to the frozen Module 20
orchestrator and the frozen step engines.

Source of truth: `01d_MODULES_AND_PIPELINE.md` §51/§70; `01e_UI_AND_TESTING.md`
§92; `01b_SCHEMA_AND_DATA.md` (`run_type` enum); `02_PROJECT_IMPLEMENTATION_CONTEXT.md`
§21; `02b_ARCHITECTURE_DECISIONS.md` §22.4; `M20_PIPELINE_ORCHESTRATOR_SPEC.md`.

Module: `app/services/debug/debug_mode.py`. Package re-exports in
`app/services/debug/__init__.py`.

---

## 1. Public API

```python
class DebugModeController:
    def __init__(self,
        db_manager=None, provider=None,
        orchestrator_factory=None, strategy_configs=None,
        scoped_feature_engine_factory=None,    # injectable for offline tests
        scoped_screening_engine_factory=None,  # injectable for offline tests
    ) -> None: ...

    def run_preset(self, preset_name, run_date, *,
                   sample_count=None, watchlist=None, strategy_names=None,
                   force_rerun=True, run_id=None) -> ServiceResult: ...

    def run(self, plan: DebugRunPlan, run_id=None) -> ServiceResult: ...
```

All heavy dependencies (orchestrator, YahooProvider, FeatureEngine,
Step3ScreeningEngine, polars) are imported **lazily** inside factory callables —
never at module import time — so offline tests that inject fakes never trigger
those imports.

---

## 2. Presets (`DEBUG_PRESETS`)

| key | tickers | trading_days | step range | strategies |
|---|---|---|---|---|
| `fast_smoke_test` | 20 | 5 | `benchmark_etf_ingestion` → `backup` (full) | `normal` |
| `indicator_validation` | 10 | 90 | `feature_calculation` only | `normal` |
| `pipeline_sanity` | 100 | 30 | `benchmark_etf_ingestion` → `step5_proposals` | `normal` |
| `config_tuning_test` | 500 | 126 | `step3_screening` → `step5_proposals` | all three |

`trading_days` is **plan metadata only** (single-date orchestrator, see §7).

---

## 3. Guards

Rejection (failed `ServiceResult`, no orchestrator built) when:

| condition | error |
|---|---|
| `db_role ∈ {prod, simulation}` | "must not target db_role…" |
| `db_role != "debug"` or `run_type != "debug"` | "invalid…" |
| step range invalid / inverted | step name errors |
| `strategy_names` empty | "strategy_names must be non-empty" |
| watchlist unique count > 500 | "watchlist has N unique tickers…" |
| `sample_count < 1` (no watchlist) | "sample_count must be >= 1…" |
| `sample_count > 500` | "…exceeds the debug cap…" |

`run_preset` order-preserving de-duplicates the watchlist before plan creation.

---

## 4. Mechanism — frozen modules respected

### 4.1 Effective start step and bridge no-ops

When `start_step > universe_ingestion`, `effective_start_step` returns
`"universe_ingestion"`. The orchestrator starts there (`resume_from`), running
universe ingestion with the `SamplingProvider` to update `debug.duckdb`'s
universe snapshot. Steps between `universe_ingestion` and `start_step` become
bridge `_NoOpStepEngine`s.

### 4.2 SamplingProvider

Wraps `list_symbols`: at most `sample_count` entries (stable sort + head) or
exact watchlist entries. Handles strings, dicts, and objects with `.ticker`
attribute. All other provider methods delegate unchanged.

### 4.3 Real ticker scoping for partial runs

**Problem**: `FeatureEngine.calculate(tickers=None)` reads all tickers from
`daily_prices`; `Step3ScreeningEngine.screen()` reads all rows from
`daily_features_current`. Updating the universe snapshot alone is not sufficient.

**Solution**: the controller resolves `selected_tickers` from the provider
*before* orchestrator construction and injects scoped engines:

#### _ScopedFeatureEngine (indicator_validation)

Wraps a real `FeatureEngine`. Its `calculate()` always passes
`tickers=selected_tickers` — never `None`. `FeatureEngine.calculate` already
accepts a `tickers` keyword so **Module 11 is not modified**.

#### _ScopedStep3ScreeningProxy (config_tuning_test)

Delegates to a real `Step3ScreeningEngine` **subclass** whose private `_read()`
method is overridden to apply a polars filter:

```python
class _FilteredStep3(Step3ScreeningEngine):
    def _read(self, db_role, signal_date):
        frame = super()._read(db_role, signal_date)
        return frame.filter(pl.col("ticker").is_in(tickers_frozen))
```

The **public** `screen()` signature of `Step3ScreeningEngine` is
**completely unchanged** — `test_screen_signature_exact` continues to pass.
**Module 13 is frozen.** The proxy's `screen()` calls the underlying engine
with the identical signature the orchestrator uses (no extra arguments).

The dynamic `_FilteredStep3` subclass and its polars import are created lazily
inside `_resolve_scoped_step3_factory()` — they never occur at module import time
or in offline tests that inject fakes.

### 4.4 Scope-detection helpers

```python
_needs_feature_scope(plan):
    # feature in range AND price_ingestion is a bridge no-op
    return not plan._is_noop("feature_calculation") and plan._is_noop("price_ingestion")

_needs_step3_scope(plan):
    # step3 in range AND feature_calculation is a bridge no-op
    return not plan._is_noop("step3_screening") and plan._is_noop("feature_calculation")
```

### 4.5 Engine injection matrix

| step | indicator_validation | config_tuning | fast_smoke / pipeline_sanity |
|---|---|---|---|
| `universe_ingestion` | real (effective start) | real (effective start) | real |
| `price_ingestion` | no-op (bridge) | no-op (bridge) | real |
| `feature_calculation` | `_ScopedFeatureEngine` | no-op (bridge) | real |
| `step3_screening` | no-op (after end) | `_ScopedStep3ScreeningProxy` | real |
| `step4/5` | no-op (after end) | real | real |

`dashboard_materialization` (V1 no-op) and `backup` (copy of `debug.duckdb`) are
internal to the orchestrator and not injectable — they may still run at the end
of any debug run (harmless).

### 4.6 force_rerun

`DebugRunPlan.force_rerun` (default `True`) is forwarded to
`orchestrator.run(force_rerun=...)` so the same `run_date` can be debugged
repeatedly without hitting the already-run guard.

---

## 5. Result metadata (`metadata["debug"]`)

```
preset, db_role, run_type, force_rerun,
sample_count (None when watchlist), watchlist,
start_step, effective_start_step, end_step,
strategy_names, trading_days, executed_steps, noop_steps,
selected_tickers,        # list[str] | None (None for full-pipeline)
needs_feature_scope,     # bool
needs_step3_scope,       # bool
```

---

## 6. Assumptions

- Single-date runs; `trading_days` is metadata only.
- Partial presets assume required upstream data exists in `debug.duckdb`.
- `prod.duckdb` is never written.
- Module 13 (`step3_screening.py`) is **not modified**.

---

## 7. Modified files (relative to M21 baseline)

| file | change |
|---|---|
| `app/services/debug/debug_mode.py` | new (M22) |
| `app/services/debug/__init__.py` | new (M22) |
| `tests/test_debug_mode.py` | new (M22) |
| `M22_DEBUG_MODE_SPEC.md` | new (M22) |

No other file is changed.

---

## 8. Tests (`tests/test_debug_mode.py`, fully offline — 53 tests)

**SamplingProvider**: limit/sort/determinism, TickerInfo objects, dict entries,
watchlist, failed pass-through, delegation.

**_NoOpStepEngine**: any method → success, 0 rows.

**_ScopedFeatureEngine**: always forwards selected_tickers (not None); ignores
caller kwarg; exposes attribute.

**_ScopedStep3ScreeningProxy**: delegates to real engine with **standard Step3
signature** (no tickers kwarg); proves `"tickers"` is absent from the
forwarded call; exposes selected_tickers.

**Scope helpers**: `_needs_feature_scope` / `_needs_step3_scope` correct for all four presets.

**Guards**: all rejection conditions covered; no orchestrator built on failure.

**Ticker-scope mechanism**: indicator_validation injects `FakeScopedFeatureEngine`
with sorted, capped tickers; selected_tickers ≠ None proved. config_tuning_test
injects `FakeScopedStep3Engine`; capped at preset limit; determinism asserted;
bridge steps no-op'd. Full-pipeline presets: feature and screening engines are
`None` (real).

**Force-rerun / isolation / presets / metadata**: complete coverage.
