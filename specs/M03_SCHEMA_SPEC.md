# M03_SCHEMA_SPEC.md — setup-mode (AD-22.19–22.24)

Replaces legacy M02_SCHEMA_SPEC.md. Schema version bumped to schema_v02.

## Database schema version
`DATABASE_SCHEMA_VERSION = "schema_v02"`

## Role mapping
- `prod` / `debug` → production schema (identical)
- `simulation` → simulation schema

## Production tables (22 tables)
schema_versions, pipeline_runs, pipeline_locks, ticker_master,
ticker_universe_snapshot, sector_etf_map, daily_prices, daily_features,
**setup_configs**, **risk_label_config**, sector_alias_map,
step3_candidates, step4_analysis, step5_proposals,
outcome_tracking_queue, signal_outcomes, earnings_calendar,
macro_events_calendar, data_repair_queue, feature_rebuild_log,
ai_reviews, execution_decisions

`strategy_configs`, `runtime_configs`, `config_activation_log` are **retired** and absent.

## Key setup-mode column changes vs legacy schema

### daily_features (features_v02 additions)
`ema20_slope`, `ema50_slope`, `atr_compression_score`, `pullback_depth_pct`,
`swing_high`, `swing_low`, `support_level`, `resistance_level`,
`next_resistance_level`, `base_high`, `base_low`, `range_width_pct`,
`range_duration`, `range_tightness_score`, `volume_dry_up_score`,
`volume_expansion_score`, `relative_strength_vs_spy`

### step3_candidates
Removed: `strategy_config_id`, `screening_score`, `passed_hard_filters`, `hard_filter_fail_reasons`, `soft_score_components`
Added: `passed_eligibility BOOLEAN NOT NULL`, `routing_status VARCHAR NOT NULL`,
`routing_fail_reason VARCHAR`, `routed_setup_types JSON`, `eligibility_score DOUBLE`

### step4_analysis
Removed: `strategy_config_id`, `breakout_quality_score`, `squeeze_score`, `timing_score`, `confirmation_score`
Key: `setup_config_id VARCHAR NOT NULL`, `setup_type VARCHAR NOT NULL`,
`setup_passed BOOLEAN NOT NULL`, `target_is_structural BOOLEAN`, `market_regime VARCHAR` (never defaulted)

### step5_proposals
Removed: `strategy_config_id`, `rank_position`
Added: `setup_config_id`, `setup_type NOT NULL`, `risk_score`, `risk_label`,
`risk_reasons`, `disposition NOT NULL`, `target_is_structural`,
`support_level`, `resistance_level`, `next_resistance_level`, `earnings_days`, `setup_reasons`

### signal_outcomes
Added: `setup_type VARCHAR NOT NULL`, `risk_label VARCHAR`, `stop_price_raw`,
`target_price_raw`, `stop_hit BOOLEAN`, `target_hit BOOLEAN`

## Production indexes (11)
idx_daily_prices_ticker_date, idx_daily_features_ticker_date,
idx_step3_run_date, **idx_step4_run_setup**, idx_step5_run_date_selected,
**idx_step5_run_setup**, idx_step5_run_raw_rank, idx_step5_run_div_rank,
**idx_outcomes_setup_date**, idx_queue_status_eval, idx_repair_status

## Production views (2, unchanged)
daily_features_current (MAX feature_schema_version),
selected_proposals_current (in_diversified_top_n = TRUE)

## Simulation tables
setup_configs, risk_label_config, sector_alias_map shared with prod schema.
sim_step3_candidates, sim_step4_analysis, sim_step5_proposals, sim_signal_outcomes
updated with setup-mode columns (setup_config_id, setup_type, risk_label, etc.).
sim_config_comparisons adds setup_type, risk_label, stop_hit_rate, target_hit_rate.

## apply_schema() public API (unchanged)
```python
apply_schema(db_role: str) -> ServiceResult
apply_prod_schema() -> ServiceResult
apply_debug_schema() -> ServiceResult
apply_simulation_schema() -> ServiceResult
```
