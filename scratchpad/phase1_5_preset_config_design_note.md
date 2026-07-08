# Phase 1.5 — Literature-Anchored Preset Config Seeding: Design Note

## Preset IDs added (6 total, plus the 4 existing v1 baselines = 10 in the sweep set)

| config_id | setup_type | Source rationale |
|---|---|---|
| `setup_breakout_canonical` | breakout | canonical — O'Neil/Bulkowski volume-confirmation threshold (~1.5x avg volume), entry within ~5% of pivot/resistance |
| `setup_breakout_strict` | breakout | strict — higher RVOL bar (~2.0x) + longer base (Bulkowski's longer-base/higher-reliability breakout profile) |
| `setup_consolidation_base_strict` | consolidation_base | strict — tighter ATR-compression/volume-dry-up (Minervini VCP-style "coiling" profile) |
| `setup_trend_continuation_template` | trend_continuation | template — Minervini-style idealized trend profile: strong EMA alignment, firmly positive 50EMA slope, not overextended |
| `setup_pullback_shallow` | pullback | shallow — classic "first pullback" entry: shallow depth, tight support tolerance |
| `setup_pullback_fib` | pullback | fib — deeper retracement toward classic Fibonacci 38.2–61.8% pullback zones |

All 6 seeded with `active_flag=False`, `parent_config_id` pointing at the corresponding `setup_*_v1` config (immutable clone-and-version pattern, CLAUDE.md). Every field name was cross-checked against what each validator in `m14_setup_validators.py` actually reads under `setup_config["validation"]` — no new fields invented.

## Two criteria from the coder note that are not representable as-is (flagged, not silently approximated)

1. **Breakout "RS filter active"** (`setup_breakout_strict`): `validate_breakout` never reads any relative-strength feature — only `validate_trend_continuation` does. There is no RS-related field in the breakout validator to gate on. Not implemented; would require a Step 4 code change (out of scope — presets are data only, no validator changes).
2. **Trend continuation "RS vs SPY >0 required not soft"** and **"price>50MA>150MA>200MA"** (`setup_trend_continuation_template`): `relative_strength_vs_spy`/`sector_relative_strength` are scoring-only inputs in `validate_trend_continuation` (no hard RS gate exists in code), and there is no 150-day EMA/SMA feature anywhere in the `daily_features` schema (only ema20/ema50/ema200). Approximated by raising the `relative_strength` scoring weight (0.20 → 0.30, rebalanced from `extension`/`volume_health` to keep the sum at 1.0) and requiring a high `min_ema_alignment` (80 vs v1's 50) — this is a soft emphasis, not a true hard requirement.

Also carried forward (not introduced by this task): `max_stop_distance_pct` exists in every `validation` block (v1 and presets alike) but is never actually read by any Step 4 validator — it's a Step 5/`risk_label_config` concern (`buy_rules.max_stop_distance_pct`), not a Step 4 gate. This is a pre-existing characteristic of the v1 seed shape; presets mirror it for structural parity, not because it does anything new.

## What shipped

- `app/services/config/default_configs.py`: new `PRESET_SETUP_CONFIGS: list[dict]` (6 entries) + `get_preset_setup_configs() -> list[dict]` accessor (list, not dict-by-type, since multiple presets share a `setup_type`).
- `app/services/config/config_service.py`: new `ConfigService.seed_preset_setup_configs(db_role)` — same `_INSERT_SETUP_CONFIG` / `ON CONFLICT (config_id) DO NOTHING` idempotency pattern as `seed_default_setup_configs`, but always inserts `active_flag=False` and never calls `activate_setup_config`. Standalone method, not bundled into `seed_defaults()` (keeps existing prod/debug/simulation init behavior byte-for-byte unchanged — no risk to environments that already call `seed_defaults()`).
- **Not touched:** `tools/init_simulation_db.py` (or any other `tools/init_*_db.py`). The coder note's "Modules touched" list named the config-seeding layer and the `setup_configs` table, not the CLI init scripts, and wiring in there would mean changing `init_simulation_db.py`'s tested output-message format too. `seed_preset_setup_configs(db_role)` is ready to be called directly (e.g. `ConfigService().seed_preset_setup_configs("simulation")`) by whatever Phase 2 tooling ends up loading the sweep's variant set — flagging this as an open wiring decision rather than assuming where it belongs.

## Testing

Added to `tests/test_config_service.py` (all passing, real tmp DuckDB):
- Preset set covers all 4 setup types, scoring_weights sum to 1.0, universe block matches v1 (run-wide parity invariant), `parent_config_id` references an existing v1 config_id.
- Seeding inserts exactly 6 rows; idempotent on re-seed (0 new rows second time); none are ever active; seeding presets after the v1 defaults does not change which config is active per setup_type; invalid db_role fails.
- Each preset passes `ConfigService.validate_setup_config()` (the same structural check applied to user-submitted configs).
- Every `validation` block key in every preset is a subset of the matching v1 default's `validation` keys — a direct, automated proof that no new fields were invented.

Full `test_config_service.py` suite green (58 tests). Config-adjacent suites (`test_orchestrator_config_loading.py`, `test_pipeline_orchestrator.py`, `test_sector_normalization.py`) unaffected.

## Exit criterion status

All 6 preset rows exist in `setup_configs` (seedable via `seed_preset_setup_configs`), pass validation, are idempotent on re-seed, and are never active in prod/debug. Full test suite green.

**Suggested commit message:** `phase1_5_preset_config_seed_stable`
