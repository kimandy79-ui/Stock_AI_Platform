# M15 Breakout Double-Credit Bug — Finding (recorded 2026-07-08)

## Provenance

This finding **originated from the architect's memory-recall of a prior
diagnostic session (2026-06-29/30)** — it was not present anywhere in this
repo's tracked history, memory files, or scratchpad notes before this write-up.
It was **not verified against the repo until this pass** (2026-07-08, during
P1.4 scoping work — see `p1_batch_rs_earnings_breakout_scoring_design_note.md`).

Verification against `diagnostics_2026-06-26.json` and the current codebase
**confirmed the core diagnosis and the headline evidence numbers byte-exact**,
but **corrected two details** relative to the architect's original recall:

1. **Cause 2's current status** — the "diversity caps are applied globally"
   half of the original finding was already fixed in commit `fb04448`
   ("26.07.01", 2026-07-01), one day after the original diagnostic session.
   A residual, narrower problem remains (see below).
2. **`sector_max_positions` default** — the active config default is **4**,
   not 3 as originally recalled. `industry_max_positions=2` was correct.

This note is therefore a **corrected/merged finding**, not a straight
transcript of the original 2026-06-29/30 diagnostic session. Anyone citing
it later should cite this version, not the original recall.

**Status update (2026-07-08, later same day):** architect approved a joint
fix (Cause 1 + P1.4 standardization, one combined change) — see
"Implementation" section appended at the end of this note. This finding
itself remains the settled record of the mechanism/evidence; the fix
implementation lives in `step5_proposal_engine.py` and
`tests/test_step5_proposal_engine.py`, held uncommitted pending review
(same pattern as P1.2/P1.3).

---

## Cause 1 — `confirmation_score` double-credits breakout only

**Mechanism.** `step5_proposal_engine.py`'s `_proposal_score_raw` (current
code, verified at `step5_proposal_engine.py:866-872`):

```
proposal_score_raw = 0.40*setup_score + 0.25*rr_score + 0.15*confirmation_score
                    + 0.10*market_score + 0.10*stop_quality
```

(5-term, weights `_W_SETUP=0.40, _W_RR=0.25, _W_CONFIRMATION=0.15,
_W_MARKET=0.10, _W_STOP_DIST=0.10` — corrected from the originally recalled
4-term `0.45/0.25/0.20/0.10` formula, which does not match current code.
Diagnosis holds regardless of the exact weights.)

`confirmation_score` is RVOL-derived: `confirmation_score = clamp((rvol_val
or 0.0) * 50.0)` where `rvol_val` comes from the candidate's own `rvol`/`rvol20`
feature (`step5_proposal_engine.py:1249-1250`). This is computed and applied
**identically for every setup type** — but only **breakout** has RVOL as a
Step 4 **hard gate** (`rvol20 >= min_rvol_breakout`, default 1.5,
`rvol_is_hard=True`). Pullback and trend_continuation both set
`rvol_is_hard=False` — RVOL is soft-only for them (AD-22.23).

The result: every breakout survivor is, by construction, already selected
for having high RVOL (≥1.5) — then gets credited *again* for that same high
RVOL via `confirmation_score` in the M15 ranking formula. Pullback/
trend_continuation candidates carry no such pre-filter, so their
`confirmation_score` reflects the natural spread of RVOL in their surviving
population, not a floor-truncated, uniformly-high band. This inflates
breakout's `proposal_score_raw` relative to the other three setup types in a
way that has nothing to do with trade quality — it's the same signal
counted once as a pass/fail gate and once as a score input.

