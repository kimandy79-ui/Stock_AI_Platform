# M18 — Export Package Engine Spec

Module 18 builds reviewer-facing export ZIP packages and records one or more
manual review row(s) per export. It is a read-mostly service: it reads existing
prod/debug or simulation tables, writes ZIP files under `settings.EXPORTS_DIR`,
and inserts into `ai_reviews` (ticker) or `sim_ai_reviews` (simulation). No
other table is mutated.

**Phase 3 delta (2026-07-05) — multi-pass review.** By default (no
`ai_review_config` passed, or a config with `ai_review.multi_pass.enabled`
falsy) exactly one legacy row is written per export, `review_kind = NULL`,
`provider = "manual"`, `model = "none"` — byte-identical to pre-Phase-3
behavior. When `ai_review_config`'s `multi_pass.enabled` is true, **three**
rows are written per export instead — one each for `review_kind` `thesis` /
`contrarian` / `audit`, each with that pass's own configured `provider` /
`model` (so contrarian resolves to a different provider than thesis, per
`M19_AI_REVIEW_ENGINE_SPEC.md`). All rows for one export are written inside a
single transaction (all-or-nothing). See
`default_configs.DEFAULT_RUNTIME_CONFIGS["ai_review"]["multi_pass"]` for the
config shape.

Source of truth: `01e_UI_AND_TESTING.md` / `UI/96_Export_Package_Specs.md` (ZIP
manifests), `01b_SCHEMA_AND_DATA.md` + `M02_SCHEMA_SPEC.md` §3.19/§4.9
(`ai_reviews` / `sim_ai_reviews` and source-table schemas),
`01a_CORE_PRINCIPLES.md` (`review_type`, `list_membership` enums),
`app/config/settings.py` (`EXPORTS_DIR`), the frozen Module 17 simulation
tables, and the Module 16 `ServiceResult` discipline.

## 1. Public API

```python
class ExportPackageEngine:
    def __init__(self, db_manager=None) -> None: ...

    def export_ticker_review(
        self, signal_date, strategy_config_id, proposal_ids,
        db_role="prod", run_id=None, ai_review_config=None,
    ) -> ServiceResult: ...

    def export_simulation_review(
        self, sim_run_id, db_role="simulation", run_id=None,
        ai_review_config=None,
    ) -> ServiceResult: ...
```

`ai_review_config` (Phase 3, optional): overrides
`default_configs.DEFAULT_RUNTIME_CONFIGS["ai_review"]`, resolved lazily only
when `None` is passed (the common case costs no extra import). Controls
legacy-single-row vs. multi-pass-three-row behavior; see the delta note above.

- Always returns `ServiceResult`; never raises for expected validation / DB /
  ZIP failures.
- `run_id` is minted with `uuid4()` only when `None`; a supplied value is
  preserved verbatim.
- `db_manager` is injected; defaults to `app.database.duckdb_manager`.

## 2. db_role validation (before any DB access)

- Ticker: `db_role ∈ {"prod", "debug"}`, non-empty `strategy_config_id`,
  non-empty `proposal_ids`.
- Simulation: `db_role == "simulation"`, non-empty `sim_run_id`.

Validation failures return `failed` with no DB access and no ZIP.

## 3. Metadata keys (every return path)

- Ticker: `run_id, export_type, db_role, signal_date, strategy_config_id,
  proposal_ids, zip_filename, zip_path, review_type, review_table,
  ai_review_rows, status, error`.
- Simulation: `run_id, export_type, db_role, sim_run_id, zip_filename, zip_path,
  review_type, review_table, ai_review_rows, status, error`.

`zip_filename` / `zip_path` / `error` are `None` on paths where they are not
available. `review_type` mirrors `export_type` (`ticker_review` /
`simulation_review`); `review_table` is `ai_reviews` / `sim_ai_reviews`.
`ai_review_rows` (Phase 3) is always a list — `[]` on paths where no row was
written yet, otherwise one `{"review_kind", "ai_review_id", "provider",
"model"}` dict per row written (length 1 for the legacy path, 3 for
multi-pass).

## 4. ZIP contracts

ZIPs are written only under `settings.EXPORTS_DIR`.

### Ticker — `ticker_review_{signal_date}_{run_id[:8]}.zip`

| File | Content |
|------|---------|
| `metadata.json` | `run_id, signal_date, strategy_config_id, proposal_ids, export_timestamp` |
| `prices.csv` | `daily_prices` for proposal tickers within ±5 trading rows of `signal_date` (see G-PRICES-WINDOW) |
| `features.csv` | `daily_features_current` for proposal tickers on `signal_date` (full view columns) |
| `step3.csv` | `step3_candidates` by `signal_date` + `strategy_config_id` + proposal tickers |
| `step4.csv` | `step4_analysis` by same scope |
| `step5.csv` | `step5_proposals` by `proposal_ids` |
| `explanation.txt` | formatted `mechanical_explanation` per proposal (JSON pretty-printed, else raw text) |

### Simulation — `simulation_review_{sim_run_id[:8]}_{run_id[:8]}.zip`

| File | Content |
|------|---------|
| `configs.json` | `config_ids` + `sim_runs` metadata (see G-CONFIGS-SOURCE) |
| `performance_metrics.csv` | `sim_config_comparisons` for `sim_run_id` |
| `score_buckets.csv` | score distributions from `sim_step3_candidates` (screening_score) and `sim_step5_proposals` (proposal_score_final); buckets 0–100 width 10; only populated buckets |
| `setup_performance.csv` | mean return by `setup_type × horizon_bd` from `sim_signal_outcomes` joined to `sim_step4_analysis` on `(strategy_config_id, ticker, signal_date)`; horizons unpivoted from `return_{5,10,20,40}bd_pct` |
| `regime_performance.csv` | header-only (see G-REGIME-SOURCE) |
| `drawdowns.csv` | per-`strategy_config_id` 40bd equity-curve drawdowns over the diversified list (`list_membership ∈ {diversified_only, both}`), ordered by `signal_date`; columns `strategy_config_id, peak_date, trough_date, drawdown_pct` |
| `unresolved_outcomes.csv` | `sim_signal_outcomes` where `outcome_status = 'partial'` |

