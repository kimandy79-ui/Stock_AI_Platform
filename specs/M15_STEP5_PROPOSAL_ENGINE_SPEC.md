# M15 Step 5 Proposal Engine Spec (setup-mode migration)

Status: Phase 5 accepted. Rewritten for setup-mode (AD-22.19–22.24).

## Purpose

Reads all Step 4 analysis rows for one `signal_date`, computes structural stop /
target / RR per (ticker, setup_type), assigns risk score and risk label
(low / medium / high) from `risk_label_config`, determines disposition
(BUY / WATCHLIST_ONLY / REJECTED), dedupes multi-route tickers to best
risk-adjusted route, ranks raw + diversified Top-N, writes to `step5_proposals`.

## Module location

`app/services/proposal/step5_proposal_engine.py`

## Public API

```python
engine = Step5ProposalEngine(db_manager=None)

result: ServiceResult = engine.propose(
    signal_date: date,
    risk_label_config: dict | None = None,  # loaded from DB if None
    setup_configs: dict[str, dict] | None = None,  # loaded from DB if None
    db_role: str = "prod",                  # "prod" or "debug" only
    run_id: str | None = None,
)
```

## DB roles

`prod` and `debug` only. `simulation` is rejected with `STATUS_FAILED`.

## Inputs (read-only)

- `step4_analysis` WHERE `signal_date = ?` — all rows (passed + failed)
- `ticker_master` — sector / industry for diversification
- `daily_features_current` JOIN `daily_prices` — structural levels (adj) + close_raw/close_adj for conversion
- `risk_label_config` WHERE `active_flag = TRUE` LIMIT 1
- `setup_configs` WHERE `active_flag = TRUE`

## Outputs (append-only inserts)

- `step5_proposals` — one row per candidate (passed + failed, deduped per ticker for ranked rows)

## Pipeline position

Step 4 (per setup_config_id) → **Step 5** (once per signal_date) → Outcome Queue

## Processing flow

1. Load `risk_label_config` + `setup_configs` from DB (or use injected dicts).
2. Read all `step4_analysis` rows for `signal_date`.
3. Read structural features for `signal_date`.
4. For each analysis row:
   - **setup_passed = FALSE** → REJECTED, no stop/target, risk_label = high, score = 0.
   - **setup_passed = TRUE** → compute stop/target/RR, risk score, risk label, disposition.
5. Dedupe: for each ticker, keep best `proposal_score_raw` among BUY/WATCHLIST rows.
6. Raw rank: BUY first, then WATCHLIST; sorted by score DESC, RR DESC, ticker ASC.
7. Diversification: hard-cap (default) or soft-penalty from `risk_label_config.diversification`.
8. Final semantics: `selected_flag = in_diversified_top_n`, `selected_top_n = in_raw_top_n OR in_diversified_top_n`.
9. Write all rows (ranked + rejected) in single BEGIN/COMMIT transaction.

## Stop / target formulas (01c §62)

All structural levels stored on adjusted basis; converted via:
`level_raw = level_adj * (close_raw / close_adj)`

### breakout
- `stop = min(base_low_raw, resistance_raw - k_atr * atr_raw) - buffer_atr`
- Target priority: `next_resistance_raw` → `swing_high_raw` (not same as resistance) → `measured_move_raw` (base_high + range)
- Fallback: `entry + min_rr * (entry - stop)`, `target_is_structural = False`

### pullback
- `stop = min(support_raw, swing_low_raw, ema_area_raw) - buffer_atr`
- Target: `swing_high_raw` → `next_resistance_raw`
- Fallback: fixed-R

### trend_continuation
- `stop = swing_low_raw - buffer_atr` (or ATR fallback)
- Target: `next_resistance_raw` → measured move `entry + (entry - swing_low_raw)`
- Fallback: fixed-R

