# Backlog Status Update — `ticker_best` Dedup Residual → P1

**Date:** 2026-07-18
**Scope:** Text-only edit to
`scratchpad/m15_ticker_best_dedup_prefamily_percentile_backlog.md`. No code
changes. No implementation — follows as a separate, later coder note.
**No commit — diff delivered, stop, per current policy.**

---

## What changed

`scratchpad/m15_ticker_best_dedup_prefamily_percentile_backlog.md`:

1. **Status line** (top of file): `backlog item, not scheduled` →
   `P1`, with a pointer to the new update section for the evidence, and the
   original 2026-07-08 "not scheduled" framing preserved as historical
   context rather than overwritten.
2. **`## Why deferred rather than folded into
   module15_score_standardization_stable`** — left fully intact (original
   2026-07-08 reasoning), retitled with a subheading noting it's the
   original reasoning and pointing forward to how it held up.
3. **New section added, `## 2026-07-18 update — real-data findings,
   status → P1`** (appended after the original "Why deferred" section, so
   the file reads chronologically): a condensed summary of the P2-H
   investigation and its impact-measurement follow-up —
   - Corrects the original "narrower blast radius" premise: multi-routed
     dual-qualifying tickers are common (407/481 on the two dates
     checked), not rare.
   - States the flip rate (142 total, 19.9%/12.7% of multi-routed
     tickers, median 5.5–9.4 percentile-point gaps).
   - States the actual consequence rate (2/142 = 1.4% change the final
     top-20 outcome), noting this was verified via a byte-exact fidelity
     check (0/2,048 disagreements) before being trusted.
   - Both concrete examples (`UPS` — thesis/setup_type changes for an
     already-selected ticker; `UA` — silent drop from the list via
     diversity-cap group placement, unrelated to quality).
   - The architect's P1 reasoning (low absolute frequency but real,
     reproducible, user-visible consequences — not P0, not indefinitely
     deferred).
   - References to both source reports:
     `reports/P2_H_rvol_double_crediting_recheck_2026-07-18.md` and
     `reports/P2_H_dedup_impact_measurement_2026-07-18.md`.
4. **`## Proposed fix direction (not designed in detail, not committed
   to)`** — left completely unchanged, per instructions (status/priority
   update, not a redesign).

## What did NOT change

- `app/services/proposal/step5_proposal_engine.py` — not touched.
- The `## Problem`, `## Concrete failure mode`, and
  `## Observability gap (secondary note)` sections — left as originally
  written; still accurate, no new information changes their content.
- The proposed fix direction itself (move percentile-rank computation
  before dedup, compare `proposal_score_ranked` instead of raw) — carried
  forward unchanged, exactly as instructed.

## Before/after — status line

```diff
 # Backlog — `ticker_best` dedup should compare on family-normalized score, not raw

-**Status:** backlog item, not scheduled. Split out from the P1.4 double-credit
-fix (`m15_double_credit_bug_finding.md`) during its 2026-07-08 go/no-go —
-explicitly out of scope for that PR (`module15_score_standardization_stable`).
+**Status: P1** (updated 2026-07-18 — see "2026-07-18 update" section below
+for the evidence behind the priority change). Originally split out from the
+P1.4 double-credit fix (`m15_double_credit_bug_finding.md`) during its
+2026-07-08 go/no-go as "not scheduled" — explicitly out of scope for that PR
+(`module15_score_standardization_stable`).
```

Full new section content is in the file itself (33 added lines,
`## 2026-07-18 update — real-data findings, status → P1`); not duplicated
verbatim here per the coder note's "condensed... not a verbatim copy"
instruction for that section — see the file directly for the exact text.

## No code changes, no commit

Only `scratchpad/m15_ticker_best_dedup_prefamily_percentile_backlog.md`
was edited. No implementation of the proposed fix. Left uncommitted in
the working tree, per current policy.