All required filenames always exist. Header-only CSV is permitted only when no
matching rows exist; this is exercised by tests (`drawdowns.csv` all-positive,
`regime_performance.csv`).

## 5. Review-row contract (exactly one row after successful ZIP)

### Ticker — `ai_reviews`

`review_type="ticker_review"`, `proposal_id=first proposal_id`, `sim_run_id=NULL`,
`provider="manual"`, `model="none"`, `prompt_version="v1"`,
`prompt_text=structured V1 text`, `selected_tickers_json=JSON array of exported
tickers`, `ai_response_text=NULL`, `human_action=NULL`.

### Simulation — `sim_ai_reviews`

`sim_run_id=supplied sim_run_id`, `provider="manual"`, `model="none"`,
`prompt_version="v1"`, `prompt_text=structured V1 text`, `ai_response_text=NULL`,
`human_action=NULL`. See **G-SIM-AI-SCHEMA**: the frozen `sim_ai_reviews` schema
does not have `review_type` / `proposal_id` / `selected_tickers_json` columns, so
those are not written to the table (they remain in `ServiceResult` metadata).

## 6. prompt_text V1 (G-PROMPT-TEXT)

Ticker:
```
[TICKER REVIEW — {signal_date} — {strategy_config_id}]
Proposals: {ticker list}
Top proposal score: {max proposal_score_final}
Setup types: {distinct setup_types}
Estimated RR range: {min}–{max}
{brief step4/step5 summary}

Assess: are these proposals worth executing today? Flag earnings risk and macro risk.
```

Simulation:
```
[SIMULATION REVIEW — {sim_run_id}]
Best config by expectancy: {config_id} / {expectancy}
Worst max drawdown: {config_id} / {max_drawdown_pct}
Resolved outcomes pct range: {min}–{max}
{brief sim_config_comparisons summary}

Assess: which config should be selected and what risks should be monitored?
```

V1 is intentionally simple; Module 19 may replace it.

## 7. Failure behavior

- Validation failure → `failed`, no DB access, no ZIP.
- DB read failure → `failed`, no ZIP.
- ZIP build failure → `failed`; no review row required.
- DB write failure **after** ZIP creation → `failed`; the ZIP is retained on
  disk and its `zip_filename` / `zip_path` are reported in metadata. See
  **G-ZIP-CLEANUP**: no automatic ZIP cleanup is performed on a post-ZIP write
  failure.

## 8. Hard boundaries

No direct `duckdb` import, no provider imports/calls, no `print()`, no DDL, no
`ATTACH`. All DB access flows through the injected `db_manager`. Only
`ai_reviews` / `sim_ai_reviews` are mutated. Tests assert these statically.

## 9. Assumptions / open gaps

- **G-PROMPT-TEXT** — V1 prompt text format frozen here; replaceable in M19.
- **G-ZIP-CLEANUP** — post-ZIP DB-write failure leaves the ZIP on disk; no
  cleanup is attempted.
- **G-PRICES-WINDOW** — the ±5 trading-day window is approximated using the
  price rows that actually exist per ticker (up to 5 rows on/before
  `signal_date` plus up to 5 after), avoiding a market-calendar dependency
  inside Module 18.
- **G-REGIME-SOURCE** — `market_regime` is not persisted in any `sim_*` table
  and attaching prod read-only is out of scope for M18, so
  `regime_performance.csv` is emitted header-only. If a future module persists
  regime into a sim table, this file can be populated without an API change.
- **G-CONFIGS-SOURCE** — full `strategy_configs` JSON is not present in the
  simulation database and `ATTACH` to prod is out of scope, so `configs.json`
  carries `config_ids` + `sim_runs` metadata and `strategy_configs: null`.
- **G-SIM-AI-SCHEMA** — the frozen `sim_ai_reviews` schema (M02 §4.9) lacks
  `review_type` / `proposal_id` / `selected_tickers_json`; those values live in
  `ServiceResult` metadata rather than being written to the table. This is a
  prompt-vs-schema conflict resolved in favor of the higher-priority schema.

## 10. Test summary

`tests/test_export_package_engine.py` (offline; fake injected `db_manager`
backed by real DuckDB temp files with the real schema; `EXPORTS_DIR` redirected
to `tmp_path`):

- `ServiceResult` success/failure and `run_id` mint/preserve for both methods.
- Pre-DB validation: invalid roles, empty `proposal_ids`,
  empty `strategy_config_id`, empty `sim_run_id`.
- ZIP path/filename under `EXPORTS_DIR`; exact file manifests; non-empty files.
- Review writes: `review_type`, first ticker `proposal_id`, `sim_run_id`,
  `selected_tickers_json`, non-empty `prompt_text`, `provider="manual"`,
  `model="none"`.
- `drawdowns.csv` row for a negative path; header-only for an all-positive path.
- `score_buckets.csv` every populated 10-point bucket.
- `regime_performance.csv` header-only; `setup_performance.csv` horizon rows.
- DB-write-after-ZIP failure → `failed` with ZIP retained.
- Static scans: no `duckdb` / provider imports, no real `print()`, no DDL/ATTACH
  in executed SQL, only `ai_reviews` / `sim_ai_reviews` mutated.
