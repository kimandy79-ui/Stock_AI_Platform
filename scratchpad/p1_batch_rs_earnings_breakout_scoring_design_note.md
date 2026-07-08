# P1 Batch — RS Percentile, Earnings Gate, Breakout Semantics, Score Standardization: Design Note

**No production code, config, or spec changes in this note.** Everything below
is analysis and proposal, per the CODER_NOTE's header ("No production code
changes without a follow-up architect sign-off note per item").

---

## Sign-off addendum (2026-07-08)

Architect approved, per-item: P1.1 proceed to design note/schema delta only;
P1.2 implement as scoped, hard-reject decided; P1.3 diagnostics
instrumentation only, config untouched; P1.4 needs a more concrete follow-up
design note before any go/no-go. See per-section updates below and
`## P1.4 — Follow-up` at the end for the requested formula/integration-point
detail.

**Universe size clarified (2026-07-08, round 3):** the current 50-ticker
`ticker_universe_snapshot` is a **deliberate test-speed choice**, not the
production scale — full-size universe (hundreds to low-thousands of
tickers) is what this design must actually serve once validated. The
in-progress `top_50/100/200/sp500/all.csv` work is separate infrastructure,
unrelated to this decision — not evidence of imminent universe-size drift,
and not something P1.1 should sequence against. The design below targets
full-universe scale directly; anywhere the design behaves differently at
50 tickers vs. full scale is called out explicitly as a caveat, not treated
as a reason to wait or as the target size to calibrate against.

---

## P1.1 — Cross-sectional RS percentile

**Two gaps, not one.** The coder note frames this as time-series-vs-SPY vs.
cross-sectional-vs-universe. Reading `feature_engine.py:976-1000` surfaces a
second, independent gap:

```python
ticker_roc = _sanitize(rec["roc20"])          # <- 20-trading-day ROC
...
rs_vs_spy = ticker_roc - spy_roc              # feature_engine.py:1000
sector_rs = ticker_roc - etf_roc              # feature_engine.py:993
```

Both `relative_strength_vs_spy` and `sector_relative_strength` are built on
**`roc20`** — a ~1-month lookback. O'Neil RS Rating / Minervini trend
template both use 6-12 month (often quarter-weighted) lookbacks. So the fix
isn't just "add a rank transform on top of the existing spread" — the
existing spread itself uses the wrong window for what RS Rating is supposed
to measure. A percentile rank of a 20-day spread is a different (and noisier)
signal than a percentile rank of a 6-12 month spread. Both dimensions should
be decided together, not the window kept and only ranking bolted on.

**(a) Computation approach — no new data source needed, designed for
full-universe scale.** Confirmed: `feature_engine.py` already loads full
multi-year `daily_prices` history per ticker for warmup (needed for
existing EMA200/ATR lookbacks), so a 252-day or 126-day ROC is computable
from data already in scope for every feature-engine run, regardless of
universe size. Cross-sectional rank requires no new provider or table —
it's a same-day groupby-rank over `daily_features` rows already being
written for every active ticker in the run. This scales the same way
whether the universe is 50 names or several thousand: it's a same-day sort
(`O(n log n)`) over already-materialized rows, no per-ticker external
calls, no new data source. The percentile always means "rank within this
platform's active screened universe," not the whole market — that's true
at any scale and isn't itself a caveat.

**Caveat — behavior genuinely differs by universe size, unrelated to
implementation cost:** percentile *granularity* and *statistical
stability* both depend on `n` (the active universe size that day), not on
anything the code does. With `_percentile_rank`'s `bisect_left`-based
formula (`100 * rank / (n-1)`), each rank step is `100/(n-1)` percentage
points — at `n=50` that's ~2.04 points per rank (coarse: "95th percentile"
and "93rd percentile" may be the same handful of names), while at
full-universe scale (hundreds to low-thousands) each step is a fraction of
a point (fine-grained, closer to what "95th percentile" is normally taken
to mean, and closer to O'Neil's own ~8,000-name universe in spirit).
Separately, a percentile computed from a very small `n` is more sensitive
to a handful of outliers dominating the ranking — the usual statistics
guidance is `n>=30` before treating a percentile as stable, which the
current 50-ticker test scale only barely clears and which full-universe
scale clears comfortably. Neither of these is a reason to wait for a
larger universe before implementing — they're properties of the *day's*
active universe size, already correct/self-adjusting at whatever `n` is
active on a given signal_date, and should be documented as a known
characteristic (larger universe -> finer, more stable percentiles) rather
than solved for or gated on.

