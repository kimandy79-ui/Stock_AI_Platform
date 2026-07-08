# 01c_FORMULAS_AND_CONFIGS

Status: split active source-of-truth file for Claude Project Files.
Generated from `01_MASTER_ARCHITECTURE_SOURCE_OF_TRUTH_MERGED.md` on 2026-05-31.
Rewritten 2026-06-19 for the setup-mode migration (AD-22.19–22.24).
Corrected 2026-06-19 per architect review (fixes 1, 4, 6, 7, 9, 10).

Step 3 = universal eligibility + setup routing (runs ONCE per signal date).
Step 4 = setup-specific validation + trade plan (iterates per setup_config_id).
Step 5 = risk labeling + disposition + ranking.

Do not edit formulas, schema, or rules here unless intentionally updating the canonical project source.

---

## FILE: `FORMULAS/60_Feature_Formulas_Complete.md`

# Feature Formulas Complete (features_v02)

All formulas use rows with date <= feature_cutoff_date. No look-ahead.

## EMA
EMA20/50/200 on close_adj.

## EMA Alignment Score
- 100 if EMA20 > EMA50 > EMA200
- 50 if close_adj > EMA200 but full alignment is false
- 0 otherwise

## EMA slopes (features_v02)
```
ema20_slope = ema20_t / ema20_{t-5} - 1
ema50_slope = ema50_t / ema50_{t-10} - 1
```

## RSI14
Wilder RSI14 on close_adj.

## ROC20
`roc20 = close_adj_t / close_adj_{t-20} - 1`

## ATR14 / ATR%
Wilder ATR using adjusted OHLC.
`atr_pct = atr14 / close_adj_t`

## ATR compression score (features_v02)
```
atr_compression_score = 100 * (1 - min(atr14_current / mean(atr14 over prior 60 td), 1))
```
Clip 0–100. Higher = more compressed (tighter range).

## RVOL20
`rvol20 = volume_raw_t / mean(volume_raw over t-20 to t-1)`

## Avg volume / dollar volume 20d
`avg_dollar_volume_20d = mean(close_raw * volume_raw over prior 20 trading days)`

## Distance from 52W high / pullback from recent high
```
distance_from_52w_high_pct = close_adj_t / max(close_adj over 252 td ending t) - 1
pullback_from_recent_high_pct = close_adj_t / max(close_adj over 20 td ending t) - 1
```

## Pullback depth (features_v02)
`pullback_depth_pct = (max(high_adj over 20 td ending t) - close_adj_t) / max(high_adj over 20 td ending t)`

## Structural levels (features_v02)

All structural levels are computed on adjusted prices, then RAW-CONVERTED
for trade-plan use (fix 6):

```
level_raw = level_adj * (close_raw_t / close_adj_t)
```

Apply this conversion to `swing_high`, `swing_low`, `support_level`,
`resistance_level`, `next_resistance_level`, `base_high`, `base_low` before
using them in stop/target/RR formulas. The `daily_features` table stores the
adjusted values; the conversion is applied at the Step 4 trade-plan stage.

**Definitions (adjusted basis, stored in daily_features):**

- `swing_high`: most recent confirmed swing high — pivot over lookback 20 td,
  requiring 2-bar confirmation on each side (i.e. the bar is higher than the
  2 bars before and 2 bars after it).
- `swing_low`: most recent confirmed swing low (same pivot method).
- `support_level`: nearest `swing_low` value strictly below `close_adj_t`;
  fallback to `ema50` if no qualifying swing low exists.
- `resistance_level`: nearest `swing_high` value strictly above `close_adj_t`;
  fallback to `max(high_adj over prior 20 td)` if none.
- `next_resistance_level`: next `swing_high` above `resistance_level`;
  fallback to `max(high_adj over prior 252 td)` (52-week high) if none.
- `base_high` / `base_low`: high/low of the most recent consolidation window,
  defined as the longest contiguous run (up to 60 td) where the daily range
  stays within `1.5 * median_true_range_60d`.
- `range_width_pct = (base_high - base_low) / base_low`
- `range_duration`: number of bars in the consolidation window.
- `range_tightness_score = 100 * (1 - min(range_width_pct / 0.20, 1))` — clip 0–100.

## Breakout proximity / consolidation score (retained from v01)
`breakout_proximity = (close_adj_t - max(high_adj over prior 20 td)) / atr14_t`
Negative when below the 20-day high; zero or positive at/above it.

