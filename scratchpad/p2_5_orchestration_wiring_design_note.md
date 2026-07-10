# P2.5 — M20 Orchestration Wiring: Design Note

**Status:** implemented, uncommitted, held for sign-off.
**Date:** 2026-07-10.

---

## Three corrections to the coder note's premises

The note's fundamentals decision rested on beliefs that don't hold against the code.
All three were surfaced before implementation and confirmed by Andrey.

### 1. `fundamentals_scores` is NOT "downgrade-only same as `ai_review_scores`"

- `ai_review_scores` genuinely is downgrade-only — both terms subtract
  (`step5_proposal_engine.py:894,896`).
- `fundamentals_scores` is **two-sided**:
  `base += fundamentals_score_weight * (clamp(q) - 50.0)` (line 898). The
  module comment (lines 166–173) already said so explicitly.

Consequence: at a non-zero weight, a high-quality ticker *gains* score,
reorders `raw_rank`/`in_raw_top_n`, and **can be promoted into a new BUY**.
"Cheap and deterministic → just always run it" is right about *cost* and wrong
about *blast radius*.

### 2. There was no fundamentals-scoring service to call

The note said "add a call to the fundamentals-scoring function/service".
None existed:

- M20's `_step_fundamentals` was already unconditional and already wired — but it
  only **refreshes raw `ticker_fundamentals` rows**, it computes no score.
- The 0–100 quality formula existed only inside M14's private
  `_compute_fundamentals_adjustment`, and it returned an *adjustment*
  (`weight*(q-50)/50`), not `q`.

So feeding Step 5 required extracting `avg_quality` into a shared helper.
Duplicating the formula would have guaranteed drift between M14 and M15.

### 3. "Zero-weighted everywhere today" was false

`_W_FUNDAMENTALS` defaulted to **0.10**, not 0. And `DEFAULT_RISK_LABEL_CONFIG`
carried no `fundamentals` block at all, so the fallback applied.

The deeper problem: **Step 5 reads `risk_label_config` from the DB**, not from
`default_configs`. Every `risk_label_config` row active today predates Phase 4,
carries no `fundamentals` block, and **config is immutable** (clone-and-version,
never edit in place). Seeding a new default therefore would *not* have protected
the live prod DB — the 0.10 fallback would have applied on the very next run,
silently moving trade decisions.

This made three of the stated requirements mutually contradictory
(`auto_invoke=True` + guard-as-specified + byte-identical output).

---

## Resolution

`_W_FUNDAMENTALS` is now **0.0** — the term is inert unless a `risk_label_config`
explicitly opts in. `DEFAULT_RISK_LABEL_CONFIG["fundamentals"]["score_weight"]`
is seeded `0.0` to document the knob. Both new and existing DBs are therefore
byte-identical, and activation is one config value (0.10 gives the reference
±5 points at quality 100/0).

This is what makes "auto-invoke fundamentals now" safe: M20 computes and threads
the score end-to-end on every run, but it contributes exactly zero until a human
raises the weight, post-backfill.

---

## Double-credit: latent, now guarded in two places

M14 folds its fundamentals adjustment into `penalized_score` → `setup_score`,
and Step 5 weights `setup_score` at `_W_SETUP = 0.40` **and** adds its own term
keyed by the same five fields. Both active ⇒ the signal is counted twice —
the defect class of `m15_double_credit_bug_finding.md` (commit `86ecbfd`).

Dormant today: no seeded `setup_config` has a `fundamentals` block, so M14's
adjustment is exactly `0.0`. It arms the moment anyone sets `enabled=True`.

Guarded at both layers, per Andrey's two answers (which specified different
mechanisms — both were implemented, they compose):