**(b) Schema landing — `FEATURE_SCHEMA_VERSION` bump required. Field name
decided.** `01a` states setup-mode structural features live in
`features_v02`; `features_v01` is "retained only for historical rows"
(frozen). Adding a new required cross-sectional field is exactly the kind
of structural addition that pattern describes — this should be
`features_v03`, not a silent addition to `v02`, for the same reason `v02`
wasn't back-filled into `v01`: existing `v02` rows were computed without
this field and shouldn't silently return `NULL` under the same version tag
as new rows that have it.

**Field name, approved: `rs_percentile_126d`.** Deliberately distinct in
form from `relative_strength_vs_spy`/`sector_relative_strength` — those are
time-series spreads against a single benchmark; this is a same-day
cross-sectional rank against the active universe, a different mechanism
entirely, and the name is chosen so that difference is obvious on sight
rather than inferred from a shared `relative_strength_*` prefix. The
existing `relative_strength_vs_spy`/`sector_relative_strength` fields stay
exactly as-is (backward compat, other consumers like `ticker_report.py`
unaffected) — `rs_percentile_126d` is additive, not a replacement.

**(c) Lookback window: approved, flat 126-trading-day ROC, no sub-period
weighting.** Cheaper to implement and reason about than O'Neil's
3/6/9/12-month quarter-weighted scheme, closer to what `roc20` already does
mechanically (same `_etf_roc`-style helper, just a bigger window), lower
risk of a subtle weighting bug.

**Skip-most-recent-month variant: considered and deliberately deferred, not
a gap.** Some momentum/RS literature (and a common practitioner variant)
excludes the most recent ~20 trading days from the lookback, on the theory
that very recent short-term reversal noise shouldn't count toward a
medium-term strength read. This is a real, legitimate variant — not
included in V1 because tuning *which* window variant performs better here
is exactly the kind of decision that needs outcome data this platform
doesn't have yet (no diagnostics-backed evidence either window is better
for this universe/setup mix). Consistent with the project's standing
principle of not pre-tuning mechanisms ahead of diagnostic evidence (see
[[p1-adaptive-threshold-deferred]]) — flat 126d ships now as the simplest
correct baseline; a skip-month variant is a candidate future refinement
once there's outcome data to justify it, not something V1 is missing.

**(d) Scoring input vs. hard gate.** Recommend **scoring-only, no hard gate,
for the initial version** — consistent with `01c`'s existing treatment (RS is
a soft "bonus" for trend_continuation today, never a hard check for any
setup type), and consistent with the CLAUDE.md pending-work note itself:
"relative_strength_score for BREAKOUT (high priority)" is listed under
**scoring additions**, not gates. Turning a brand-new, undiagnosed metric
into a hard gate on day one risks rejecting setups for a reason nobody has
validated yet — the same caution already applied to `enforce_compression_floor`
and the deferred adaptive-threshold decision ([[p1-adaptive-threshold-deferred]]
in prior note).

**Not touched:** `validate_breakout()`, `validate_trend_continuation()`,
`feature_engine.py`, schema, `default_configs.py` — all read-only
investigation.

---

## P1.2 — Earnings hard-block option (scoped, not implemented)

Confirmed current state via `m14_setup_validators.py`:
- Shared helper `_compute_penalties()` (line 191) computes a **soft**,
  linearly-ramped `earnings_penalty` for breakout/pullback/trend_continuation,
  called identically in all three validators.
- `validate_consolidation_base()` alone has a **hard** fail (line 1320):
  `if earnings_days is not None and 0 < earnings_days <= min_earnings_days: hard_fails.append("earnings_too_close(...)")`
  — a different config key (`min_earnings_days`) and a different mechanism
  than the soft penalty the other three use.

**Proposed change**, exactly mirroring the already-shipped
`enforce_compression_floor` pattern (`m14_setup_validators.py:1235-1239`,
tests at `test_m14_setup_validators.py:1751-1808`):