`consolidation_score`: ATR/range/volume contraction blend (v01 formula), clip 0–100.

## Volume dry-up / expansion (features_v02)
```
volume_dry_up_score  = 100 * (1 - min(mean(volume_raw over prior 10 td) / mean(volume_raw over prior 60 td), 1))
volume_expansion_score = 100 * min(max(rvol20 - 1, 0) / 1.0, 1)
```

## Relative strength
```
relative_strength_vs_spy    = ticker_20d_return_adj - spy_20d_return_adj
sector_relative_strength    = ticker_20d_return_adj - sector_etf_20d_return_adj
```
If sector is unmapped, `sector_relative_strength` = NULL; no sector bonus/penalty applied.

### Cross-sectional RS percentile (features_v03, P1.1)
```
roc126             = close_adj / close_adj[126 trading days ago] - 1
rs_percentile_126d = percentile_rank(roc126, within: all active tickers
                      processed for this signal_date with a valid roc126)
```
Distinct mechanism from `relative_strength_vs_spy`/`sector_relative_strength`
above — those are time-series spreads against one benchmark; this is a
same-day cross-sectional rank (0-100) against the active universe. Flat
126-trading-day window, no sub-period weighting, no skip-most-recent-month
adjustment for V1 — a skip-month variant was considered and deliberately
deferred pending outcome data (consistent with the platform's
no-pre-diagnostic-tuning principle), not a gap.

`NULL` when the ticker has <126 bars of history. A ticker with a valid
`roc126` but no other active ticker that day also has one ranks at `100.0`
(not `NULL`) — percentile rank degrades gracefully for a lone population
member rather than being undefined, unlike a z-score.

