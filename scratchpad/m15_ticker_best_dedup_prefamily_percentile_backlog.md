# Backlog — `ticker_best` dedup should compare on family-normalized score, not raw

**Status: P1** (updated 2026-07-18 — see "2026-07-18 update" section below
for the evidence behind the priority change). Originally split out from the
P1.4 double-credit fix (`m15_double_credit_bug_finding.md`) during its
2026-07-08 go/no-go as "not scheduled" — explicitly out of scope for that PR
(`module15_score_standardization_stable`).

## Problem

`Step5ProposalEngine._build_rows`'s multi-route dedup (`step5_proposal_engine.py`,
the `ticker_best` loop) picks, for a ticker that qualifies for more than one
`setup_type` on the same `signal_date`, whichever route has the higher
**raw** `proposal_score_raw`. This runs *before* the P1.4 Part B percentile
pass, which only computes `proposal_score_ranked` over `ticker_best.values()`
— i.e., the post-dedup survivors. Dedup itself never sees the
family-normalized score; it's fully isolated from the ranking fix.

Multi-route qualification is real and already tested
(`TestMultiRouteDedupe::test_best_setup_type_selected_per_ticker`), not a
hypothetical — M13 routes setup types independently, so a ticker can pass
Step 4 validation for e.g. both `breakout` and `pullback` the same day.

## Concrete failure mode

Ticker XYZ qualifies for both `breakout` (raw `proposal_score_raw=82`) and
`pullback` (raw `78`) the same day. Dedup picks breakout (82 > 78),
permanently discarding the pullback route. But suppose breakout's cohort
that day is tightly clustered high (e.g. 78-85 across candidates), so XYZ's
82 nets only the **40th percentile** within breakout — while pullback's
cohort is lower and more spread that day (e.g. 30-80), so XYZ's 78 would
have landed at the **95th percentile** within pullback. Dedup never
considers this: it already discarded the pullback route based on the raw
comparison, before any percentile computation exists.

## Observability gap (secondary note)

The discarded route isn't merely "not selected" — it's **never written to
`step5_proposals` at all**. `all_items = all_ranked + rejected_items`, and
a dedup-loser (still `rankable=True`, i.e. not `DISPOSITION_REJECTED`, just
outbid by another route for the same ticker) belongs to neither list; it
silently falls out of scope. This predates P1.4 entirely and isn't
introduced by it, but it means there's no direct way to notice this
scenario from `step5_proposals` alone. The only place it's visible: M14's
`step4_analysis` table retains **both** setup-type rows for the ticker (M14
doesn't dedupe), so cross-referencing `step4_analysis` against
`step5_proposals` for the same ticker/date could show a strong pullback
analysis with no corresponding pullback proposal, without it being obvious
that dedup — not a disposition/gate failure — is why. Worth keeping in mind
if this ever gets debugged from the dashboard/ticker_report side without
this context.

## Proposed fix direction (not designed in detail, not committed to)

Move the percentile-rank computation earlier: compute
`proposal_score_ranked` per `setup_type` across **all rankable `enriched`
items** (before dedup), not just `ticker_best` survivors. Then change the
dedup comparison itself (`item["proposal_score_raw"] > ticker_best[t][...]`)
to compare `proposal_score_ranked` instead. This is a real restructuring —
the pre-pass currently sits after dedup specifically because it only needs
to iterate the (smaller) post-dedup set — and would need its own dedicated
test (e.g. a `TestMultiRouteDedupe` case where the raw-best route and the
ranked-best route disagree, asserting the ranked-best route wins).

## Why deferred rather than folded into `module15_score_standardization_stable`
### (original 2026-07-08 reasoning — see 2026-07-18 update below for how this held up)

- Narrower blast radius than Cause 1: only affects tickers with genuine
  same-day dual-qualification, not the entire breakout population.
- No data corruption or visible contradiction today — just a silent,
  potentially suboptimal selection.
- Restructuring the computation order deserves review and its own test,
  not a bolt-on to a PR whose approved scope was Part A (source fix) + Part
  B (global merge/sort), neither of which named dedup explicitly.

## 2026-07-18 update — real-data findings, status → P1

The P2-H investigation and its impact-measurement follow-up (full detail:
`reports/P2_H_rvol_double_crediting_recheck_2026-07-18.md` and
`reports/P2_H_dedup_impact_measurement_2026-07-18.md`) checked this item
against two real high-RVOL campaign dates (2026-06-18, 2026-06-26), now
that this platform has real data suited to the question. Findings:

- **The "narrower blast radius" premise above was too optimistic on
  scale.** Multi-routed, dual-qualifying tickers are not rare: 407 (06-18)
  and 481 (06-26) tickers independently passed Step 4 for 2+ `setup_type`s
  the same `signal_date`.
- Of those, **142 total (81/407 = 19.9% on 06-18, 61/481 = 12.7% on
  06-26) get a different dedup winner** under a family-normalized
  comparison than today's raw-score comparison picks — with median
  percentile-point gaps of 5.5–9.4, not marginal ties.
- **But the real-world consequence rate is small: only 2 of the 142
  (1.4%) actually change the final top-20 outcome**, verified by
  extending the recomputation through the full downstream pipeline
  (risk_score, disposition, diversity-capping) with a byte-exact fidelity
  check against the real stored `step5_proposals` data (0 disagreements
  across 2,048 checked candidates) before drawing this conclusion.
  - **`UPS` (06-18):** selected either way, but shown to the user as
    `BUY pullback @ rank 3` under the actual (raw) dedup vs.
    `WATCHLIST_ONLY trend_continuation @ rank 12` under the
    family-normalized alternative — a materially different trade thesis
    for the same ticker, not a relabeling.
  - **`UA` (06-26):** an outright selection flip — its `breakout` route
    hits the sector/industry diversity cap within breakout's own
    candidate pool and is silently dropped from the final list entirely
    (`selected_flag=False`); its `trend_continuation` route, landing in a
    less-crowded diversity-cap pool for the same sector/industry (caps
    apply independently per `setup_type` in `_apply_hard_cap`), would
    have cleared and been selected at rank 6. UA's absence from the list
    has nothing to do with UA's quality — purely which route the raw-score
    dedup happened to keep.

**Architect's reasoning for P1 (not P0, not indefinitely deferred):** low
absolute frequency — roughly one consequential event per high-RVOL day
observed so far, across the only two real high-RVOL dates checked — but a
real, reproducible, non-hypothetical mechanism, confirmed against
production data rather than remaining a theoretical concern, with
consequences that are directly user-visible: a ticker silently missing
from the final list, or shown with the wrong setup type/disposition, for a
reason unrelated to its actual quality. Not urgent by volume, but not
something to leave unscheduled either — P1 reflects "real and worth
fixing soon," short of "drop everything."

The "Proposed fix direction" section above is unchanged by this update —
this is a priority/status change backed by new evidence, not a redesign.