### consolidation_base
- `stop = min(base_low_raw, support_raw) - buffer_atr`
- Target (position-aware):
  - Lower/middle of range → `base_high_raw` → `next_resistance_raw`
  - Near ceiling → `measured_move_raw` (base_high + range) → `next_resistance_raw`
- Fallback: fixed-R

### estimated_rr
Always an output: `estimated_rr = (target - entry) / (entry - stop)`.
Never a fixed constant. `target_is_structural = True` iff structural target found.

## Risk score (01c §63)

8-factor weighted sum from `risk_label_config.factor_weights` (each 0–100, higher = riskier):
- `stop_distance_pct`: (entry-stop)/entry, normalized at 15%
- `atr_pct`: normalized at 8%
- `ema_extension`: max(|dist_ema20|, |dist_ema50|), normalized at 20%
- `liquidity`: inverse, 100M ADV → 0 risk
- `earnings_proximity`: ≤5bd = 80, ≤10bd = 40, else 0
- `estimated_rr`: inverse, 3.0+ → 0 risk
- `market_regime`: bull=0, neutral=30, bear=70, high_risk/extreme_risk/NULL = 100
- `setup_confirmation`: inverse of setup_score

Risk label thresholds from config:
- `low`: score ≤ low_max (default 33)
- `medium`: low_max < score ≤ med_max (default 66)
- `high`: score > med_max

## Disposition rules (01c §63)

- `REJECTED`: `setup_passed = FALSE`
- `BUY`: `setup_passed AND estimated_rr >= min_rr_for_buy AND risk_label in allowed_buy_labels AND market_regime not in block_market_regimes AND market_regime IS NOT NULL` (when block_if_regime_null=True)
- `WATCHLIST_ONLY`: passed but fails any BUY gate

NULL regime → `market_score = 0`, blocks BUY (fix 9, AD-22.23).

## Proposal score (01c §63)

```
proposal_score_raw = 0.45 * setup_score
                   + 0.25 * rr_score
                   + 0.20 * confirmation_score
                   + 0.10 * market_score
```

RR tier: ≥3.0→100, ≥2.2→80, ≥1.8→60, ≥1.3→30, else 0.
Market score: bull=100, neutral=60, bear=20, high_risk/extreme_risk/NULL=0.

## Ranking

- `top_n` comes exclusively from `risk_label_config.ranking.top_n`. Setup configs do not control Top-N.
- `raw_rank`: 1-based, BUY before WATCHLIST, sorted by score DESC / RR DESC / ticker ASC.
- `in_raw_top_n = raw_rank <= top_n`.
- REJECTED rows: `raw_rank = NULL`, `in_raw_top_n = False`.

## Diversification

**Hard-cap (default, AD-22.12):** process in raw_rank order; over-cap candidates get `diversified_rank = NULL`, `rejection_reason = sector_cap | industry_cap`, `proposal_score_final = proposal_score_raw` (no double penalty).

**Soft-penalty:** `proposal_score_final = proposal_score_raw * sector_penalty^prior * industry_penalty^prior`; re-rank by final score.

`selected_flag = in_diversified_top_n`
`selected_top_n = in_raw_top_n OR in_diversified_top_n`

## Key contracts inherited from prior phases

- `ServiceResult(status, run_id, rows_processed, warnings, errors, metadata)`
- `metadata` always carries exactly: `db_role`, `signal_date`, `run_id`, `setup_config_id`, `analyses_read`, `proposals_written`, `raw_top_n_count`, `diversified_top_n_count`, `hard_cap_rejections`
- No look-ahead. No DDL. No print(). Append-only. BEGIN/COMMIT/ROLLBACK pattern.
- No `strategy_config_id`, `strategy_name`, `aggressive`, `normal`, `conservative`.

## Retired legacy behavior

Old `step5_proposal_engine.py` accepted `strategy_config`, `strategy_config_id`, `timing_score`, `screening_score`. All retired. This module uses `risk_label_config` + `setup_configs` exclusively.
