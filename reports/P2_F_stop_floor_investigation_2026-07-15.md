# P2 Item F — ATR Stop-Floor Policy Investigation

**Date:** 2026-07-15
**Scope:** Read-only investigation, plus read-only structural computation.
**No writes to `prod`. No changes to `m14_setup_validators.py` or any other
live validator/proposal logic.** No M17 simulation write ended up being
necessary — see §3 for why a better, code-change-free method answered the
same question.

**Headline finding, ahead of the four requested items:** the "reject vs.
widen" framing undersells what's actually going on. For **80.6%** of the
cohort that fails Step 4 solely on `stop_below_atr_floor` (458 of 568
candidates), Step 5's *own, already-existing, unmodified* stop formula —
which no candidate in this cohort ever reaches, because Step 4 rejects them
first — would already place the stop **at or beyond the floor**, with zero
widening. The Step 4 gate compares against a different, narrower proxy than
the stop Step 5 would actually compute. This looks like a **gate/formula
mismatch bug**, not primarily a reject-vs-widen policy tradeoff, for most of
the cohort. A genuine structural tradeoff (where even Step 5's real formula
stays under the floor) exists for the remaining 110 candidates — concentrated
in `trend_continuation` (71 of 110).

---

## 1. Affected-cohort enumeration (all 5 campaign dates + 07-10 caveat)

**Method:** `step4_analysis` rows with `setup_passed = FALSE`, `setup_type IN
('pullback','trend_continuation')`, and `setup_reasons` (JSON) containing
**exactly one** element that starts with `stop_below_atr_floor` — i.e., every
other hard check passed. (`setup_reasons` = `list(hard_fails)` when any hard
check fails — `m14_setup_validators.py:598-599,909-910,1203-1204` — so a
length-1 list means this was the *sole* blocker; `hard_fails` is never
short-circuited, all checks run every time.)

Live query against `data/duckdb/prod.duckdb` (read-only), 2026-06-11,
06-18, 06-26, 07-02, 07-08:

| Date | Pullback n / score range (min–max, mean) | Trend_continuation n / score range |
|---|---|---|
| 2026-06-11 | 58 / 70.7–91.3, mean 83.8 | 14 / 66.7–83.0, mean 73.5 |
| 2026-06-18 | 71 / 69.8–94.6, mean 84.6 | 31 / 62.1–91.9, mean 76.5 |
| 2026-06-26 | 50 / 64.6–95.2, mean 85.2 | 17 / 70.2–84.3, mean 78.3 |
| 2026-07-02 | 68 / 67.1–92.5, mean 84.6 | 37 / 62.4–84.6, mean 75.6 |
| 2026-07-08 | 145 / 67.8–93.3, mean 83.8 | 77 / 58.5–90.0, mean 72.8 |
| **Total** | **392** | **176** |

**Cohort total: 568** (392 pullback + 176 trend_continuation). For
reference, 495 additional rows fail on `stop_below_atr_floor` *plus* other
gates (not solely) — excluded from the cohort as specified.

**Pattern confirmed and quantified:** cohort mean scores (83–85 pullback,
73–78 trend_continuation) sit well above the overall passed-candidate mean
for these setup types across the same 5 dates (pullback passed mean 73.6,
trend_continuation passed mean 73.0) and above the overall failed-candidate
mean (pullback 68.6, trend_continuation 66.3). The floor disproportionately
removes high scorers, consistently across all 5 dates — not a one-day
artifact.

**07-10 caveat:** `data/duckdb/prod.duckdb` currently spans only
2026-06-11 → 2026-07-08 (`SELECT MIN/MAX(signal_date) FROM step4_analysis`
confirms this; `pipeline_runs` has no row for 07-10). 2026-07-10 was not one
of the 5 campaign dates and its step3/4/5 data no longer exists — it was
dropped in the 2026-07-14 prod rebuild (see [[prod_rebuild_2026_07_14]]).
The referenced `funnel_2026-07-10.json` machine-readable payload is not in
the repo either. I could not re-enumerate a fresh "solely failed" cohort for
07-10 the same way as the 5 campaign dates; the only surviving record is
`reports/funnel_diagnostics_2026-07-10.md`, whose named examples (IRT 92.9,
DRH 92.7, LNTH 92.6, SJM 92.4 for pullback; TSHA 83.1, DNA 82.8, AYI 81.7 for
trend_continuation) and aggregate counts (pullback: 120/285 fails =42.1% on
this gate; trend_continuation: 45/413=10.9%) are cited as-is from that
report, not recomputed. Note that report's methodology groups by
`setup_fail_reason` (the *first* hard-fail label), not the stricter
"exactly one reason" cohort definition used above for the 5 campaign dates —
the two are not perfectly apples-to-apples, though both qualitatively show
the same high-scorer skew.

## 2. M17 replay policy-variant feasibility