| Layer | Mechanism | Rationale |
|---|---|---|
| Authoring | `ConfigService.validate_setup_config` rejects `fundamentals.enabled=True` while `score_weight != 0`, naming the conflict | Primary. Fails loud where a human can see it. Same creation-time pattern as the AD-22.23 `rvol_is_hard` check. |
| Scoring | `step5_proposal_engine._m14_owns_fundamentals` suppresses Step 5's term per-row | Defense in depth. The validator has no callers in the seeding/pipeline read paths, so a config row persisted before the check still can't double-count. M14 ran first, so M14 wins. |

**The rejection is dormant today, by construction.** It fires only when Step 5's
term is *active* (`score_weight != 0`), per the stated rule. With the seeded
`0.0` weight, authoring a `fundamentals.enabled=True` setup_config is *accepted* —
correctly, because Step 5 contributes nothing, so nothing is double-counted. The
guard arms the moment the weight is raised. The rejection test forces a live
weight to exercise it; a companion test pins the seeded-inert case as allowed.

**Divergence from the final decision set, flagged.** The consolidated note says
the guard should fail "at validation time, not scoring time." The scoring-time
suppression is retained anyway, because `validate_setup_config` has *no callers
in the seeding or pipeline read paths* (its own code comment says so) — a config
row can be active without ever having been validated, and in that case the
validator cannot protect the run. It is a backstop, not the enforcement point.
Removing it is ~5 lines in `_build_rows` plus one test; say the word.

`_m14_owns_fundamentals` mirrors M14's own activation test exactly
(`enabled` truthy **and** non-zero `weight`) — a config with
`enabled=True, weight=0` contributes nothing in M14, so Step 5 stays free to score.

---

## AI review: provisioned, off, and one decision still open

Flag `pipeline.auto_invoke_ai_review = False`, never flipped. Wiring is behind an
injected `ai_review_scores_provider` seam, `None` by default — so the default
pipeline never constructs an M18/M19 engine, never writes a review ZIP, and never
makes a paid call.

Two things the note didn't anticipate:

1. **Enabling it makes Step 5 run twice per `signal_date`.** `step5_proposals`
   must exist before M18 can export them and M19 can review them, so the scores
   cannot exist during the first pass. M15's `_write` is INSERT-only, so the
   first pass's rows are deleted before re-proposing. This is exactly the
   "timing / re-run semantics" Phase 3 flagged as deferred.

2. **`ai_reviews` has no `ticker` column.** One row covers many tickers via
   `selected_tickers_json`, keyed by `setup_config_id`. But Step 5 wants
   `dict[ticker, {...}]`. Producing it requires *broadcasting* an export's
   contrarian/audit score across every ticker in that export — a coarse and
   unreviewed semantic.

**This is why no default provider ships.** Baking the broadcast semantic into
production for a code path that can never execute would be shipping unreviewed
trade logic. The seam is in place; the correlation decision is Andrey's, and it's
the same follow-up Phase 3 flagged.

Failure handling: any error in the AI review path degrades to the first pass's
already-committed proposals. An unreviewable AI response must not cost the run.

---

## Naming: `orchestrator_config` does not exist

The note names `orchestrator_config.auto_invoke_fundamentals` /
`.auto_invoke_ai_review`. There is no `orchestrator_config` anywhere in the
codebase — it appears only in `CLAUDE.md`, specs, and scratchpad notes. The real
home is the `pipeline` runtime config
(`default_configs.DEFAULT_RUNTIME_CONFIGS["pipeline"]`, served by
`ConfigService.get_active_runtime_config(db_role, "pipeline")`; the
`runtime_configs` *table* was retired with strategy-mode, so these are served from
in-memory defaults). Both flags live there. If a real `orchestrator_config`
namespace is wanted, that's a separate change.

## Backlog (logged, not implemented — Andrey's explicit instruction)

