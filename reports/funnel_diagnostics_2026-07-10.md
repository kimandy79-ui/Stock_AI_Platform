# Funnel Diagnostics — Signal Date 2026-07-10

**Setup-mode pipeline · candidate yield & validator drop-off**

| | |
|---|---|
| DB role | `prod` |
| run_id | `29bd26e0-0c05-4d67-b771-ec6ef7e46804` |
| Rows loaded | step3 = 4078 · step4 = 3132 · step5 = 2842 |
| Generated | 2026-07-13 |
| Source | `tools/run_funnel_diagnostics.py --date 2026-07-10 --db-role prod` |

> [!WARNING]
> **Read before acting — single-date, partial-input run.**
> This is one signal date on a **low-volume session** (universe median RVOL ≈ 0.55), so the breakout RVOL shortfall below is partly a market artifact, not proof of a mis-set threshold. **Earnings & fundamentals refresh were skipped** on this run (to avoid the slow per-ticker loops), so `earnings_too_close` is under-counted and `feature_missing = 452`. These are **yield** metrics, not outcome metrics — do not tune thresholds off a single date.

---

## 1. Where the day landed

| Metric | Count | Note |
|---|--:|---|
| Universe | 4,078 | active stocks |
| Passed eligibility | 1,908 | 46.8% of universe |
| Failed eligibility | 2,170 | 53.2% |
| Routed to a setup | 1,477 | 77.4% of eligible |
| Not routed | 431 | 10.6% |
| Multi-routed | 1,111 | to 2+ setups |
| **Selected shortlist** | **20** | 0.5% of universe |

**Final trade decisions:** BUY **8** (0.3%) · WATCHLIST_ONLY **855** (30.1%) · REJECTED **1,979** (69.6%)

**Risk labels:** low 224 · medium 628 · high 1,990

---

## 2. Drop-off through the pipeline

Each rung as a share of the 4,078-ticker universe.

| Layer | Count | % universe |
|---|--:|--:|
| Ineligible — step3 hard gates | 2,170 | 53.2% |
| Eligible but not routed — no setup match | 431 | 10.6% |
| Routed → all validators failed (step4) | 614 | 15.1% |
| Validator passed → no step5 row | 0 | 0.0% |
| Step5 REJECTED — setup failed in step5 | 1,979 | 48.5% |
| Step5 WATCHLIST_ONLY → not selected | 841 | 20.6% |
| Step5 BUY → diversity-rejected | 2 | 0.0% |
| **SELECTED — final shortlist** | **20** | **0.5%** |

---

## 3. Validator pass rate by setup

Two setups are near-zero: **breakout** and **consolidation_base** convert almost nothing routed into a validated candidate.

| Setup | Routed | Pass | Fail | Pass rate | Step5 | Selected | Health |
|---|--:|--:|--:|--:|--:|--:|---|
| breakout | 713 | 14 | 699 | **2.0%** | 710 | 1 | 🔴 critical |
| pullback | 749 | 464 | 285 | **62.0%** | 616 | 9 | 🟢 healthy |
| trend_continuation | 1,075 | 662 | 413 | **61.6%** | 924 | 9 | 🟢 healthy |
| consolidation_base | 595 | 13 | 582 | **2.2%** | 592 | 1 | 🔴 critical |

> Service-raised warning: `consolidation_validator_too_strict_or_misconfigured` (routed=595, passed=13/595, pass_rate=2.2%).

Routing coverage was 100% for every setup (routed == step4 rows); 1,111 tickers were multi-routed (breakout may absorb consolidation-like candidates when both qualify).

---

## 4. Why validators fail (step4, by setup)

One rule dominates each of the two broken setups. Example shows the median offending comparison.

### breakout — 14 / 713 pass · 2.0%
| Rule | Count | % | Example |
|---|--:|--:|---|
| **rvol_below_hard_threshold** | 698 | 99.9% | actual 0.75 < min 1.5 · [A, AAPL, AAT] |
| stop_below_atr_floor | 1 | 0.1% | actual 0.30 < min 0.5 · [LEVI] |