**A config-level threshold override exists and works with zero code
changes.** `min_atr_stop_floor_multiple` is read from
`setup_config["validation"]` in all four validator functions
(`m14_setup_validators.py:413,702,1025,1310`), and `SimulationEngine._replay_date`
already calls `m14.validate_setup(setup_type, feat_for_validator, config)`
with an **in-memory** `config` dict per `(sim_date, config_id)` variant
(`simulation_engine.py:1106`), writing only to `sim_step4_analysis`/
`sim_step5_proposals` (`_INSERT_SIM_STEP4`/`_INSERT_SIM_STEP5`,
`simulation_engine.py:1140-1160`) — fully isolated from `prod` per the M17
design. A sim variant with a cloned `setup_config` carrying a different
`min_atr_stop_floor_multiple` is genuinely runnable with no code edits.

**But that only tests a different threshold *value*, not literal "Policy
B."** The note's Policy B is `stop = min(structural_stop, entry −
floor·ATR)` — a deliberate widen when structural is tighter than the floor.
Checked where the real stop gets computed: `step5_proposal_engine.py`'s
`_compute_stop_target` (`:409-566`). For pullback:
`stop = min(support_raw, swing_low_raw, ema_area_raw) − buffer_atr`
(`:521-531`); for trend_continuation: `stop = swing_low_raw − buffer_atr`
(`:549-551`). **Neither formula references `min_atr_stop_floor_multiple` at
all.** The floor concept exists only as a Step 4 pass/fail gate; nothing in
Step 5's stop computation implements floor-based widening. Implementing
literal Policy B would require coordinated changes to *both* Step 4 (let the
candidate through) *and* Step 5's `_compute_stop_target` (add the floor-widen
branch) — real logic changes, not a config knob, and squarely inside "no
changes to live validator logic" for this investigation. **NO-GO on running
literal Policy B via config-only M17 replay.**

A tempting shortcut — pass a sim `setup_configs` variant with
`min_atr_stop_floor_multiple: 0.0` to just disable the gate and let Step 5's
*existing* (unmodified) formula run — was considered and rejected as
misleading: it doesn't widen anything, it lets the candidate through with
whatever stop Step 5's structural formula produces (which could still be
tight), and interpreting its resulting RR distribution as evidence "for" or
"against" Policy B would conflate three different things (the gate's
proxy, Step 5's real formula, and Policy B's hypothetical widen). See §3 for
what was done instead.

## 3. Structural comparison (read-only, no simulation write needed)

Rather than run a partial/misleading proxy simulation, the actual question
— "if Step 4 didn't block these candidates, what stop would they end up
with, and does it clear the floor?" — is answerable directly: **Step 5's
`_compute_stop_target` is already deterministic given the same
`daily_features`/`daily_prices` data these candidates were evaluated
against**, and that data is still in `prod` (only `step3/4/5` were touched
by the AD-22.26 work, not `daily_features`). So for all 568 cohort rows, I
pulled `support_level`, `swing_low`, `ema20`, `ema50`, `atr14`/`atr_pct` from
`daily_features` (joined to `daily_prices` for the raw-conversion ratio) and
computed Step 5's actual formula in Python — same `buffer_atr_multiple` /
`k_atr_stop` read from the live active `pullback`/`trend_continuation`
`setup_configs` rows (`buffer_atr_multiple=0.25` both; `k_atr_stop=1.2`
pullback, `1.5` trend_continuation).

**Result:**

| Setup type | n | Clears floor under Step5's real (unmodified) formula | Still fails |
|---|--:|--:|--:|
| pullback | 392 | 353 (90.1%) | 39 |
| trend_continuation | 176 | 105 (59.7%) | 71 |
| **Total** | **568** | **458 (80.6%)** | **110** |

**Why the split is so lopsided between setup types:** pullback's gate
(`m14_setup_validators.py:511-521`) checks distance to `support_raw` only.
Step 5's real pullback stop (`step5_proposal_engine.py:521-531`) takes the
**minimum of three** candidates — `support_raw`, `swing_low_raw`,
`ema_area_raw` (min of ema20/ema50) — minus a buffer. Spot-checked examples
(KN, CPAY, CMI, SHOO, all 2026-06-18/06-26 pullback) show `support_raw` and
`swing_low_raw` coinciding, but `ema_area_raw` sitting well below both —
e.g. KN: entry=40.05, support=39.96 (gate basis, 0.05 ATR), but
`ema_area`=36.19 → Step 5's real stop lands at 2.37 ATR, comfortably past
the 0.5 floor. The gate is checking the wrong (narrowest) candidate almost
every time an EMA sits meaningfully below support — which is common.

trend_continuation's gate (`m14_setup_validators.py:1113-1125`) and Step 5's
real formula use the **same single candidate**, `swing_low_raw` — the only
difference is Step 5's `buffer_atr` subtraction. So
`real_stop_atr = gate_stop_atr + buffer_atr_multiple` (0.25) essentially
exactly: ERAS (06-18) gate=0.305 → real=0.555 (clears); DXPE (06-26)
gate=0.092 → real=0.342 (still fails), matching the formula precisely. This
is why trend_continuation's flip rate (59.7%) is lower than pullback's
(90.1%) but still substantial — any trend_continuation candidate with
`gate_stop_atr ≥ 0.25` already clears 0.5 once the real buffer is applied.

**The genuine reject-vs-widen tradeoff cohort is much smaller than 568: 110
candidates**, where even Step 5's real (unmodified, non-widened) formula
stays under the floor:

| Setup type | n | Score mean / median / max |
|---|--:|---|
| pullback | 39 | 82.9 / 83.7 / 92.2 |
| trend_continuation | 71 | 74.7 / 74.8 / 90.0 |

Top of that list, for spot-check: AMGN (06-18 pullback, 92.2, real
stop=0.26 ATR), TKO (06-18 pullback, 91.5, 0.48 ATR — barely misses), SMG
(07-08 trend_continuation, 90.0, 0.31 ATR), CLF (06-18 trend_continuation,
89.8, 0.45 ATR — barely misses). Several of these (TKO, EOG, ANDE, CLF) sit
within 0.02–0.05 ATR of the floor even under the real formula — genuinely
borderline, not obviously safe to widen past.

**Implication for the architect's reject-vs-widen decision:** this splits
into two separate, sequential questions rather than one. (1) Should the
Step 4 gate be fixed to check against the *same* stop basis Step 5 actually
uses (support/swing_low/ema_area-min for pullback, buffered swing_low for
trend_continuation) instead of its current narrower proxy? That alone would
recover 458 of the 568 candidates with **no policy change and no widening**
— the risk being taken was never as tight as the gate claimed. (2) For the
remaining, smaller 110-candidate cohort where the real stop is genuinely
under the floor, reject-vs-widen (Policy A vs B) remains a live, ADR-worthy
question — this note doesn't answer it, and per its own instructions,
shouldn't.

## 4. Outcome-data maturity check

```
signal_outcomes total (platform-wide): 117
  by setup_type: breakout=33, consolidation_base=4, pullback=22, trend_continuation=58
  by outcome_status: complete=116, partial=1
