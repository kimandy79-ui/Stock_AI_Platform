# Claude Coding Prompt — Module 18: Export Package Engine

Use Project Instructions and Project Files. Use `00_PROJECT_FILE_MAP.md` only for targeted retrieval. Do not restate global rules.

## Inputs / Scope

- Base code: `stock_ai_platform_module17_simulation_engine_stable.zip`
- No Module 18 spec exists. Create `M18_EXPORT_PACKAGE_ENGINE_SPEC.md` from Project Files + this prompt.
- Implement **Module 18 only**. Modules 01–17 are frozen. Do not implement Module 19+.
- Do not modify frozen modules unless required by a failing test or real integration blocker; document any such change.

Allowed files:
```text
app/services/export/__init__.py
app/services/export/export_package_engine.py
tests/test_export_package_engine.py
M18_EXPORT_PACKAGE_ENGINE_SPEC.md
README.md                           # Module 18 note only
```

## Public API

```python
class ExportPackageEngine:
    def __init__(self, db_manager=None) -> None: ...

    def export_ticker_review(
        self,
        signal_date: date,
        strategy_config_id: str,
        proposal_ids: list[str],
        db_role: str = "prod",
        run_id: str | None = None,
    ) -> ServiceResult: ...

    def export_simulation_review(
        self,
        sim_run_id: str,
        db_role: str = "simulation",
        run_id: str | None = None,
    ) -> ServiceResult: ...
```

Rules:
- Always return `ServiceResult`; do not raise for expected validation/DB/export failures.
- Mint `uuid4()` only when `run_id is None`; preserve supplied `run_id`.
- Inject `db_manager`; default to `app.database.duckdb_manager`.
- Validate before DB access:
  - ticker: `db_role in ("prod", "debug")`, non-empty `proposal_ids`, non-empty `strategy_config_id`
  - simulation: `db_role == "simulation"`, non-empty `sim_run_id`
- Stable `metadata` keys on every return path:
  - ticker: `run_id, export_type, db_role, signal_date, strategy_config_id, proposal_ids, zip_filename, zip_path, review_type, review_table, status, error`
  - simulation: `run_id, export_type, db_role, sim_run_id, zip_filename, zip_path, review_type, review_table, status, error`
  - use `None` for unavailable failed-path values.

## Hard Boundaries

- No direct `duckdb`, provider imports/calls, `print()`, DDL, or `ATTACH` in Module 18.
- All DB access through `db_manager`.
- ZIPs only under `settings.EXPORTS_DIR`.
- Ticker review writes only one row to `ai_reviews` in prod/debug role.
- Simulation review writes only one row to `sim_ai_reviews` in simulation role.
- Do not mutate any table except `ai_reviews` / `sim_ai_reviews`.

## ZIP Contracts

Ticker ZIP: `ticker_review_{signal_date}_{run_id[:8]}.zip`
```text
metadata.json     run_id, signal_date, strategy_config_id, proposal_ids, export_timestamp
prices.csv        daily_prices for proposal tickers within signal_date ± 5 trading days
features.csv      daily_features_current for proposal tickers on signal_date
step3.csv         step3_candidates by signal_date + strategy_config_id + proposal tickers
step4.csv         step4_analysis by same scope
step5.csv         step5_proposals by proposal_ids
explanation.txt   formatted mechanical_explanation JSON per proposal
```

Simulation ZIP: `simulation_review_{sim_run_id[:8]}_{run_id[:8]}.zip`
```text
configs.json              config_ids + strategy_configs JSON from sim_runs
performance_metrics.csv   sim_config_comparisons for sim_run_id
score_buckets.csv         score distributions from sim_step3_candidates/sim_step5_proposals; buckets 0–100 width 10
setup_performance.csv     mean return by setup_type × horizon_bd from outcomes joined to sim_step4_analysis
regime_performance.csv    mean return by market_regime × horizon_bd from outcomes joined to sim_step3_candidates
drawdowns.csv             diversified-list 40bd equity-curve drawdowns ordered by signal_date; strategy_config_id, peak_date, trough_date, drawdown_pct
unresolved_outcomes.csv   sim_signal_outcomes where outcome_status = 'partial'
```

Required filenames must exist. Files must be non-empty; header-only CSV is allowed only when no matching rows exist and must be documented in spec/tests.

