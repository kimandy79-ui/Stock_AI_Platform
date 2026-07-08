# Backlog — `ticker_best` dedup should compare on family-normalized score, not raw

**Status:** backlog item, not scheduled. Split out from the P1.4 double-credit
fix (`m15_double_credit_bug_finding.md`) during its 2026-07-08 go/no-go —
explicitly out of scope for that PR (`module15_score_standardization_stable`).

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

- Narrower blast radius than Cause 1: only affects tickers with genuine
  same-day dual-qualification, not the entire breakout population.
- No data corruption or visible contradiction today — just a silent,
  potentially suboptimal selection.
- Restructuring the computation order deserves review and its own test,
  not a bolt-on to a PR whose approved scope was Part A (source fix) + Part
  B (global merge/sort), neither of which named dedup explicitly.