**Retire M14's fundamentals adjustment entirely**, making Step 5 the sole owner
of the fundamentals signal. This would remove the double-credit hazard at the
root rather than guarding it in two places, and would collapse two weights
(`setup_config.fundamentals.weight`, `risk_label_config.fundamentals.score_weight`)
into one. Out of scope here: it changes M14 scoring semantics beyond orchestration
wiring. Worth revisiting once diagnostics show whether the fundamentals signal
carries in the first place.

---

## What shipped

- **New** `app/services/fundamentals/fundamentals_quality.py` — single source of
  truth for the 0–100 formula (`compute_fundamentals_quality`), the point-in-time
  reader (`read_fundamentals_map`), and the ticker→score map
  (`build_fundamentals_scores`). Altman zones + valuation-band map live here now.
- `m14_setup_validators.py` — `_compute_fundamentals_adjustment` delegates to the
  shared helper; `_ALTMAN_*`/`_VALUATION_BAND_QUALITY` re-exported for existing callers.
- `step4_setup_validation_engine.py` — `_read_fundamentals` delegates to the shared
  reader; the duplicated SQL is now an alias.
- `step5_proposal_engine.py` — `_W_FUNDAMENTALS` 0.10 → **0.0**; new
  `_m14_owns_fundamentals` predicate; per-row suppression in `_build_rows`.
- `config_service.py` — `validate_setup_config(config_json, risk_label_config=None)`
  gains the double-credit rejection; two module-level predicates.
- `default_configs.py` — `risk_label_config.fundamentals.score_weight = 0.0`;
  `pipeline.auto_invoke_fundamentals = True`, `pipeline.auto_invoke_ai_review = False`.
- `pipeline_orchestrator.py` — `_pipeline_flags`, `_valuation_band_quality`,
  `_build_fundamentals_scores`, rewritten `_step_step5`, `_rescore_with_ai_review`,
  `_delete_step5_rows`; new `ai_review_scores_provider` ctor seam.
- Specs: `01c_FORMULAS_AND_CONFIGS.md` (the fundamentals term was **never**
  documented — pre-existing Phase 4 drift, fixed here),
  `M20_PIPELINE_ORCHESTRATOR_SPEC.md`, `CLAUDE.md`.

## Tests

`tests/test_p2_5_orchestration_wiring.py` (34 tests, offline except the two
DB-backed golden tests, which use a temp DuckDB):

- **Golden byte-identity** — `test_auto_invoked_fundamentals_change_nothing_under_seeded_weight`
  drives the real `propose()` against a temp DuckDB and compares *every* written
  column (minus `proposal_id`/`run_id`/`created_at`) before vs after M20 feeds
  scores. A control test proves it isn't vacuous: the same scenario *does* move
  once the weight is raised to 0.10.
- **Double-credit, engine-level** — `test_step5_term_suppressed_end_to_end_when_m14_owns_fundamentals`
  runs `propose()` three ways at a *live* weight: no scores fed, M14-off, M14-on.
  Asserts M14-off raises the score and M14-on returns exactly to baseline.
  Verified to actually bite: stubbing out the `_build_rows` guard makes it fail
  with `ALPHA: Step 5 double-counted a signal M14 already owns`. (The earlier
  version of this test re-implemented the guard inside the test and would have
  passed with the guard deleted — replaced.)
- **Validator rejection** — asserts failure and that the message names
  `fundamentals.enabled`, `score_weight`, and "scored twice"; plus the
  seeded-inert case is allowed, and all four seeded setup_configs validate.
- **AI review** — flag `False` ⇒ one `propose()` call, no `ai_review_scores`
  kwarg, no `DELETE`, and an injected provider that raises if touched; flag
  `True` ⇒ provider invoked, two passes, Step 5 receives the scores,
  fundamentals still carried through; provider failure / empty scores / missing
  provider all degrade to the first pass.

Four existing `test_step5_proposal_engine.py` tests were updated: they asserted
the old live 0.10 default. They now pass the weight explicitly (preserving formula
coverage), and a new test pins the inert default. This is a deliberate,
documented default change — see correction 3.
