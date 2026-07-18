# P2 Item H Follow-up — Impact of the `ticker_best` Dedup Residual on Final Selection

**Date:** 2026-07-18
**Scope:** Read-only investigation only. No code changes. No fix proposed.
**No writes to `prod`. No pipeline rerun.** `step5_proposal_engine.py` not
modified — every scoring/risk/disposition/diversity step reuses the
**actual imported production functions** (`_compute_stop_target`,
`_compute_estimated_rr`, `_confirmation_score_raw`, `_percentile_rank`,
`_proposal_score_raw`, `_compute_risk_score`, `_assign_disposition`,
`Step5ProposalEngine._apply_hard_cap`/`_apply_soft_penalty`). Only the
`ticker_best` dedup comparison itself is varied (raw vs. family-ranked)
since that specific block is inline in `_build_rows` and not separately
callable — everything else is real code, not a reimplementation.

---

## 0. Question being answered

The P2-H investigation found 81 (06-18) and 61 (06-26) — 142 total —
multi-routed tickers where the `ticker_best` dedup step's raw-score
comparison picks a different winning `setup_type` than a family-normalized
comparison would. Unknown until now: does that flip actually change what
the user sees in the final top-20 selected list, or is it invisible
mid-table churn that never reaches the selection line?

---

## 1. Fidelity check (required before trusting any conclusion below)

Extended the prior recomputation through the full downstream pipeline —
`_compute_risk_score` → `_assign_disposition` (+ the resistance-blocks
WATCHLIST override) → `ticker_best` dedup → Part B percentile ranking →
`Step5ProposalEngine._apply_hard_cap` (the active mode:
`hard_cap_enabled=True`, `max_sector_count=4`, `max_industry_count=2`) →
`selected_flag`. Ran the **ACTUAL** scenario (raw-score dedup, matching
today's real code exactly) and compared every candidate's recomputed
`(winning setup_type, selected_flag)` against the real stored
`step5_proposals` rows:

```
2026-06-18: 0/917 disagreements
2026-06-26: 0/1131 disagreements
```

Byte-exact match across every rankable candidate on both dates (917 +
1131 = 2048 checked). The recomputation — including the newly-added
risk_score/disposition/diversity-capping steps — is a faithful stand-in
for the real pipeline; the sample size here is far larger than the 142
flipped tickers themselves, so this validates the general machinery, not
just the cases that happen to matter. The flip count itself also
reproduced exactly: 81 (06-18) and 61 (06-26), matching the prior
investigation.

---

## 2. Per-flip categorization — all 142 tickers

Two scenarios computed per flipped ticker:
- **Actual:** today's real raw-score dedup winner, run through the full
  pipeline — is it selected?
- **Counterfactual:** the family-normalized-percentile dedup winner
  (the alternative route), run through the identical pipeline — would
  *it* be selected?

| Category | Count | % of 142 |
|---|--:|--:|
| No impact (neither route selected either way) | 140 | 98.6% |
| Same ticker selected either way, different `setup_type` | 1 | 0.7% |
| Selection changes (selected under one scenario, not the other) | 1 | 0.7% |
| **Consequential (either of the last two rows)** | **2** | **1.4%** |

### The two consequential cases, in full

**`UPS`, 2026-06-18 — same ticker, different setup_type (selected either way):**
```
pullback:            proposal_score_raw=75.86  disposition=BUY
trend_continuation:  proposal_score_raw=73.21  disposition=WATCHLIST_ONLY
ACTUAL:         pullback wins dedup  → selected=True, diversified_rank=3   (as BUY pullback)
COUNTERFACTUAL: trend_continuation wins dedup → selected=True, diversified_rank=12  (as WATCHLIST_ONLY trend_continuation)
```
UPS makes the final list regardless, but the *trade thesis shown to the
user* differs meaningfully — `BUY pullback @ rank 3` vs.
`WATCHLIST_ONLY trend_continuation @ rank 12` are different
recommendations for the same ticker, not a cosmetic relabeling.

**`UA`, 2026-06-26 — selection changes:**
```
breakout:            proposal_score_raw=75.15  disposition=BUY
trend_continuation:  proposal_score_raw=71.53  disposition=BUY
ACTUAL:         breakout wins dedup → selected=False, diversified_rank=None  (capped: sector/industry cap hit within the breakout group)
COUNTERFACTUAL: trend_continuation wins dedup → selected=True, diversified_rank=6
```
`_apply_hard_cap` applies sector/industry caps **independently within each
`setup_type`'s own candidate pool** before merging
(`step5_proposal_engine.py:1650-1682`). UA's `breakout` route lost to the
sector/industry cap inside the crowded breakout pool that day; its
`trend_continuation` route, in a less-crowded pool for the same
sector/industry, would have cleared. Today, UA is silently absent from the
final list — not because it was a worse candidate, but because dedup
picked the specific route that happened to land in the more-capped group.

### The other 140

All 81/61 flips not listed above land in "no impact": neither the raw
winner's route nor the ranked winner's route was ever close enough to the
`diversified_rank <= top_n=20` cutoff for the choice to matter — pure
mid-table churn.

---

## 3. Clear summary

**2 of 142 flipped tickers (1.4%) actually change something in the final
top-20 outcome — 1 changes the selected trade thesis/setup_type for an
otherwise-selected ticker, 1 flips a ticker between selected and not
selected entirely.** The other 140 (98.6%) are inconsequential — the raw
vs. family-normalized disagreement is real and measurable (per the prior
report: median 5.5–9.4 percentile-point gaps), but for the overwhelming
majority of flips, neither candidate route was ever a plausible top-20
member, so the dedup choice doesn't reach anything the user sees.

**Framing for priority:** across two real high-RVOL campaign dates, this
gap produced exactly one outright selection flip and one thesis-label
flip — small in absolute count, but non-zero and directly visible to a
user (a ticker silently missing from the list, or shown with the wrong
setup label/disposition, for a reason that has nothing to do with its
actual quality). Two consequential events in two dates is not urgent by
volume, but it is a real, reproducible, non-hypothetical defect class —
distinct from "140/142 flips are harmless noise," which on its own might
read as low priority. Both readings are true simultaneously; this report
provides the numbers for whoever makes the prioritization call, per
instructions, without recommending one.

---

## 4. Anomalies (verbatim)

- None beyond what the prior P2-H report already flagged (recomputation
  fidelity bugs found and fixed *before* drawing conclusions, not
  encountered fresh here — this investigation's fidelity check passed on
  the first fully-assembled run, 0/917 and 0/1131, because it reused the
  already-corrected `proposal_score_raw`/resistance-cap logic from the
  prior investigation rather than re-deriving it).
- `UA`'s `sector` and `industry` fields are both `"Consumer Discretionary"`
  in `ticker_master` (no finer industry granularity stored for that
  ticker) — real stored data, not a computation artifact; noted since it's
  visible in the detail above and might otherwise look like a bug.

## No writes, no code changes

Read-only recomputation only, against `data/duckdb/prod.duckdb`
(`read_only=True`). No DB writes, no pipeline rerun, no changes to
`step5_proposal_engine.py` or any other file, no commit.