## Review Row Contract

After successful ZIP generation, write exactly one row.

Ticker `ai_reviews`:
```text
review_type="ticker_review"; proposal_id=first proposal_id; sim_run_id=NULL;
provider="manual"; model="none"; prompt_version="v1";
prompt_text=structured V1 plain text; selected_tickers_json=JSON array of exported tickers;
ai_response_text=NULL; human_action=NULL
```

Simulation `sim_ai_reviews`:
```text
review_type="simulation_review"; proposal_id=NULL; sim_run_id=supplied sim_run_id;
provider="manual"; model="none"; prompt_version="v1";
prompt_text=structured V1 plain text; selected_tickers_json=NULL;
ai_response_text=NULL; human_action=NULL
```

Failure behavior:
- ZIP failure → failed `ServiceResult`; no review row required.
- DB write failure after ZIP creation → failed `ServiceResult`; ZIP may remain. Document `G-ZIP-CLEANUP`.

## Prompt Text V1

Ticker:
```text
[TICKER REVIEW — {signal_date} — {strategy_config_id}]
Proposals: {ticker list}
Top proposal score: {max proposal_score_final}
Setup types: {distinct setup_types}
Estimated RR range: {min}–{max}
<brief step4/step5 summary>

Assess: are these proposals worth executing today? Flag earnings risk and macro risk.
```

Simulation:
```text
[SIMULATION REVIEW — {sim_run_id}]
Best config by expectancy: {config_id} / {expectancy}
Worst max drawdown: {config_id} / {max_drawdown_pct}
Resolved outcomes pct range: {min}–{max}
<brief sim_config_comparisons summary>

Assess: which config should be selected and what risks should be monitored?
```

Document exact V1 format as `G-PROMPT-TEXT`; Module 19 may replace it later.

## Source Retrieval Map

- Export specs: `01e_UI_AND_TESTING.md` / `UI/96_Export_Package_Specs.md`
- Schemas: `01b_SCHEMA_AND_DATA.md`, `M02_SCHEMA_SPEC.md` §3.19 / §4.9
- Enums: `01a_CORE_PRINCIPLES.md`
- `settings.EXPORTS_DIR`: `app/config/settings.py`
- `daily_features_current`: frozen Module 11 / schema manager
- Simulation tables: `M17_SIMULATION_ENGINE_SPEC.md`, `app/database/schema_manager.py`
- `ServiceResult`: Module 16 `app/services/outcomes/outcome_queue.py`

## Tests

Create `tests/test_export_package_engine.py`. All tests offline with `tmp_path` + fake injected DB manager; never touch real DB/network/provider.

Cover:
- ServiceResult success/failure; `run_id` mint/preserve for both methods.
- Pre-DB validation before DB access: invalid roles, empty `proposal_ids`, empty `sim_run_id`, empty `strategy_config_id`.
- ZIP path/filename under `settings.EXPORTS_DIR`; exact files present; non-empty or documented header-only CSV.
- Review writes: correct `review_type`, first ticker `proposal_id`, `sim_run_id`, `selected_tickers_json`, non-empty `prompt_text`, `provider="manual"`, `model="none"`.
- `drawdowns.csv`: row for negative-return path; header-only/empty for all-positive path.
- `score_buckets.csv`: every populated 10-point bucket.
- DB write failure after ZIP creation returns failed `ServiceResult`; ZIP may remain.
- Static scans: no direct `duckdb`, providers, `print()`, DDL/ATTACH; no frozen module edits except allowed files.

Run:
```text
pytest -q tests/test_export_package_engine.py
pytest -q
```

## Spec Required Content

`M18_EXPORT_PACKAGE_ENGINE_SPEC.md` must include: API, db_role validation, metadata keys, ZIP filename/content contracts, review writes, prompt_text V1, failure behavior, assumptions, open gaps (`G-PROMPT-TEXT`, `G-ZIP-CLEANUP`, `G-PRICES-WINDOW`), test summary.

## Output

Work silently. Return only:
1. Updated project zip.
2. Added/changed files.
3. Spec summary.
4. Short design notes.
5. Test commands/results.
6. Assumptions/open gaps.
7. Suggested commit message: `module18_export_package_engine_stable`