- New key `earnings_hard_block: bool` in each of the three setup configs'
  `earnings` block, default `False`.
- In `validate_breakout`, `validate_pullback`, `validate_trend_continuation`,
  after the existing `_compute_penalties()` call: if
  `earnings_hard_block` is `True` and `0 < days_to_earnings_bd <= avoid_within_bd`,
  append a hard fail (e.g. `f"earnings_within_avoid_window({days_to_earnings}bd<={avoid_bd}bd)"`)
  instead of / in addition to the soft penalty. Open question for architect:
  when the flag is on, does the soft penalty still apply on top of the hard
  fail (redundant once `setup_passed=False`, but affects `explanation_json`
  score fields), or is it suppressed? Recommend: still compute and store the
  penalty in evidence (matches consolidation's existing pattern, where
  `earnings_penalty` is separate machinery from the hard `min_earnings_days`
  check and both coexist today).
- Do not add `disposition <= WATCHLIST_ONLY`-only semantics — the note offers
  it as an alternative to hard-reject, but consolidation's existing precedent
  is a straight `setup_passed = False` hard fail, not a disposition cap. Matching
  that precedent avoids introducing a second earnings-gate *shape* into the
  codebase. Flagging for architect confirmation since the note left it as an
  either/or.

**Tests** (byte-identical-by-default proof, same structure as
`TestCompressionFloorGate`): `test_earnings_hard_block_disabled_by_default_soft_penalty_only`
(flag omitted, within-window candidate still passes with penalty applied, not
hard-failed) + `test_earnings_hard_block_enabled_within_window_fails` +
`test_earnings_hard_block_enabled_outside_window_passes`, one set per
validator (breakout/pullback/trend_continuation) or parametrized across all
three if the test harness supports shared fixtures — architect/coder call at
implementation time.

**Explicitly out of scope, not designed here:** growth/surprise-based
earnings signals (CANSLIM C/A, post-earnings drift) — separate future EDGAR
feature-engine item per the note.

