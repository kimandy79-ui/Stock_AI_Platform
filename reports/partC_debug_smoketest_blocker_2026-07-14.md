# Part C — Debug Stage-1 Smoke Test: Deliverables 1-3 + Execution Blocker

**To:** Architect
**From:** Coder
**Date:** 2026-07-14
**Re:** CODER NOTE — Finalize Date Selection + Targeted Debug Seeding (Part C)
**Status:** Dates ✅ confirmed · Deliverables 2 & 3 ✅ · **Part C execution ⛔ blocked — the specced `run_debug_pipeline.py --sample-count 50` command is neither bounded nor scoped as assumed; running it would trigger a full-universe (~3,900-ticker) network fetch → the known kill.** One bounded path exists but deviates from the specced CLI — needs your nod.
**No writes made** (no `debug.duckdb` created, no copy, no run).

---

## Confirmed inputs
- **5 dates:** 06-11 (0.873, med), 06-18 (1.559, high), 06-26 (1.827, high), 07-02 (0.771, low), 07-08 (0.761, low).
- **Smoke-test date:** 2026-07-08.

## Deliverable 2 — `--sample-count` selection logic
`SamplingProvider._select` (`debug_mode.py:296`): `sorted(symbols, key=symbol)[:sample_count]` → **first-N alphabetical, deterministic.** BUT `symbols` comes from the base provider's `list_symbols`, and the debug controller's default base provider is a **bare `YahooProvider()` with no symbol source** (`debug_mode.py:721-725`) → `list_symbols()` returns **empty** (`yahoo_provider.py:477-487`). So the "sample" is `first-N of []` = **empty** unless a provider with a symbol source is injected.

## Deliverable 3 — feature step recompute behavior
For a **full** preset, `_step_features` calls `FeatureEngine.calculate(run_date, run_date)` unconditionally and **overwrites** `daily_features` (`pipeline_orchestrator.py:1303`). So seeding `daily_features` is pointless there — must seed `daily_prices`. **However** (see below) the only *bounded* preset skips the feature step entirely, which flips this: there, seeding `daily_features` is exactly right and necessary.

---

## Part C execution blocker — why the specced command fails

`python tools/run_debug_pipeline.py --date 2026-07-08 --sample-count 50 --db-role debug` uses the default **`fast_smoke_test`** preset = **full pipeline** (`start_step=first … end_step=last`). Three independent facts make it unsafe/ineffective:

1. **Price ingestion is NOT scoped by `--sample-count`.** `_step_price` calls `ingest(provider=…, start=run_date, end=run_date)` with **`tickers=None`** → `DailyPriceIngestionEngine` runs `_select_active_stocks(db_role)` = **every active ticker in `ticker_master`**, then one network `get_price_history` per ticker (`daily_price_ingestion.py:283-315`). On a debug DB whose `ticker_master` was populated by the run's own `universe_ingestion` (CSV = full universe), that's **~3,900 network calls** — the full-universe loop that gets killed on this machine.

2. **`--sample-count` only limits `provider.list_symbols`, which ingestion never uses.** The sampling decorator scopes the *provider's* symbol list; price ingestion reads `ticker_master` instead. So sampling has **no effect** on what gets ingested in a full preset.

3. **Feature/step3 scoping only activates for PARTIAL presets.** `_needs_feature_scope` requires the **price** step to be a noop; `_needs_step3_scope` requires the **feature** step to be a noop (`debug_mode.py:115-126`). For `fast_smoke_test` both are False → the feature and step3 engines run **unscoped** over the whole universe too.

**Net:** the CLI command would attempt a full-universe network ingest (kill risk) and, even if it survived, compute only `run_date`'s single bar per ticker → `feature_ready=False` → empty diagnostics. `--sample-count 50` does not produce a 50-ticker run. Targeted seeding of "the 50-sample" therefore has nothing to attach to.

---

## The one bounded path that works (deviates from the CLI)

Use the **`config_tuning_test`** preset instead: `start_step=step3_universal_eligibility … end_step=step5_proposals` (`debug_mode.py:173-181`). That makes **benchmark / universe / price / validation / mutation / feature all noops** → **zero network, `ticker_master` and prices untouched**, and step3 reads **pre-seeded** `daily_features`. Because the feature step is skipped here, **seeding `daily_features` is correct** (contrast Deliverable 3).

To make it a *bounded 50-ticker* run, three things are required — none expressible through `run_debug_pipeline.py` (no `--preset config_tuning_test` control of provider, no `--watchlist`, no provider injection):

1. **Inject a base provider with a 50-symbol source** so `list_symbols` is non-empty and `selected_tickers` = the 50 (scoped step3 filters to them). A bare provider yields empty even with a watchlist, since `_select` filters the base list rather than supplying one.
2. **Seed `debug.duckdb`** (network-free, read prod → write debug) for those 50 tickers: `ticker_master` (50 active rows), `daily_prices` (07-08 bar — step3's eligibility join needs `close_raw`/`data_quality`), and `daily_features` (07-08, `feature_ready=True`, copied from prod).
3. **Drive it via `DebugModeController.run_preset("config_tuning_test", date(2026,7,8), watchlist=<50>)`** in a thin script — the controller supports this; the CLI does not.

Then `run_funnel_diagnostics.py --date 2026-07-08 --db-role debug --json-out funnel_stage1_check.json` reads the resulting step3/4/5 rows.

This is **existing components only** (controller + preset + engines + a read-prod/write-debug copy) — no new pipeline logic — but it is a real deviation from the specced CLI, so I'm holding for your go-ahead per the note's guardrail.

### Alternatives if you'd rather not deviate
- **A. Approve the bounded driver above** (recommended) — I implement the thin script + seed and run it. ~50 tickers, no network, real diagnostics.
- **B. Full prod→debug copy + partial run** — copy prod's raw+features wholesale into `debug.duckdb`, then the same `config_tuning_test` driver over a 50-symbol watchlist. Heavier copy, same run.
- **C. Accept an unscoped, network-heavy run** — only viable off-hours with the kill worked around; not recommended.

---

## Deliverables recap
1. **5 dates** — ✅ confirmed; smoke date 2026-07-08.
2. **Sample logic** — first-N alphabetical, but base provider returns empty → needs an injected symbol source.
3. **Recompute behavior** — full preset overwrites features (seed prices); the bounded preset skips the feature step (seed features).
4. **Part C diagnostics output** — ⛔ not produced; specced CLI command is unsafe/ineffective (full-universe network fetch, no 50-scoping). Bounded path defined above; **awaiting your pick (A/B/C).**
5. **Errors** — none run; blocker is mechanism, established from code (file:line cited throughout).

**One decision needed:** approve path **A** (bounded driver + targeted seed via the controller, not the CLI), **B**, or **C**. On approval I'll execute and return `funnel_stage1_check.json` with sections 1b / 5 / 6b.