outcome_tracking_queue: done=117, pending=347 (total 464)
```

The project memory's "~25" estimate is stale — actual count is **117**, not
25 (worth correcting that memory). Of those, pullback+trend_continuation
account for 80 rows, all `outcome_status='complete'` with
`realized_r_multiple` populated for 116/117 overall.

**Is this enough to compare Policy A vs B by actual trade results?** No, on
two independent grounds:

1. **Zero outcome rows exist for the actual cohort in question.** A
   `REJECTED` disposition never gets a `stop_price_raw`/`target_price_raw`
   (confirmed directly: `step4_analysis`/`step5_proposals` rows for this
   cohort all have `stop_price_raw IS NULL`, `target_price_raw IS NULL`,
   `estimated_rr IS NULL` — e.g. KN's step5_proposals row:
   `disposition='REJECTED', stop_price_raw=None, ...`). No trade plan means
   no `outcome_tracking_queue` enqueue, since M16 only enqueues proposals
   that are `in_raw_top_n OR in_diversified_top_n`
   (`outcome_queue.py:113-120`) — REJECTED candidates were never a
   real trade to begin with, so there is nothing to compare outcomes
   against, for either policy, today.
2. **117 total (80 for these two setup types) is thin for any platform-wide
   A/B statistic even where outcomes do exist** — not enough to draw a
   reliable win-rate/realized-RR comparison for a policy that hasn't even
   run yet.

**This investigation is structural, not outcome-based, by necessity — not
by choice.** An outcomes-based Policy A vs. B comparison is a genuine future
step, but it requires *some* candidates to actually run under Policy B (or
the gate fix from §3) and mature through the 40bd outcome horizon first;
there is no shortcut around that with today's data.

## 5. Anomalies (verbatim)

- **07-10 diagnostics data has been dropped from prod** and its
  machine-readable JSON companion isn't in the repo — this coder note's
  premise ("across all 5 campaign dates plus 07-10") assumed that data was
  still queryable; it isn't, and I couldn't reconstruct it beyond what's in
  the archived markdown report.
- **Gate/formula mismatch is a more consequential finding than the
  reject-vs-widen question this note was scoped to investigate.** Flagging
  explicitly since fixing it is a smaller, more mechanical change than an
  ADR-gated reject-vs-widen policy decision, and would resolve the large
  majority of the disputed cohort on its own — worth the architect's
  attention as a candidate P2-item independent of the ADR.
- `step4_analysis.stop_price_raw`/`target_price_raw`/`estimated_rr` are
  `NULL` for **every** row, pass or fail — these fields are computed only in
  Step 5 (`step5_proposal_engine.py`), never populated in Step 4 despite the
  columns existing on `step4_analysis`. Not a bug (Step 5 owns the trade
  plan by design), but worth knowing before assuming Step 4 data alone can
  answer stop/RR questions in future investigations.

## No writes, no code changes

Read-only investigation and read-only structural computation only
(`m14_setup_validators.py`, `step5_proposal_engine.py`,
`simulation_engine.py`, plus `duckdb` queries against
`data/duckdb/prod.duckdb` opened `read_only=True`, plus a standalone Python
script replicating Step 5's published formula against `daily_features`/
`daily_prices`/`setup_configs` data). No files were modified, no DB writes
were made, `m14_setup_validators.py` was not touched, no M17 simulation run
was executed (§3 explains why a cleaner read-only method answered the
question instead).