**Status: implemented per sign-off (2026-07-08), not committed.** Architect
decided hard-reject (matching consolidation's precedent, as recommended).
Implemented exactly as scoped in `m14_setup_validators.py`: `earnings_cfg.get("earnings_hard_block", False)`
check added as the last hard check in each of `validate_breakout`,
`validate_pullback`, `validate_trend_continuation`, immediately after the
existing ATR stop-floor check — reuses `earnings_cfg`'s existing
`avoid_within_bd` key (no new config key needed beyond the flag itself), and
reuses the `earnings_too_close(...)` reason string for consistency with
consolidation_base's existing hard fail. `default_configs.py` **not**
touched — the flag is absent from `_EARNINGS_BLOCK`, read purely via
`.get(..., False)`, so no active `_v1`/`_strict`/`_template` config carries
it. Added `TestEarningsHardBlockGate` (10 tests: disabled-by-default ×3,
enabled-within-window-fails ×3, enabled-outside-window-passes ×3, plus one
proving `_EARNINGS_BLOCK` has no such key at all) to
`tests/test_m14_setup_validators.py`. Full `test_m14_setup_validators.py`
(173 tests), `test_step5_proposal_engine.py`, and `test_phase6_orchestrator.py`
all green. **Not committed** — per the batch note's "nothing merges without
separate sign-off," this diff is ready for review but awaiting an explicit
merge go-ahead.

---

## P1.3 — Breakout `breakout_prox_min` semantics (analysis only)

**Funnel diagnostics do not currently capture this breakdown.**
`evidence_summaries.breakout` in both available diagnostics snapshots
(`diagnostics_2026-06-26.json`, `diagnostics_2026-06-29.json`) records
percentile stats for `setup_score`, `rvol`, `atr_pct`, `ema20/50_distance_pct`,
`days_in_range`, `estimated_rr_s5`, `stop_distance_pct_s5` — **no
`breakout_proximity` field at all.** Adding it to
`SetupModeFunnelDiagnosticsService`'s evidence collector would be a small,
useful follow-up but is out of scope for this note (analysis only).

**Live DB has only one retained run's worth of raw candidates.**
`step4_analysis` (prod.duckdb) holds rows for a single `signal_date`
(2026-06-23, one `run_id`) — not a history across the four diagnosed dates
(6/11, 6/23, 6/26, 6/29 appear in `pipeline_run_diagnostics`, but the
underlying per-candidate `step4_analysis` rows for the earlier three dates
have since been overwritten/pruned). This means the requested band-vs-outcome
breakdown can only be reconstructed from **this single day**, not a
historical distribution.

**What that one day shows** (queried `explanation_json.breakout_proximity`
directly, since it's present per-row even though not in the diagnostics
rollup):

| Band | n | setup_passed=True |
|---|---|---|
| Anticipatory `[-1.0, -0.05)` | 11 | 0 |
| Confirmed `[-0.05, 0.5]` | 3 | 0 |

All 14 candidates failed on `rvol_below_hard_threshold` — the proximity gate
itself never bound (or unbound) any candidate that day; RVOL was the
uniformly-decisive gate. **This sample cannot answer the question the note
asked.** n=14, one day, and the one gate that actually fired (RVOL) is
orthogonal to the proximity value, so there's no signal here on whether
anticipatory-band candidates behave differently from confirmed-band ones —
not enough passed candidates existed to reach Step 5 stop/target computation,
let alone accumulate realized outcomes. `signal_outcomes` (realized
5/10/20/40bd returns, target/stop hits) has only 25 rows total across *all*
setup types platform-wide; none are breakout candidates from a
proximity-diverse sample.

**Finding, stated plainly: diagnostics aren't ready for this decision yet.**
This mirrors the note's own hedge ("or note if funnel diagnostics aren't
ready yet"). Recommend: (1) add `breakout_proximity` to the diagnostics
evidence collector (cheap, unblocks this the next time diagnostics run), (2)
revisit this question after several more pipeline runs accumulate enough
breakout candidates that clear the RVOL gate to populate both proximity
bands with passing rows, or after `signal_outcomes` has enough breakout rows
to compare. **No config value changed** — `breakout_prox_min` remains `-1.0`
in the active config.

**Status: instrumentation implemented per sign-off (2026-07-08), not
committed.** Added `breakout_proximity` collection to `_rpt_evidence()` in
`app/services/diagnostics/funnel_diagnostics.py`, gated by `st == "breakout"`,
alongside the existing `consolidation_base`-only special case. **Important
caveat surfaced while implementing:** `_rpt_evidence` only collects any
evidence field — this one included — for rows where `setup_passed` is
`True` (same gating every other field in that function already uses,
confirmed by reading the surrounding loop, not assumed). That means on a day
like 2026-06-23 (0 of 14 breakout candidates passed), the new field would
still be **absent from the report entirely**, not present with `n=0` — it
doesn't retroactively fix the specific sample-size problem found above, it
only starts capturing the value on future days where at least one breakout
candidate passes. Added 3 unit tests in `tests/test_phase6_diagnostics.py`
(`test_evidence_summaries_collects_breakout_proximity_for_passed_breakout`,
`..._absent_when_no_passed_rows`, `..._other_setup_types_have_no_breakout_proximity_key`)
directly exercising `_rpt_evidence`, since no existing test covered that
function at all. `test_phase6_diagnostics.py` + `test_funnel_diagnostics.py`
(re-export) both green. **Not committed**, config untouched, semantic
decision on `breakout_prox_min` still open as above.

---

## P1.4 — Within-family score standardization (design proposal)

**Confirmed structural asymmetry**, reading `default_configs.py`
`scoring_weights` directly:

| Setup type | Volume-adjacent credit |
|---|---|
| Breakout | RVOL **hard gate** (`rvol_is_hard=True`) *and* `volume_expansion` weight **0.20** *and* `breakout_confirmation` (0.25) itself blends in `close_strength` (`0.6*proximity + 0.4*close_strength`, `m14_setup_validators.py:503`) |
| Pullback | **zero** — no volume-weighted scoring component at all (`rvol_is_hard=False`, soft penalty only) |
| Trend continuation | `volume_health` weight 0.10 (default) or 0.05 (template) |
| Consolidation base | `volume_dry_up` weight 0.15 — different direction (rewards *low* volume, not expansion) |

So breakout's `setup_score` structurally runs hot relative to pullback's on
any day with elevated volume, independent of the known M15 double-credit bug
(a separate, already-tracked issue per the note). Confirmed via
`step5_proposal_engine.py:866-872` (`_proposal_score_raw`) that `setup_score`
feeds into the shared cross-setup ranked list **as a raw, unnormalized
input** (`_W_SETUP * setup_score`), and `_sort_key` (line 1397-1399) sorts
`ticker_best.values()` — i.e. all setup types — into one global `all_ranked`
list by `-proposal_score_raw`. There is no per-family normalization anywhere
between M14's `setup_score` output and M15's merge. This confirms the note's
premise structurally, not just anecdotally.

**Proposed approach: same-day, same-family percentile-rank transform,
ranking-stage only.** Before merging into `all_ranked`, replace each
candidate's `setup_score` contribution to `_proposal_score_raw` with its
percentile rank within that day's same-`setup_type` distribution (i.e.,
"how does this breakout score compare to today's other breakout candidates,"
not "how does it compare to today's pullback candidates"). Concretely: group
`ticker_best` by `setup_type`, rank each group's `setup_score` values
(0-100), pass the rank in place of the raw score to `_proposal_score_raw`'s
`_W_SETUP` term. This is a **ranking-stage transform only** — confirmed no
schema change needed (`setup_score` itself is untouched and still stored
raw in `step4_analysis`/`step5_proposals` for audit/display; only the
value fed into the *ranking* formula changes). `ticker_report.py` and any
other consumer of the stored `setup_score` column are unaffected.