### consolidation_base — 13 / 595 pass · 2.2%
| Rule | Count | % | Example |
|---|--:|--:|---|
| **range_tightness_too_low** | 233 | 40.0% | actual 57.0 < min 60.0 · [ABM, ABNB, ACAD] |
| price_above_base_high | 184 | 31.6% | actual 25.12 > max 21.05 · [AAT, ABCB, ACGL] |
| score_below_threshold | 107 | 18.4% | actual 42.8 < min 55.0 · [ACA, ACM, ALLY] |
| price_below_base_low | 50 | 8.6% | actual 168.59 < min 174.29 · [AMT, AQN, ARI] |
| stop_below_atr_floor | 4 | 0.7% | actual 0.13 < min 0.3 · [EFX, GNL, LADR] |
| earnings_too_close | 3 | 0.5% | actual 4bd < min 5bd · [PLD, TCBI, TFC] |
| atr_too_high | 1 | 0.2% | actual 0.1024 > max 0.05 · [MLI] |

### pullback — 464 / 749 pass · 62.0%
| Rule | Count | % | Example |
|---|--:|--:|---|
| stop_below_atr_floor | 120 | 42.1% | actual 0.08 < min 0.5 · [ABBV, ACT, AEP] |
| pullback_too_deep | 119 | 41.8% | actual 0.184 > max 0.12 · [AAON, ABSI, ACHV] |
| score_below_threshold | 24 | 8.4% | actual 49.7 < min 55.0 · [AGYS, AMPL, BWIN] |
| pullback_no_rebound_confirmation | 21 | 7.4% | [BRX, CDNS, COLM] |
| support_broken | 1 | 0.4% | actual 44.49 < min 45.17 · [XPEL] |

### trend_continuation — 662 / 1075 pass · 61.6%
| Rule | Count | % | Example |
|---|--:|--:|---|
| roc20_out_of_range | 239 | 57.9% | actual −0.024 not in [0.02, 0.4] · [AAP, ABM, ABSI] |
| too_extended_from_ema50 | 119 | 28.8% | actual 0.175 > max 0.15 · [ACHC, ACIW, ACMR] |
| stop_below_atr_floor | 45 | 10.9% | actual 0.08 < min 0.5 · [ABBV, ACA, ACT] |
| score_below_threshold | 10 | 2.4% | actual 54.2 < min 55.0 · [BDX, CNXN, EXPD] |

---

## 5. Step5 rejection reasons — all dispositions

`industry_cap` (diversity capping, working as designed) is the single largest reason, ahead of the breakout RVOL shortfall. `stop_below_atr_floor` spans every setup.

| Reason | Count | % of 2,842 | Example (actual vs threshold) |
|---|--:|--:|---|
| industry_cap | 795 | 28.0% | diversity cap — LOAR, SWBI, FDX |
| rvol_below_hard_threshold | 698 | 24.6% | 0.75 < min 1.5 |
| roc20_out_of_range | 239 | 8.4% | −0.024 ∉ [0.02, 0.4] |
| range_tightness_too_low | 233 | 8.2% | 57.0 < min 60.0 |
| price_above_base_high | 184 | 6.5% | 25.12 > max 21.05 |
| stop_below_atr_floor | 170 | 6.0% | 0.08 < min 0.5 |
| score_below_threshold | 141 | 5.0% | 42.8 < min 55.0 |
| pullback_too_deep | 119 | 4.2% | 0.184 > max 0.12 |
| too_extended_from_ema50 | 119 | 4.2% | 0.175 > max 0.15 |
| target_room_insufficient | 53 | 1.9% | DAL, CRI, CPRX |
| price_below_base_low | 50 | 1.8% | 168.59 < min 174.29 |
| pullback_no_rebound_confirmation | 21 | 0.7% | BRX, CDNS, COLM |
| stop_distance_exceeds_max | 6 | 0.2% | PHAT, CALM, CNMD |
| rr_below_min | 3 | 0.1% | DEI, CINF, EMR |
| earnings_too_close | 3 | 0.1% | actual 4bd < min 5bd |
| atr_too_high | 1 | 0.0% | actual 0.1024 > max 0.05 |
| support_broken | 1 | 0.0% | actual 44.49 < min 45.17 |

---

## 6. Evidence distribution — validated (step4-passed) rows

`setup_score` and `rvol` percentiles. Breakout/consolidation RVOL samples are tiny (n=14, n=13) — the gate leaves almost nothing behind.