**Fix direction (unchanged from original finding):** normalize
`confirmation_score` per `setup_type` (percentile-rank or z-score within
that day's same-family distribution) before applying the shared formula —
the same normalization idea as [[p1_batch_rs_earnings_breakout_design_note]]'s
P1.4 proposal for `setup_score` itself. These are two instances of the same
underlying gap (M15 has no per-family normalization anywhere before merging
into the shared ranked list), not two unrelated fixes.

---

## Cause 2 — diversity caps and the shared ranked list (split into two states)

**Original problem, as recalled: fixed.** The architect's original finding
was that sector/industry diversification caps (`sector_max_positions`,
`industry_max_positions`) were applied against the single global ranked
list, so breakout — already dominating on raw score via Cause 1 — could
consume the entire cap budget before any pullback/trend_continuation/
consolidation_base candidate was even evaluated against it.

Verified via `git log -p -S"Apply caps independently within each setup_type"
-- app/services/proposal/step5_proposal_engine.py`: commit `fb04448`
("26.07.01", authored 2026-07-01 00:20:46 +0900) — **one day after** the
2026-06-29/30 diagnostic session — rewrote `_apply_hard_cap` to group
candidates by `setup_type` first and apply sector/industry caps
**independently within each group**, before merging survivors:

```python
# _apply_hard_cap, current code (step5_proposal_engine.py:1543-1580)
by_setup: dict[str, list[dict]] = {}
for item in ranked:
    by_setup.setdefault(item["setup_type"], []).append(item)

survivors: list[dict] = []
for group in by_setup.values():
    # sector/industry caps applied per-group here — one setup_type can no
    # longer exhaust another's diversity slots
    ...
# "Merge survivors across setup_types; re-rank by score for final top_n."
```

The commit's own comment states the intent explicitly: *"so that one
setup_type cannot exhaust all sector/industry slots before candidates from
other setup_types are evaluated."* This part of Cause 2 is resolved.

**Residual problem, still open.** After per-setup-type diversity capping,
the code's own comment says survivors are then *"merged and re-ranked by
score for the shared top_n cutoff."* That final merge is still a single
global sort by `proposal_score_raw` (`_sort_key`, unaffected by the
`fb04448` fix) with **no per-setup-type quota or floor on the `top_n` slots
themselves**. So even with diversity caps now fairly applied per family,
if breakout's `proposal_score_raw` still runs structurally hot (Cause 1,
unfixed), breakout can still fill every `top_n` slot — diversity caps
constrain *how many same-sector/industry breakout names* can stack up, they
don't guarantee *any* pullback/trend_continuation/consolidation_base name
gets a slot at all.

**Precise framing for future reference:** the residual issue is *"`top_n`
allocation is still winner-take-all by raw score across setup types,"* not
*"diversity caps are global"* — that half is already fixed.

---

## Correction: `sector_max_positions` default

Active config (`app/services/config/default_configs.py:419-426`,
`DEFAULT_RISK_LABEL_CONFIG["diversification"]`):

```python
"diversification": {
    "hard_cap_enabled": True,
    "sector_max_positions": 4,      # originally recalled as 3
    "industry_max_positions": 2,    # matches original recall
    ...
},
```

Doesn't change either cause's mechanism — just the actual cap number in
force today.

---

## Evidence (verified byte-exact against `diagnostics_2026-06-26.json`)

| Setup type | Routed (M13→M14) | M14 pass | M14 pass rate | Selected (final M15 list) |
|---|---|---|---|---|
| Breakout | 1,110 | 762 | **68.65%** | **20** |
| Pullback | 344 | 172 | 50.0% | 0 |
| Trend continuation | 1,117 | 651 | 58.28% | 0 |
| Consolidation base | 586 | 15 | 2.56% | 0 |
| **Total** | | **1,600** | | **20** |

Verified via direct read of `diagnostics_2026-06-26.json`'s `setup_funnel`
array — matches the architect's originally recalled figures exactly. Also
consistent with that same file's recorded `diagnostic_warnings` entry:
`setup_dominance.breakout_selected_share_high` — *"breakout selected 20/20
= 100% of final list."*

**M14 pass rates are healthy and roughly setup-independent** (breakout
68.65%, pullback 50.0%, trend_continuation 58.28% — all in a comparable
range; consolidation_base's 2.56% is a separate, already-flagged issue,
`consolidation_validator_too_strict_or_misconfigured`, not part of this
finding). This rules out M14 as the bottleneck for the 20/20-breakout
outcome — the distortion happens entirely at M15's scoring/ranking stage
(Cause 1) and, until `fb04448`, at the diversity-cap stage (Cause 2,
now partially resolved).

**Conclusion, unchanged from original finding: M14 gates should not be
relaxed.** They are confirmed healthy, not the bottleneck.

---

## Relationship to P1.4

[[p1_batch_rs_earnings_breakout_design_note]]'s P1.4 section proposed
normalizing `setup_score` per family before it enters
`proposal_score_raw`. This note's Cause 1 shows `confirmation_score` needs
the identical treatment, for the identical structural reason (a
setup-type-specific hard gate pre-selecting for a signal that's then
re-scored uniformly). Both should likely be fixed together, in the same
normalization pass, rather than as two separate diffs — this is a call for
the architect when P1.4 gets its go/no-go, not decided here.