**Interaction with the known double-credit bug**: the note is right that
these are independent and don't substitute for each other. Double-credit (if
it's RVOL counted once as a hard gate *and* again inside a soft weight, e.g.
breakout's own `volume_expansion` scoring after already hard-gating on RVOL)
inflates breakout's *absolute* score. Family-standardization corrects
*relative* comparison across families regardless of any one family's absolute
inflation — but if double-credit also makes breakout's *within-family*
distribution artificially compressed near the top (every passing breakout
candidate double-credited similarly), percentile rank could still overstate
confidence in a merely-average breakout candidate. Fixing double-credit first
would give a cleaner distribution to rank; doing standardization first
doesn't block or corrupt the double-credit fix later. No dependency ordering
required, but doing double-credit first is probably the better sequencing
since it's already a tracked P0 item.

**Not implemented.** This is a scoping note for a future M15 ranking-stage
change. Per the note: M15 is in the AD-22.24 exemption scope, but ranking
logic changes affect live disposition output, so this explicitly awaits
architect sign-off before any diff is written.

### P1.4 — Follow-up (concrete design, requested 2026-07-08 sign-off round)

**Caveat up front on the double-credit bug**: I could not find it documented
anywhere in this repo — no hits for "double-credit"/"double_credit" in any
`.md` or `.py` file, and neither `m14_gate_fixes.md` nor `01c` nor any
scratchpad note mentions it. It appears to exist only in prior conversation
outside this repo's tracked history. My description of it below (RVOL
double-counted via the hard gate *and* the `volume_expansion` soft weight)
is a structural hypothesis built from reading `validate_breakout()`, not a
confirmed root cause — whoever owns that P0 item should confirm or correct
this before it's used to justify sequencing.

**Exact integration point — corrected after checking what's actually in
scope where.** My first pass at this proposed inserting the percentile-rank
step *after* the `ticker_best` dedup (~line 1387-1401), operating on the
`enriched` dict's stored fields. Checking those fields against what's
actually stored (`step5_proposal_engine.py:1341-1384`) shows that won't
work as written: `confirmation_score`, `contrarian_risk_score`,
`audit_consistency_score`, and `fundamentals_quality_score` are **local
variables inside the per-candidate loop, never written into the `enriched`
dict** — only `setup_score`, `estimated_rr`, `market_regime`,
`stop_distance_pct` are. A post-dedup step can't reconstruct
`_proposal_score_raw`'s inputs from `ticker_best.values()` alone.

**Corrected approach: a lightweight pre-pass before the main loop**, using
only `a["setup_score"]` from the raw `analyses` rows (available before any
of the stop/target/risk computation the main loop does), then apply the
percentile inline where `psc_raw` is already computed today
(`step5_proposal_engine.py:1326-1335`), where all the real inputs are still
in scope:

```python
# Pre-pass, before the main `for a in analyses:` loop (~line 1191):
by_type_scores: dict[str, list[float]] = {}
for a in analyses:
    if a["setup_passed"]:
        st = a.get("setup_type") or ""
        by_type_scores.setdefault(st, []).append(_f(a["setup_score"]) or 0.0)
for scores in by_type_scores.values():
    scores.sort()

def _family_percentile(setup_type: str, score: float) -> float:
    scores = by_type_scores.get(setup_type) or []
    n = len(scores)
    return 100.0 if n <= 1 else 100.0 * bisect.bisect_left(scores, score) / (n - 1)

# Inside the main loop, right before the existing psc_raw call (line 1326):
setup_score_family_percentile = _family_percentile(setup_type, setup_score)
psc_ranked = _proposal_score_raw(
    setup_score_family_percentile, estimated_rr, confirmation_score, market_regime,
    stop_distance_pct=stop_distance_pct,
    contrarian_risk_score=contrarian_risk_score,
    audit_consistency_score=audit_consistency_score,
    contrarian_penalty_weight=cfg["contrarian_penalty_weight"],
    audit_penalty_weight=cfg["audit_penalty_weight"],
    fundamentals_quality_score=fundamentals_quality_score,
    fundamentals_score_weight=cfg["fundamentals_score_weight"],
)
# ...then add to the enriched dict alongside the existing proposal_score_raw:
#   "proposal_score_ranked": psc_ranked,
```

Then change `_sort_key` (currently `-x["proposal_score_raw"]`, line 1399) to
sort on `-x["proposal_score_ranked"]` instead. **Recommendation: keep
`proposal_score_raw`/`proposal_score_final` computed and stored exactly as
today** (audit trail, dashboard display, any historical analysis stays
comparable) — only the new `proposal_score_ranked` field drives the sort.
This is the safer of two options; overwriting `proposal_score_raw` itself
would change the stored meaning of a column other code/dashboards already
read, for the same ranking benefit.

**Open question this correction surfaces**: the `ticker_best` dedup
(line 1394, `if t not in ticker_best or item["proposal_score_raw"] > ...`)
compares a single ticker's *own* candidate routes (e.g. routed to both
breakout and pullback) using the same raw, cross-family-unfair
`proposal_score_raw` this whole item is trying to fix. Whether dedup should
also switch to `proposal_score_ranked` is a real question, not addressed by
the note's original framing (which only asked about the final merge into
the shared ranked list) — flagging for the architect rather than deciding
it here.

**Why percentile rank, not z-score, concretely:** z-score
(`(x - mean) / stdev`) is undefined/unstable when a family has 1-2 candidates
on a given day (`stdev` near 0 or undefined) — the actual 2026-06-29 sample
had pullback with only 1 selected candidate and consolidation_base with 0
passing. Percentile rank degrades gracefully to `100.0` for a lone candidate
(no peers, ranks at the top of its own family) instead of producing `NaN`/`inf`.

**Test additions needed at implementation time** (not written now): a
same-day fixture with 3 breakout candidates (scores e.g. 90/70/50) and 1
pullback candidate (score 60) — assert the pullback candidate's
`proposal_score_ranked` reflects "100th percentile of its own family" while
its raw `setup_score`/`proposal_score_raw` are unchanged from today's values,
and assert the breakout candidates rank relative to each other exactly as
raw `setup_score` would (percentile rank preserves within-family order, only
changes cross-family comparability).

**Go/no-go needed on:** (a) confirm the integration point (pre-pass over
`analyses` feeding into the existing `psc_raw` call site) is correct given
the double-credit bug's actual location once that's found in code — if
double-credit turns out to live inside `setup_score` itself (M14) rather
than inside `_proposal_score_raw` (M15), fixing it there first changes what
distribution this transform ranks, per the sequencing note above; (b)
confirm keeping `proposal_score_raw` stored as today (recommended) vs.
redefining it to the ranked value; (c) whether the `ticker_best` dedup
comparison should also move to the ranked score (open question surfaced
above, not decided here).