| Setup · metric | n | min | p25 | p50 | p75 | max | mean |
|---|--:|--:|--:|--:|--:|--:|--:|
| breakout · setup_score | 713 | 33.99 | 49.89 | 54.84 | 60.07 | 83.34 | 55.22 |
| breakout · rvol | 14 | 1.50 | 1.63 | 2.07 | 3.45 | 8.32 | 2.98 |
| breakout · atr_pct | 14 | 0.0190 | 0.0395 | 0.0516 | 0.0635 | 0.1724 | 0.0598 |
| pullback · setup_score | 749 | 37.12 | 63.34 | 74.98 | 83.69 | 93.27 | 72.75 |
| pullback · rvol | 464 | 0.02 | 0.42 | 0.54 | 0.67 | 2.42 | 0.57 |
| trend_continuation · setup_score | 1075 | 37.25 | 60.63 | 66.13 | 71.57 | 87.01 | 66.07 |
| trend_continuation · rvol | 662 | 0.17 | 0.44 | 0.55 | 0.70 | 2.42 | 0.59 |
| consolidation_base · setup_score | 595 | 12.71 | 38.81 | 41.97 | 45.67 | 72.49 | 41.92 |
| consolidation_base · rvol | 13 | 0.31 | 0.38 | 0.49 | 0.57 | 1.13 | 0.52 |
| consolidation_base · range_width% | 13 | 0.0061 | 0.0229 | 0.0394 | 0.0577 | 0.0770 | 0.0410 |

---

## 7. Borderline — highest-scoring rejects

Strong candidates dropped by a single gate. In pullback & trend these are almost all `stop_below_atr_floor`; in breakout, all RVOL.

| Setup | Ticker | Score | Failed rule | Comparison |
|---|---|--:|---|---|
| breakout | BBW | 73.31 | rvol_below_hard_threshold | 1.46 < 1.5 |
| breakout | CALM | 71.85 | rvol_below_hard_threshold | 1.03 < 1.5 |
| breakout | HTGC | 71.17 | rvol_below_hard_threshold | 0.78 < 1.5 |
| breakout | WRB | 70.02 | rvol_below_hard_threshold | 0.53 < 1.5 |
| pullback | IRT | 92.91 | stop_below_atr_floor | 0.41 < 0.5 |
| pullback | DRH | 92.73 | stop_below_atr_floor | 0.29 < 0.5 |
| pullback | LNTH | 92.60 | stop_below_atr_floor | 0.04 < 0.5 |
| pullback | SJM | 92.37 | stop_below_atr_floor | 0.23 < 0.5 |
| trend_continuation | TSHA | 83.14 | stop_below_atr_floor | 0.23 < 0.5 |
| trend_continuation | DNA | 82.79 | stop_below_atr_floor | 0.41 < 0.5 |
| trend_continuation | AYI | 81.66 | stop_below_atr_floor | 0.40 < 0.5 |
| trend_continuation | CDNA | 76.25 | too_extended_from_ema50 | 0.167 > 0.15 |
| consolidation_base | TFC | 59.85 | earnings_too_close | 5bd < min 5bd |
| consolidation_base | ADC | 59.63 | price_above_base_high | 77.95 > 77.01 |
| consolidation_base | GBTG | 54.56 | score_below_threshold | 54.6 < 55.0 |
| consolidation_base | GEHC | 54.04 | score_below_threshold | 54.0 < 55.0 |

---

## Architect notes & open questions

- **Breakout RVOL gate (min 1.5)** removes 698/713 routed candidates, including score-70+ names. On a low-RVOL day the whole universe sits near 0.55 — confirm across higher-volume dates before deciding whether the floor or the day is at fault. RVOL is a setup-specific gate (AD-22.23), not universal.
- **consolidation_base** — the service itself raised `consolidation_validator_too_strict_or_misconfigured`. range_tightness (min 60) + price-above-base together account for 71.6% of its fails; review whether the tightness scale or base-boundary logic is calibrated.
- **ATR stop floor (min 0.5)** clips the highest-scoring pullback & trend candidates (IRT 92.9, DRH 92.7). Cross-cutting — a review candidate independent of any single setup.
- **Method:** generated against a pipeline run resumed from `validation` (earnings/fundamentals refresh skipped). Yield metrics only — outcome rates (false-breakout, target-hit, pullback-failure) require the outcome queue to mature.
- **Next:** re-run the same diagnostics across several trading days (mixed volume regimes) to separate day-effects from threshold-effects before any config change. Full machine-readable payload: `funnel_2026-07-10.json`.