Percentile granularity (`100 / (n-1)` points per rank) and statistical
stability both scale with `n` (the day's active universe size) — coarse and
less stable at small `n` (e.g. a 50-ticker dev/test universe), fine-grained
and stable at full production scale (hundreds to low-thousands of tickers).
This is a property of whichever universe size is active that day, not
something the formula itself corrects for.

Scoring input only (per-setup-type scoring-weight wiring is a separate,
future decision — not part of this addition). No hard gate.

---

## FILE: `FORMULAS/61_Step3_Universal_Eligibility.md`

# Step 3 — Universal Eligibility + Setup Routing (fix 1, 3, 4, 5)

**Step 3 runs exactly once per signal date.** It does NOT iterate per setup
config. It produces `step3_candidates` with one row per universe ticker.

## Inputs (fix 3)
- `daily_features_current` — feature columns
- `daily_prices` WHERE date = signal_date — `close_raw`, `data_quality_status`,
  `open_raw`, `high_raw`, `low_raw`, `volume_raw` for OHLCV anomaly checks
- `ticker_master` — `symbol_type`

## Universal eligibility config (fix 4)
Universal filters are controlled by a **single global config** — either a
dedicated `universe` config section or the shared `universe` block that ALL
active setup configs must carry identically. Different `min_price` or
`min_avg_dollar_volume_20d` per setup type is NOT allowed. If a per-setup
universe block is present, all four must be identical; the service layer asserts
this before running Step 3. Divergence is a configuration error, not a routing
decision.

## Eligibility filters (ALL must pass to be eligible)
Fail (`passed_eligibility = FALSE`, `routing_status = 'ineligible'`) if any:
- `feature_ready != TRUE`
- `symbol_type != 'stock'`
- `close_raw < min_price`
- `avg_dollar_volume_20d < min_avg_dollar_volume_20d`
- `data_quality_status != 'ok'`
- OHLCV anomaly: `high_raw < low_raw`, `close_raw <= 0`, `open_raw <= 0`,
  `volume_raw < 0`

Not eligibility gates (setup-specific only): rvol20, setup score, momentum,
atr_pct, ema extension, consolidation quality.

## Eligibility score
Coarse tradability score for diagnostics/ordering only. Does NOT gate:
```
eligibility_score = 0.5 * liquidity_norm + 0.3 * price_norm + 0.2 * history_norm
```
Each norm is min-max scaled to 0–100 within the day's universe. Never rejects.

## Setup routing (for eligible tickers)
Evaluate routing predicates to determine which setup_types this ticker
qualifies for. Record ALL matching types in `routed_setup_types` (JSON array).
A ticker may route into multiple setup types simultaneously.

**Routing predicates (coarse gating only; full validation is in Step 4):**
- `breakout`: `breakout_proximity >= -1.0` AND `range_duration >= 10`
- `pullback`: `close_adj > ema200` AND `pullback_from_recent_high_pct BETWEEN -0.20 AND -0.02` AND `ema20 > ema50`
- `trend_continuation`: `ema_alignment_score >= 50` AND `ema50_slope > 0` AND `close_adj > ema50`
- `consolidation_base`: `range_tightness_score >= 50` AND `range_duration >= 10`

A ticker matching no predicate: `routing_status = 'no_route'`,
`routing_fail_reason = 'no_route'`, not analyzed further.
A ticker matching one or more: `routing_status = 'routed'`,
`routed_setup_types = [<list of matching setup_types>]`.

---

## FILE: `FORMULAS/62_Step4_Setup_Validation.md`

# Step 4 — Setup-Specific Validation, Scoring, and Trade Plan (fix 1, 5, 6, 7, 9, 10)

**Step 4 iterates active `setup_config_id`s matching each ticker's
`routed_setup_types`.** It does NOT re-classify or re-route. One
`step4_analysis` row is written per (ticker, setup_type) pair.

## Variable definitions (fix 7)

| Term used in formulas | Definition |
|---|---|
| `entry_proxy_raw` | `close_raw` on signal_date (from daily_prices) |
| `atr_raw` | `atr14 * (close_raw / close_adj)` — ATR in raw-price units |
| `buffer_atr` | `buffer_atr_multiple * atr_raw`; `buffer_atr_multiple` from setup config (default 0.25) |
| `resistance_level_raw` | `resistance_level * (close_raw / close_adj)` |
| `next_resistance_raw` | `next_resistance_level * (close_raw / close_adj)` |
| `support_raw` | `support_level * (close_raw / close_adj)` |
| `base_high_raw` | `base_high * (close_raw / close_adj)` |
| `base_low_raw` | `base_low * (close_raw / close_adj)` |
| `swing_high_raw` | `swing_high * (close_raw / close_adj)` |
| `swing_low_raw` | `swing_low * (close_raw / close_adj)` |
| `prior_swing_high_raw` | `swing_high_raw` — the most recent confirmed swing high above entry |
| `recent_swing_low_raw` | `swing_low_raw` — most recent confirmed swing low below entry |
| `ema_area_raw` | `min(ema20, ema50) * (close_raw / close_adj)` |
| `higher_low_raw` | most recent raw price bar that was a higher swing_low than the one before it; if unavailable, use `recent_swing_low_raw` |
| `measured_move_raw` | `base_high_raw + (base_high_raw - base_low_raw)` (consolidation measured move) |
| `range_high_raw` | `base_high_raw` |
| `top_n` | from `risk_label_config.ranking.top_n` (default 20); controls final proposal Top-N selection. Setup configs must NOT control final Top-N. |
| `confirmation_score` | sub-score for volume/RVOL confirmation within setup scoring (see scoring_weights per setup) |

## Raw/adjusted conversion rule (fix 6)
All structural levels stored in `daily_features` are on an **adjusted** basis.
Before any stop/target/RR calculation, convert with:
```
level_raw = level_adj * (close_raw_t / close_adj_t)
```
The conversion uses the signal-date `close_raw` / `close_adj` ratio from
`daily_prices`. Store the raw-converted values in `step4_analysis` columns.

## Entry proxy (all setups)
`entry_proxy_raw = close_raw` on signal_date. Used for stop/target/RR
estimation. Actual next-day entry recorded later by outcome tracking.

Gap warning: if `abs(open_raw_next_day / entry_proxy_raw - 1) > 0.05`, log
warning; do NOT recompute the signal.

## Market regime missing / unknown (fix 9)
If `market_regime IS NULL` or equals any unrecognised value:
- `market_score = 0`
- `disposition` may be at most `WATCHLIST_ONLY` (even if setup passed and RR
  is sufficient); BUY is blocked.
- Record `setup_fail_reason` or `mechanical_explanation` noting the regime gap.
- Do NOT default to `neutral`.

## BREAKOUT (config: setup_breakout_v1)

> **Note on the stop-distance gate (all four setup types below):** the
> `stop_distance_pct <= max_stop_distance_pct` ceiling is a **risk-sizing**
> gate, not a setup-validation gate. It requires the actual structural stop
> price, which is only computed in Step 5. It is enforced there, universally
> across all setup types, via `risk_label_config.buy_rules.max_stop_distance_pct`
> (see Step 5 / risk labeling section) — it is not duplicated per-setup at
> Step 4. What Step 4 enforces instead, per setup, is an ATR-normalized
> minimum stop-tightness floor (below), which needs only `atr_pct` and
> guards against stops so tight they'd be triggered by normal ATR noise.

**Hard checks (any failure → setup_passed = FALSE):**
- `resistance_level` exists (not NULL)
- `breakout_proximity` in `[breakout_prox_min, breakout_prox_max]`
- `range_duration >= min_base_duration`
- RVOL hard gate: `rvol20 >= min_rvol_breakout` (when `rvol_is_hard = TRUE`)
- `stop_distance_atr = stop_distance_pct / atr_pct >= min_atr_stop_floor_multiple`

**Soft checks (score contributions):**
- Close strength: `(close_raw - low_raw) / (high_raw - low_raw) >= min_close_strength`
- Target room check (structural only, fix 10): use structural target (see below)
  to verify `estimated_rr >= min_rr`; if only fixed-R target available, record
  `target_is_structural = FALSE`, flag in explanation, but do NOT count target
  room as confirmed.

**Trade plan:**
- `stop_price_raw = min(base_low_raw, resistance_level_raw - k_atr_stop * atr_raw) - buffer_atr`
- `target_price_raw` (structural, priority order):
  1. `next_resistance_raw` if available
  2. `prior_swing_high_raw` if above entry and not same as resistance
  3. `measured_move_raw` (breakout measured move)
  4. Fallback (fix 10): `entry_proxy_raw + min_rr * (entry_proxy_raw - stop_price_raw)` — fixed-R only if no structural target; `target_is_structural = FALSE`; log warning.

## PULLBACK (config: setup_pullback_v1)

**Hard checks:**
- `close_adj > ema200`
- `ema20 > ema50`
- `pullback_depth_pct <= max_pullback_depth`
- `close_raw >= support_raw * (1 - support_break_tol)`
- `stop_distance_atr = stop_distance_pct / atr_pct >= min_atr_stop_floor_multiple`
- RVOL: **never a hard reject** (`rvol_is_hard = FALSE`). Low RVOL → soft penalty only.

**Soft checks:**
- Proximity to support/EMA: `abs(distance_to_ema20_pct) <= pull_band` OR `close_adj near support_level`
- Higher-low structure: `recent_swing_low_raw > prior prior swing low` (trend of higher lows intact)
- Target room (structural only, fix 10)

**Trade plan:**
- `stop_price_raw = min(support_raw, recent_swing_low_raw, ema_area_raw) - buffer_atr`
- `target_price_raw` (structural, priority order):
  1. `prior_swing_high_raw`
  2. `next_resistance_raw`
  3. Fixed-R fallback if neither available; `target_is_structural = FALSE`.

## TREND_CONTINUATION (config: setup_trend_continuation_v1)

**Hard checks:**
- `ema_alignment_score >= min_ema_alignment`
- `ema50_slope > min_ema50_slope`
- `close_adj > ema50`
- `close_adj > ema200`
- `roc20 BETWEEN roc_min AND roc_max`
- `distance_to_ema50_pct <= max_ext` (not too extended)
- `stop_distance_atr = stop_distance_pct / atr_pct >= min_atr_stop_floor_multiple`
- RVOL: soft confirmation only. `rvol_is_hard = FALSE`.

**Soft checks:**
- `relative_strength_vs_spy > 0` (RS bonus)
- `sector_relative_strength > 0` (sector RS bonus if available)
- Volume health (rvol20 >= `rvol_moderate_threshold`)
- Target room (structural only, fix 10)

**Trade plan:**
- `stop_price_raw = max(higher_low_raw, recent_swing_low_raw) - buffer_atr`
  If no higher_low available: `stop_price_raw = entry_proxy_raw - k_atr_stop * atr_raw`
- `target_price_raw` (structural, priority order):
  1. `next_resistance_raw`
  2. `measured_move_raw` (continuation measured move: `entry_proxy_raw + (entry_proxy_raw - recent_swing_low_raw)`)
  3. Fixed-R fallback; `target_is_structural = FALSE`.

## CONSOLIDATION_BASE (config: setup_consolidation_base_v1)

**Hard checks:**
- `range_tightness_score >= min_tightness`
- `atr_pct <= max_atr_pct`
- `base_low_raw <= close_raw <= base_high_raw` (price still inside base)
- `range_duration >= min_range_duration`
- `days_to_earnings_bd > min_earnings_days` OR within earnings penalty band
- `stop_distance_atr = stop_distance_pct / atr_pct >= min_atr_stop_floor_multiple`
- RVOL: **not required** (`rvol_required = FALSE`). Controlled/low volume inside base is acceptable.

**Soft checks:**
- `atr_compression_score >= min_compression`
- `volume_dry_up_score >= min_dry_up`
- Support/resistance clarity
- Target room (structural only, fix 10)

**Trade plan:**
- `stop_price_raw = base_low_raw - buffer_atr` (or `support_raw - buffer_atr` if lower)
- `target_price_raw` (structural, priority order, position-aware):
  - Price in lower/middle of range (`close_raw < base_low_raw + 0.66 * (base_high_raw - base_low_raw)`):
    1. `base_high_raw` / `range_high_raw`
    2. `next_resistance_raw`
    3. Fixed-R fallback; `target_is_structural = FALSE`.
  - Price near upper range (`close_raw >= base_low_raw + 0.66 * (base_high_raw - base_low_raw)`):
    1. `measured_move_raw` (breakout from base: `base_high_raw + (base_high_raw - base_low_raw)`)
    2. `next_resistance_raw`
    3. Fixed-R fallback; `target_is_structural = FALSE`.
  Note: a hard check already requires `close_raw <= base_high_raw`, so the
  upper-range branch applies to tickers near (but not above) the base ceiling.

## Setup score
```
setup_score = sum(w_i * component_score_i)   -- weights from config, sum to 1.0
```
Component scores are 0–100. `setup_passed = setup_score >= min_setup_score AND all hard checks pass`.

## Estimated RR (always an output, fix 10)
```
estimated_rr = (target_price_raw - entry_proxy_raw) / (entry_proxy_raw - stop_price_raw)
```
`estimated_rr` is ALWAYS an output of the trade plan. It is never a fixed
constant. Fixed-R is only the explicit fallback formula for computing
`target_price_raw` when no structural target is available.

## Target-room validation rule (fix 10)
The target-room check in hard/soft scoring MUST use the structural target.
If `target_is_structural = FALSE` (fixed-R fallback was used), the target-room
component score is 0 and this is flagged in `explanation_json`. The fixed-R
fallback does NOT constitute evidence of adequate target room.

## Earnings / macro penalty
```
earnings_penalty = penalty_points_max * (1 - days_to_earnings_bd / avoid_within_bd)
```
Applied when `days_to_earnings_bd <= avoid_within_bd`. Negative points.
Macro penalty: flat `penalty_points` when within the event window.

---

## FILE: `FORMULAS/63_Step5_Risk_Labeling_And_Proposals.md`

# Step 5 — Risk Labeling, Disposition, Proposal Scoring

## Risk score (config: risk_label_config_v1)
Risk is assigned after the trade plan exists. Never decides setup validity.
Objective factors, each normalized to a 0–100 risk contribution (higher = riskier):
- `stop_distance_pct`
- `atr_pct`
- `ema_extension` (distance_to_ema20_pct or distance_to_ema50_pct, whichever larger)
- `liquidity` (inverse of avg_dollar_volume_20d normalized)
- `earnings_proximity` (closer earnings = higher risk)
- `estimated_rr` (lower RR = higher risk)
- `market_regime` (bear/high_risk/extreme_risk = high risk contribution; NULL = maximum risk contribution, fix 9)
- `setup_confirmation` (inverse of setup_score, normalized)

```
risk_score = sum(w_j * factor_j)   -- weights from risk_label_config, sum to 1.0
```
Clip to 0–100. `risk_reasons` records top contributing factors.

## Risk label thresholds
```
low    : risk_score <= low_max           (default 33)
medium : low_max < risk_score <= med_max (default 66)
high   : risk_score > med_max
```

## Disposition rules (fix 9)
- `BUY`: `setup_passed = TRUE` AND `estimated_rr >= min_rr_for_buy` AND
  `risk_label in allowed_buy_labels` AND `market_regime NOT IN block_market_regimes`
  AND `market_regime IS NOT NULL` (NULL regime blocks BUY, allows WATCHLIST_ONLY at most)
- `WATCHLIST_ONLY`: `setup_passed = TRUE` but fails one or more BUY gates
  (e.g. RR slightly low, high risk label, near earnings, NULL or high_risk/extreme_risk regime)
- `REJECTED`: `setup_passed = FALSE`

## Raw proposal score
**Spec-drift correction (2026-07-05):** this section previously documented a
4-term formula (0.45/0.25/0.20/0.10) that no longer matched the shipped code.
A stop-distance-quality term was added to the implementation in a prior
session's diagnostics-driven fix (2026-06-27) without updating this file. The
formula below is the actual current 5-term base plus the two Phase 3
AI-review terms added in this delta — the correction and the new terms are
both part of this same documented change, per project rule (formula changes
are reviewed edits, not silent ones).

```
base = 0.40 * setup_score
     + 0.25 * rr_score
     + 0.15 * confirmation_score
     + 0.10 * market_score
     + 0.10 * stop_quality

proposal_score_raw = base
                   - contrarian_penalty_weight * contrarian_risk_score        (if present)
                   - audit_penalty_weight * (100 - audit_consistency_score)   (if present)
```

**rr_score:**
- `estimated_rr >= 3.0` → 100
- `2.2 <= estimated_rr < 3.0` → 80
- `1.8 <= estimated_rr < 2.2` → 60
- `1.3 <= estimated_rr < 1.8` → 30
- `< 1.3` → 0

**confirmation_score:** weighted volume/RVOL/pattern confirmation from setup scoring (0–100).

**market_score (fix 9):**
- bull → 100
- neutral → 60
- bear → 20
- high_risk → 0
- extreme_risk → 0
- NULL (unknown) → 0

**stop_quality:** tighter stop = higher quality; `100 * max(0, 1 - stop_distance_pct / 0.10)`
when a stop distance is known, else neutral (50).

**contrarian_risk_score / audit_consistency_score (Phase 3, 01c delta
2026-07-05):** both 0–100, both `NULL`/absent for the large majority of
proposals — the AI review passes (M19) run later and selectively relative to
Step 5's original scoring (see `M19_AI_REVIEW_ENGINE_SPEC.md`), so most
proposals are scored exactly as before this delta. When present, each
contributes an **additive downgrade-only penalty** (never a bonus):
`contrarian_penalty_weight` / `audit_penalty_weight` default to 0.10 each,
config-overridable via `risk_label_config.ai_review.{contrarian_penalty_weight,
audit_penalty_weight}`. A strong audit failure
(`audit_consistency_score < risk_label_config.ai_review.audit_consistency_min_for_buy`,
default 40) additionally forces `disposition = WATCHLIST_ONLY` outright
(`rejection_reason = 'audit_consistency_below_threshold'`), independent of
the score penalty above — this is a hard gate, not a soft weighting.

Only `BUY` and `WATCHLIST_ONLY` dispositions are scored and ranked.
`REJECTED` rows are stored for diagnostics, excluded from ranking.

## Raw ranking
Sort `BUY` then `WATCHLIST_ONLY` by `proposal_score_raw DESC`, `estimated_rr DESC`,
`ticker ASC`. Assign `raw_rank`; `in_raw_top_n = raw_rank <= top_n`.
`top_n` is sourced exclusively from `risk_label_config.ranking.top_n` (default 20).
Setup configs do not control final Top-N.

## Diversified ranking (hard cap default)
Process candidates in raw_rank order:

**hard_cap_enabled = TRUE (default):**
If candidate exceeds `sector_max_positions` or `industry_max_positions`: reject
from diversified list, `diversified_rank = NULL`,
`rejection_reason = 'sector_cap' | 'industry_cap'`, no soft penalty.
Otherwise: accept, assign next `diversified_rank`,
`proposal_score_final = proposal_score_raw`.

**hard_cap_enabled = FALSE:**
No hard rejection. `proposal_score_final = proposal_score_raw * sector_penalty * industry_penalty`.

## Final selection semantics
```
selected_flag     = in_diversified_top_n
selected_top_n    = in_raw_top_n OR in_diversified_top_n
```

---

## FILE: `FORMULAS/64_Outcome_Calculation_Rules.md`

# Outcome Calculation Rules

## Entry definitions
Entry date = next US trading day after `signal_date`.
`entry_price_raw = next trading day open_raw` (audit reference).
`entry_price_sim = open_raw * (1 + slippage_bps / 10000)` (long-only V1).
`entry_price_sim` is the denominator for all return/MFE/MAE/R-multiple calculations.

## Horizon returns (adjusted close)
`return_Nbd_pct = close_adj_Nbd / entry_price_sim - 1` for N in {5, 10, 20, 40}.

## MFE / MAE
```
mfe_40bd_pct = max(high_adj over entry_date..40bd) / entry_price_sim - 1
mae_40bd_pct = min(low_adj  over entry_date..40bd) / entry_price_sim - 1
```

## Stop / target hit
```
stop_hit   = TRUE if min(low_raw  over entry_date..eval) <= stop_price_raw
target_hit = TRUE if max(high_raw over entry_date..eval) >= target_price_raw
```
`stop_price_raw` and `target_price_raw` from the proposal; stored in
`signal_outcomes` for audit.

## Realized R multiple
```
realized_r_multiple = (exit_price_sim - entry_price_sim) / (entry_price_sim - stop_price_raw)
```

## Outcome grouping dimensions
Outcomes carry `setup_type` and `risk_label`. Report expectancy, win rate,
stop-hit rate, target-hit rate, and MFE/MAE per setup type and risk label.

## Missing eval candle
Repair first. If unresolved after 3 business days: mark `UNRESOLVABLE`, exclude
from aggregate metrics.

---

## FILE: `CONFIG/20_Setup_Config_Breakout.json`

```json
{
  "config_id": "setup_breakout_v1",
  "setup_type": "breakout",
  "version": "breakout_v1",
  "universe": {
    "min_price": 5,
    "min_avg_dollar_volume_20d": 10000000,
    "allowed_symbol_types": ["stock"],
    "exclude_benchmarks": true
  },
  "features": { "feature_schema_version": "features_v02" },
  "validation": {
    "breakout_prox_min": -1.0,
    "breakout_prox_max": 0.5,
    "min_base_duration": 10,
    "min_rvol_breakout": 1.5,
    "rvol_is_hard": true,
    "min_close_strength": 0.5,
    "max_stop_distance_pct": 0.10,
    "k_atr_stop": 1.0,
    "buffer_atr_multiple": 0.25,
    "min_rr": 1.8,
    "min_setup_score": 55
  },
  "scoring_weights": {
    "resistance_clarity": 0.20,
    "breakout_confirmation": 0.25,
    "volume_expansion": 0.20,
    "base_quality": 0.20,
    "target_room": 0.15
  },
  "ranking": { "top_n": null },  -- reserved; Top-N is controlled by risk_label_config only
  "earnings": { "avoid_within_bd": 5, "penalty_points_max": -15 },
  "macro_event_risk": { "enabled": true, "window_bd_before": 1, "window_bd_after": 1, "penalty_points": -10 }
}
```

---

## FILE: `CONFIG/21_Setup_Config_Pullback.json`

```json
{
  "config_id": "setup_pullback_v1",
  "setup_type": "pullback",
  "version": "pullback_v1",
  "universe": {
    "min_price": 5,
    "min_avg_dollar_volume_20d": 10000000,
    "allowed_symbol_types": ["stock"],
    "exclude_benchmarks": true
  },
  "features": { "feature_schema_version": "features_v02" },
  "validation": {
    "pull_band": 0.04,
    "max_pullback_depth": 0.12,
    "support_break_tol": 0.02,
    "k_atr_stop": 1.2,
    "buffer_atr_multiple": 0.25,
    "min_rr": 1.8,
    "rvol_is_hard": false,
    "rvol_bonus_threshold": 1.3,
    "max_stop_distance_pct": 0.10,
    "min_setup_score": 55
  },
  "scoring_weights": {
    "uptrend_intact": 0.25,
    "support_ema_hold": 0.25,
    "pullback_depth": 0.20,
    "trend_structure": 0.15,
    "rr": 0.15
  },
  "ranking": { "top_n": null },  -- reserved; Top-N is controlled by risk_label_config only
  "earnings": { "avoid_within_bd": 5, "penalty_points_max": -15 },
  "macro_event_risk": { "enabled": true, "window_bd_before": 1, "window_bd_after": 1, "penalty_points": -10 }
}
```

---

## FILE: `CONFIG/22_Setup_Config_TrendContinuation.json`

```json
{
  "config_id": "setup_trend_continuation_v1",
  "setup_type": "trend_continuation",
  "version": "trend_continuation_v1",
  "universe": {
    "min_price": 5,
    "min_avg_dollar_volume_20d": 10000000,
    "allowed_symbol_types": ["stock"],
    "exclude_benchmarks": true
  },
  "features": { "feature_schema_version": "features_v02" },
  "validation": {
    "min_ema_alignment": 50,
    "min_ema50_slope": 0.0,
    "roc_min": 0.02,
    "roc_max": 0.40,
    "max_ext": 0.15,
    "k_atr_stop": 1.5,
    "buffer_atr_multiple": 0.25,
    "min_rr": 1.8,
    "rvol_is_hard": false,
    "rvol_moderate_threshold": 1.2,
    "max_stop_distance_pct": 0.10,
    "min_setup_score": 55
  },
  "scoring_weights": {
    "trend_health": 0.25,
    "relative_strength": 0.20,
    "extension": 0.15,
    "momentum": 0.20,
    "volume_health": 0.10,
    "target_room": 0.10
  },
  "ranking": { "top_n": null },  -- reserved; Top-N is controlled by risk_label_config only
  "earnings": { "avoid_within_bd": 5, "penalty_points_max": -15 },
  "macro_event_risk": { "enabled": true, "window_bd_before": 1, "window_bd_after": 1, "penalty_points": -10 }
}
```

---

## FILE: `CONFIG/23_Setup_Config_ConsolidationBase.json`

```json
{
  "config_id": "setup_consolidation_base_v1",
  "setup_type": "consolidation_base",
  "version": "consolidation_base_v1",
  "universe": {
    "min_price": 5,
    "min_avg_dollar_volume_20d": 10000000,
    "allowed_symbol_types": ["stock"],
    "exclude_benchmarks": true
  },
  "features": { "feature_schema_version": "features_v02" },
  "validation": {
    "min_tightness": 60,
    "max_atr_pct": 0.05,
    "min_compression": 50,
    "min_range_duration": 10,
    "min_dry_up": 40,
    "min_earnings_days": 5,
    "k_atr_stop": 1.0,
    "buffer_atr_multiple": 0.25,
    "min_rr": 1.8,
    "rvol_required": false,
    "max_stop_distance_pct": 0.10,
    "min_setup_score": 55
  },
  "scoring_weights": {
    "range_tightness": 0.25,
    "support_resistance_clarity": 0.20,
    "atr_compression": 0.20,
    "volume_dry_up": 0.15,
    "breakout_readiness": 0.10,
    "stop_tightness": 0.10
  },
  "ranking": { "top_n": null },  -- reserved; Top-N is controlled by risk_label_config only
  "earnings": { "avoid_within_bd": 5, "penalty_points_max": -15 },
  "macro_event_risk": { "enabled": true, "window_bd_before": 1, "window_bd_after": 1, "penalty_points": -10 }
}
```

---

## FILE: `CONFIG/24_Risk_Label_Config.json`

```json
{
  "config_id": "risk_label_config_v1",
  "version": "risk_v1",
  "factor_weights": {
    "stop_distance_pct": 0.20,
    "atr_pct": 0.15,
    "ema_extension": 0.10,
    "liquidity": 0.10,
    "earnings_proximity": 0.10,
    "estimated_rr": 0.15,
    "market_regime": 0.10,
    "setup_confirmation": 0.10
  },
  "thresholds": { "low_max": 33, "med_max": 66 },
  "buy_rules": {
    "min_rr_for_buy": 1.8,
    "allowed_buy_labels": ["low", "medium"],
    "block_market_regimes": ["extreme_risk"],
    "block_if_regime_null": true
  },
  "market_regime": { "high_risk_vix": 25, "extreme_risk_vix": 30 },
  "ranking": { "top_n": 20 },
  "diversification": {
    "hard_cap_enabled": true,
    "sector_max_positions": 3,
    "industry_max_positions": 2,
    "sector_penalty_factor": 0.9,
    "industry_penalty_factor": 0.85,
    "penalty_applies_before_cap_only": true
  },
  "sector_etf_mapping": {
    "Technology": "XLK", "Financials": "XLF", "Healthcare": "XLV",
    "Consumer Discretionary": "XLY", "Consumer Staples": "XLP",
    "Communication Services": "XLC", "Industrials": "XLI",
    "Energy": "XLE", "Materials": "XLB", "Utilities": "XLU",
    "Real Estate": "XLRE"
  },
  "simulation": {
    "entry_rule": "next_trading_day_open_raw",
    "return_price_type": "adjusted_close",
    "slippage_bps": 10,
    "commission_per_trade": 0,
    "horizons_bd": [5, 10, 20, 40],
    "min_resolved_outcomes_pct": 0.85,
    "max_drawdown_constraint_pct": 25
  }
}
```

> NOTE: all threshold values are migration starting points, not tuned values.
> Tuning waits for setup-mode funnel diagnostics.