**Status: go/no-go received 2026-07-08 — joint fix approved.** Architect
decided this item ships together with the Cause 1 fix from
[[m15_double_credit_bug_finding]] (which confirmed the double-credit
mechanism lives in M15's `_proposal_score_raw`, not M14's `setup_score` —
resolving open question (a) in favor of the pre-pass approach as designed).
(b) confirmed: `proposal_score_raw`/`proposal_score_final` keep their
existing stored meaning; the new `proposal_score_ranked` field drives
ranking only, not persisted. (c) — the `ticker_best` dedup question — was
**not** addressed by the go/no-go and remains open, left unchanged in the
implementation. See [[m15_double_credit_bug_finding]]'s "Implementation"
section for the actual diff, test results (real measured redistribution:
40/30/30 split vs. the 100%-breakout baseline), and the exact code
locations. **Implemented, not committed** — held pending review, same
pattern as P1.2/P1.3.

---

## Summary / exit criteria (updated after 2026-07-08 sign-off round)

- **P1.1 (RS percentile):** still design-only, per sign-off. Added universe
  provenance answer (production, not dev/debug; 50 tickers) and flagged
  in-progress uncommitted universe-expansion work as a sequencing
  consideration. Awaiting: lookback window choice (flat 126d recommended
  vs. O'Neil-weighted) and field naming before `features_v03` work starts.
- **P1.2 (earnings hard-block):** **implemented, not committed.** Hard-reject
  decided by architect. Code + 10 tests in place in
  `m14_setup_validators.py` / `test_m14_setup_validators.py`; full relevant
  suites green. Awaiting merge go-ahead only.
- **P1.3 (breakout prox_min):** **instrumentation implemented, not
  committed.** `breakout_proximity` now collected in diagnostics evidence
  (with the important caveat that it's still gated on `setup_passed=True`,
  so it won't retroactively help small-sample days like 2026-06-23). 3 new
  tests in `test_phase6_diagnostics.py`, green. Config (`breakout_prox_min`)
  untouched; semantic decision remains open pending more data.
- **P1.4 (score standardization):** still design-only, per sign-off
  requiring a separate go/no-go. Delivered the concrete integration point,
  formula, and test plan requested — corrected mid-write once I checked
  which fields actually exist on the `enriched` dict at the point I first
  proposed inserting the transform (they don't; moved the insertion point to
  a pre-pass over `analyses` feeding the existing `psc_raw` call site
  instead). Flagged that the "double-credit bug" isn't documented anywhere
  in this repo — my description of it is an unconfirmed hypothesis.
  Surfaced one new open question (does `ticker_best` dedup need the same
  fix?) not covered by the original note.

**Nothing committed in this round.** P1.2 and P1.3 diffs exist in the
working tree, tested and green, ready for review/merge on your word. P1.1
and P1.4 remain analysis/design only.

## Final status (2026-07-08, end of day)

- **P1.1:** design-only, updated 2026-07-08 round 3: universe size
  clarified as a deliberate test-speed choice (50 tickers), not the
  production target — the "wait for universe expansion" framing has been
  dropped entirely. Design now targets full-universe scale (hundreds to
  low-thousands of tickers) directly; scale-dependent behavior (percentile
  granularity, minimum stable sample size) is documented as an explicit
  caveat in the P1.1 section above, not treated as a blocker. **Design note
  now complete (2026-07-08, round 4):** lookback approved as flat 126d, no
  skip-most-recent-month adjustment for V1 (that variant is noted as
  deliberately deferred pending outcome data, not a gap); field name
  approved as `rs_percentile_126d`, deliberately distinct in form from
  `relative_strength_vs_spy`/`sector_relative_strength` since it's a
  different mechanism (cross-sectional rank vs. time-series benchmark
  spread). **Held for architect review of the finished design note before
  any `features_v03` schema/code work begins** — same pattern as P1.4 was
  held before implementation.

### P1.1 — Implementation (2026-07-08, round 5, approved for implementation)

**Implemented, not committed.** `FEATURE_SCHEMA_VERSION` bumped
`features_v02` -> `features_v03` (`app/config/constants.py`). New column
`rs_percentile_126d DOUBLE` added to `daily_features`
(`app/database/schema_manager.py`) — additive only, no `ALTER TABLE`
(confirmed forbidden project-wide via `test_schema_manager.py`'s static
source scan); a fresh `init_prod_db.py`/`init_debug_db.py` run is required
before any *existing* `prod.duckdb`/`debug.duckdb` file gets the new
column — same reinit-not-migrate pattern this project already uses for
schema bumps (no in-place `ALTER TABLE` mechanism exists anywhere in this
codebase).

`app/services/features/feature_engine.py`: added `roc126` (126-trading-day
ROC, `close_adj / close_adj.shift(126) - 1`) as a Polars-vectorized column
alongside the existing `roc20`. New module-level `_percentile_rank()`
helper (same `bisect_left`-based formula as the one added to
`step5_proposal_engine.py` for P1.4 — not shared/imported across modules,
kept as parallel local helpers per module, consistent with this file's
existing self-contained-helper style). `_build_feature_rows` now does a
post-pass after the per-ticker loop: groups completed rows by
`feature_date` (not assumed single-date per run), sorts each group's valid
`roc126` values, and assigns `rs_percentile_126d` per ticker.
`roc126` itself is a transient key on the row dict (`_assemble_row` sets
it, the post-pass pops it) — not persisted; only `rs_percentile_126d` is
written to `daily_features`.

**Specs updated:** `01a` (version bump + field description), `01b`
(schema DDL), `01c` (new "Cross-sectional RS percentile" formula section,
including the skip-month-deferred and granularity/stability caveats),
`M11_FEATURE_ENGINE_SPEC.md` (header, source references, new §6a, both
per-column tables).

**Tests** (`tests/test_feature_engine_v03.py`, new file, 11 tests):
`_percentile_rank` unit tests; schema-version-bump regression check;
NULL-propagation for <126 bars *and* confirmation a short-history ticker
doesn't skew a same-day peer's ranking; **the granularity caveat directly
tested**, not just documented — n=5 gives exact 25-point steps, n=21 gives
exact 5-point steps, same mechanism, both asserted; and a test proving
`rs_percentile_126d` is independent of `relative_strength_vs_spy` (two
tickers, identical 20d return, different 126d return, different
percentile) — caught and fixed a real math error of my own while writing
it (uniformly scaling a whole price series doesn't change % returns; the
first draft accidentally produced identical `roc126` for both tickers).

**Regressions found and fixed** (3 existing tests hardcoded
`"features_v02"` as an expected value, now legitimately wrong):
`test_step3_universal_eligibility.py` (constant equality),
`test_feature_engine_v02.py::TestSchemaCompatibility` (renamed
`test_feature_schema_version_is_v02` -> `_is_v03`), `test_schema_manager.py`
(constant equality; also added a parallel `FEATURES_V03_COLUMNS`
existence-check test mirroring the existing v02 one). Full suite (minus
the 3 already-known unrelated pre-existing failures) green.

**Not touched, deliberately out of scope:** wiring `rs_percentile_126d`
into any setup validator's `scoring_weights` (breakout, trend_continuation,
etc.) — the go/no-go's "scoring-only, no hard gate" describes the field's
intended *nature*, not an instruction to wire it into a formula this
cycle; per CLAUDE.md ("do not add scoring components... before diagnostics
data is available") and the design note's own P1.1(d) reasoning, that's a
separate future decision. Also not touched: `ticker_report.py`,
`simulation_engine.py`'s replay SELECT list — neither was asked to consume
the new field and both are unaffected by its addition (nullable, additive
column).

**Status: held uncommitted for review**, same as P1.2/P1.3/P1.4 were.
- **P1.2:** **committed** (`af6deed`, `m14_earnings_hard_block_gate_stable`).
- **P1.3:** **committed** (`0015a85`, `m22_breakout_proximity_evidence_instrumentation_stable`).
- **P1.4:** go/no-go received, joint fix (Cause 1 + standardization)
  **implemented and committed** (`00867ae`, `module15_score_standardization_stable`)
  — see [[m15_double_credit_bug_finding]]'s "Implementation" section for the
  actual diff and verified redistribution results. The double-credit bug
  itself was a real, previously-established finding (not the unconfirmed
  hypothesis this note originally flagged it as) — corrected and recorded
  in that standalone note after byte-exact verification.