---

## Implementation (2026-07-08, joint fix approved same day)

**Decision:** joint fix, one combined change in `step5_proposal_engine.py` —
Cause 1 fix (Part A) and P1.4 standardization (Part B) together, not as
separate PRs. Residual Cause 2 (`top_n` global-merge allocation) explicitly
out of scope, untouched.

**Shared primitive added:**
```python
def _percentile_rank(sorted_values: list[float], value: float) -> float:
    n = len(sorted_values)
    if n <= 1:
        return 100.0
    return 100.0 * bisect.bisect_left(sorted_values, value) / (n - 1)
```
`n<=1` returns `100.0` rather than an undefined/NaN result — deliberate choice
over z-score, since single-candidate setup_types occur in real diagnostic
data (e.g. pullback had 1 selected candidate on 2026-06-29).

**Part A** — new `_confirmation_score_raw(a, feat)` factors out the existing
`clamp((rvol_val or 0.0) * 50.0)` formula. A pre-pass over `analyses`
(`setup_passed=True` rows only) builds `confirmation_by_type: dict[setup_type,
sorted list of raw confirmation scores]`. Inside the main per-candidate loop,
`confirmation_score` (previously the raw RVOL-derived value, fed directly
into `_proposal_score_raw`) is now `_percentile_rank(confirmation_by_type[setup_type],
_confirmation_score_raw(a, feat))`. `rvol_val` itself is unchanged and still
used unnormalized for `_compute_final_trade_decision` (a different, unrelated
consumer).

**Part B** — after the `ticker_best` dedup (dedup itself still compares a
ticker's own multi-route candidates on raw `proposal_score_raw`, unchanged —
see open question below), a second pass groups `ticker_best.values()` by
`setup_type` and computes `item["proposal_score_ranked"] = _percentile_rank(...)`
against that family's `proposal_score_raw` distribution. `_sort_key` (drives
`raw_rank`/`in_raw_top_n`), `_apply_hard_cap`'s final cross-family merge sort,
and `_apply_soft_penalty`'s penalty base and sort all switched from
`proposal_score_raw` to `proposal_score_ranked`. `proposal_score_raw` and
`proposal_score_final` (in hard-cap mode, `hard_cap_enabled=True` default)
keep their exact existing stored meaning/values — `proposal_score_ranked` is
transient, drives ranking/selection only, not persisted to
`step5_proposals`. In soft-penalty mode (not the active default), the
penalty is now applied on top of `proposal_score_ranked` rather than raw, to
keep that function's own sort key and stored `proposal_score_final`
internally consistent — a judgment call since the go/no-go note didn't
specify soft-penalty mode explicitly; flagged for review.

**Verified behavior** (`tests/test_step5_proposal_engine.py::TestDoubleCreditRedistribution`):
5 breakout candidates (RVOL 1.6-2.4, clustered high per their own Step 4
hard gate) vs. 5 pullback and 5 trend_continuation candidates (RVOL 0.4-1.3,
soft-only), identical `setup_score=70` and feature geometry across all 15,
unique sector/industry per ticker (isolates this from the Cause 2 cap
mechanism entirely). `top_n=10`. Actual selected distribution:
**breakout 4/10 (40%), pullback 3/10 (30%), trend_continuation 3/10 (30%)**
— down from the diagnostic baseline's 20/20 (100%) breakout. Well inside
the 50-60% acceptance bar.

**Tests added:** `TestPercentileRank` (5 unit tests), `TestConfirmationScoreRaw`
(5 unit tests), `TestDoubleCreditRedistribution` (1 integration test, above).
Full `test_step5_proposal_engine.py` (145 tests, all passing, no existing
test modified) plus `test_phase6_orchestrator.py`, `test_m14_setup_validators.py`,
`test_simulation_engine.py` all green.

**Open question surfaced, not resolved:** `ticker_best` dedup (a ticker
routed to multiple setup_types) still picks its best route by raw
`proposal_score_raw`, not `proposal_score_ranked` — the same cross-family-unfair
comparison this whole fix targets, just at the per-ticker dedup step instead
of the final list. Left unchanged pending explicit direction (this was
flagged as an open question in the original P1.4 design note too, not
addressed by this go/no-go).

**Status: implemented, not committed.** Held pending review per the
go/no-go note's instruction, same as P1.2/P1.3's pattern.
