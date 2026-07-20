# CLAUDE.md Diagnostics Clarification (Item 1) + ema150 Dormant Field (Item 2)

**Date:** 2026-07-20
**Status:** Item 1 investigation-only (no implementation). Item 2 implemented, tested, not committed.

---

## Item 1 — Does the 5-date campaign satisfy CLAUDE.md's diagnostics precondition?

### Exact, literal wording (CLAUDE.md, "Current state & next steps")

```
Pending (in order):
1. Funnel diagnostics — collect real data on false_breakout_rate, target_hit_rate, pullback_failure_rate
2. Scoring additions (post-diagnostics only):
   - relative_strength_score for BREAKOUT (high priority)
   - sector_strength_score for BREAKOUT/PULLBACK, nearby_resistance_penalty for PULLBACK (medium)
3. Config threshold tuning — deferred until diagnostics provide empirical signal

Do not add scoring components or tune thresholds before diagnostics data is available.
```

Quoted verbatim, character-for-character, from the current `CLAUDE.md` on disk.

### Assessment: NOT ambiguous — the wording names three specific metrics, and none of them have been computed anywhere

I grepped the entire repository (source, tests, specs, and every `reports/*.md`,
including the AD-22.26 campaign's own closure/rescale/reverification reports)
for the three literal metric names:

- **`false_breakout_rate`** — appears **nowhere** in the codebase except
  `CLAUDE.md` itself. Zero occurrences in any module, test, spec, or report.
- **`pullback_failure_rate`** — same: appears **nowhere** except `CLAUDE.md`.
- **`target_hit_rate`** — the one of the three that *does* exist elsewhere,
  but not from the funnel-diagnostics campaign: it's a column in
  `sim_config_comparisons` (`schema_manager.py:768`) and a value computed by
  `app/services/learning/config_recommender.py` (M23) from **M17 simulation
  replay outcomes** — a completely different subsystem (backtest/simulation)
  from M22's funnel diagnostics.

`app/services/diagnostics/funnel_diagnostics.py` (M22, the module the 5-date
campaign actually ran) computes a different vocabulary entirely: `pass_rate`
per validator, rejection-reason breakdowns, borderline-proximity sorting,
routing/eligibility counts, evidence-distribution percentiles. These are
**screening-funnel yield metrics** — how many candidates survive each gate
and why — not outcome/backtest metrics about what happened to a trade after
it was taken.

The project's own first funnel-diagnostics report,
`reports/funnel_diagnostics_2026-07-10.md`, already drew this exact
distinction explicitly, **before** the 5-date campaign ran (2026-06-11
through 07-08) or this coder note was written:

> **Method:** generated against a pipeline run resumed from `validation`
> (earnings/fundamentals refresh skipped). **Yield metrics only — outcome
> rates (false-breakout, target-hit, pullback-failure) require the outcome
> queue to mature.**

The 5-date campaign's actual deliverable, per
`reports/ad_22_26_option1_rescale_implementation_2026-07-15.md`, was
`penalized_score` distribution calibration (`setup_score` percentiles per
setup_type) — the data that justified the `consolidation_base` rescale
(AD-22.26) and validated the breakout RVOL gate's threshold. That is real,
completed, valuable diagnostics work, and it satisfies a reasonable
*informal* reading of "funnel diagnostics" as a general activity — but it is
not the specific three-metric deliverable CLAUDE.md's item 1 names.

### Honest answer

**No, the campaign does not satisfy CLAUDE.md item 1's literal wording.**
This is not ambiguous — I'm not choosing between two plausible readings. The
wording names three specific rates; two of the three (`false_breakout_rate`,
`pullback_failure_rate`) have never been computed anywhere in this codebase,
and the third (`target_hit_rate`) exists only as an M17/M23 simulation-replay
metric, unconnected to the funnel-diagnostics campaign or module. The
project's own prior documentation (`funnel_diagnostics_2026-07-10.md`,
written 3 days before this session's Part B report and 5 days before the
campaign's later dates even ran) pre-emptively made the same distinction I'm
making now — "yield metrics" (what the campaign measured) versus "outcome
rates" (what item 1 asks for, "require the outcome queue to mature").

**Whether the outcome queue has now "matured" enough to actually compute
these three rates is a separate, answerable question I did not chase down**
(it depends on how many of the 5 campaign dates' 5bd/10bd/20bd horizons have
resolved as of 2026-07-20 — `reports/AD-22.26_closure_2026-07-15.md` notes
some already had by 07-15) — but "the data might now be available to compute
these rates" is different from "these rates have been computed," and only
the latter would satisfy the literal wording. Nobody has computed them yet.

**No implementation was attempted for this item**, per the note's explicit instruction.

---

## Item 2 — ema150 dormant field (implemented)

### Diff summary

**`app/config/constants.py`**
- `FEATURE_SCHEMA_VERSION` bumped `"features_v04"` → `"features_v05"`.
- Module docstring: new paragraph documenting the v05 bump (mirrors the
  existing v02/v03/v04 paragraphs).

**`app/database/schema_manager.py`**
- `daily_features` DDL: `ema150 DOUBLE,` added immediately after `ema200
  DOUBLE,` (before `ema_alignment_score`), with a comment noting it's
  dormant pending the trend_continuation gate decision.
- Module docstring's `FEATURE_SCHEMA_VERSION` history line updated to v05.
- Per this module's own documented discipline ("Reinit, not migrate:
  `daily_features` is a derived table... existing rows are discarded by the
  wipe/backfill, never ALTERed in place"), **no migration/ALTER TABLE was
  written** — none exists for any prior `features_v0N` bump either. A
  production wipe+backfill of `daily_features` is required before `ema150`
  populates for historical rows; that operational step is out of scope here,
  same as it was for v02/v03/v04.

**`specs/01b_SCHEMA_AND_DATA.md`**
- Mirrored the same `ema150 DOUBLE, -- v05 (2026-07-20), dormant` addition
  in the spec's copy of the DDL, following the precedent set by `market_cap`/
  `vcp_sequence_score`'s v04 entries in the same file.

**`app/services/features/feature_engine.py`**
- Module docstring: header bumped to `features_v05`; new prose paragraph
  documenting `ema150` (mirrors the existing v02/v03/v04 paragraphs), explicit
  that it's dormant and that the MA-stacking gate decision is separate/open.
- `_MIN_BARS_EMA150: Final[int] = 150` (new constant, alongside the existing
  `_MIN_BARS_EMA20/50/200`).
- Stage B: `pl.col("close_adj").ewm_mean(span=150, adjust=False)...alias("_ema150_raw")`
  — identical construction to `_ema20_raw`/`_ema50_raw`/`_ema200_raw`.
- Stage D: `pl.when(bar_index >= _MIN_BARS_EMA150).then(_ema150_raw).otherwise(None).alias("ema150")`
  — identical masking pattern to `ema20`/`ema50`/`ema200`.
- `OPTIONAL_FEATURE_COLUMNS`: `"ema150"` added under a new `# v05 new` group
  — **not** `REQUIRED_FEATURE_COLUMNS`. See "Design choice" below.
- `_FEATURE_PARAM_COLUMNS`: `"ema150"` inserted immediately after `"ema200"`,
  matching the DDL column position exactly (this list "must match daily_features
  DDL order exactly" per its own comment; the upsert SQL is generated from it
  programmatically, so no separate SQL string needed editing).
- `_select_base` (the Polars column-select list feeding row assembly):
  `"ema150"` added.
- `_assemble_row`: `"ema150": _sanitize(rec["ema150"])` added to the returned
  dict — a plain pass-through, no new function parameter needed (unlike
  per-ticker-computed fields like `swing_high`).

**`app/services/screening/m14_setup_validators.py`**
- One docstring comment fixed (`feature_version: str  e.g. "features_v04"` →
  `"features_v05"`) — an illustrative example only, not a behavioral pin, but
  now accurate.

### Design choice: OPTIONAL, not REQUIRED

`ema20`/`ema50`/`ema200` are all in `REQUIRED_FEATURE_COLUMNS`, which
directly gates `feature_ready` (`feature_ready = all(required[col] is not
None for col in REQUIRED_FEATURE_COLUMNS)`, `feature_engine.py:1409-1410`).
I put `ema150` in `OPTIONAL_FEATURE_COLUMNS` instead, for two reasons:
1. **Precedent** — every genuinely new dormant field added since v02
   (`rs_percentile_126d`, `market_cap`, `vcp_sequence_score`) went into
   `OPTIONAL_FEATURE_COLUMNS`, not `REQUIRED_FEATURE_COLUMNS`. `ema150` is
   the same kind of addition: landed, not yet wired to anything.
2. **No functional reason to require it** — nothing reads `ema150` yet, so
   making it a `feature_ready` precondition would only make an otherwise-live
   ticker "not ready" for a field nobody consumes. (In practice this is moot
   for the 150-vs-200-bar boundary specifically, since `ema200` already
   requires 200 bars and thus already implies 150 — but the precedent and
   the "don't couple readiness to an unread field" reasoning both point the
   same direction regardless.)

Also, per the note's own framing ("the 150-day EMA or SMA... use SMA only if
there's a specific reason to deviate"): used EMA (`ema150`), matching
`ema20`/`ema50`/`ema200`'s existing convention exactly, no deviation. Landed
as a bare field only — no `distance_to_ema150_pct` companion — since the
note asked specifically for "the 150-day EMA or SMA," and the three existing
dormant fields are each a single field, not a field-plus-companion pair.

### What was NOT touched (per instruction)

- `validate_trend_continuation` / any other validator — `ema150` is read
  nowhere.
- `relative_strength_vs_spy`, the breakout validator, or the trend_continuation
  soft-scoring weight — untouched, still paused pending Item 1's answer.

### Testing

**New tests** (`tests/test_feature_engine.py`):
- `test_ema_roc_rvol_exact` extended with an `ema150` assertion against the
  same `_ema(closes, span)` reference helper already used for
  `ema20`/`ema50`/`ema200` (300-bar series, exact formula match, `rel=1e-9`).
- `test_ema_alignment_score_values` extended with `ema50 > ema150 > ema200`
  in a steady uptrend (sanity-check ordering; `ema150` isn't itself part of
  `ema_alignment_score`'s formula and wasn't added to it).
- `test_ema150_null_below_minimum_bars` (new) — 149 bars → `ema150 is None`.
- `test_ema150_present_at_minimum_bars` (new) — exactly 150 bars → `ema150`
  populated and matches the reference EWM formula at the boundary bar itself.
- `test_ema150_dormant_not_in_required_columns` (new) — pins
  `"ema150" not in REQUIRED_FEATURE_COLUMNS` and
  `"ema150" in OPTIONAL_FEATURE_COLUMNS`.

**`FEATURE_SCHEMA_VERSION` bump — pinned tests found and updated (6 files):**
version-string literals don't auto-update, so every test asserting the exact
current version needed a matching edit. Found via `grep -r "features_v04"
tests/`:

| File | Change |
|---|---|
| `tests/test_schema_manager.py` | Added `FEATURES_V05_COLUMNS = frozenset({"ema150"})`; new `test_features_v05_columns_exist` (parametrized prod/debug, mirrors the existing v04 test); `test_feature_schema_version_column_exists` assertion bumped to `"features_v05"`. |
| `tests/test_feature_engine_v02.py` | `test_feature_schema_version_is_current` now asserts against `constants.FEATURE_SCHEMA_VERSION` (already imported) instead of a hardcoded string, pinned `== "features_v05"`. |
| `tests/test_feature_engine_v03.py` | `test_new_rows_are_features_v03` bumped literal to `"features_v05"` (already compared against `constants.FEATURE_SCHEMA_VERSION` too). |
| `tests/test_p2_4_shares_market_cap.py` | `test_rows_are_features_v04` renamed to `test_rows_are_current_feature_schema_version`, rewritten to assert against `constants.FEATURE_SCHEMA_VERSION` (added the missing `from app.config import constants` import) instead of a literal — future bumps won't need to touch this test again. |
| `tests/test_step3_universal_eligibility.py` | `test_constants_feature_schema_version` literal bumped to `"features_v05"`. |
| `app/services/screening/m14_setup_validators.py` | Docstring example string only (not a test), fixed for accuracy. |

Three of the five test-file edits (`test_feature_engine_v02.py`,
`test_feature_engine_v03.py`, and now `test_p2_4_shares_market_cap.py`) now
assert against the live `constants.FEATURE_SCHEMA_VERSION` rather than a
hardcoded string where that was easy to do without changing what the test is
actually proving — reduces (doesn't eliminate) how many tests need a manual
edit on the *next* schema bump. `test_schema_manager.py` and
`test_step3_universal_eligibility.py` keep an explicit literal by design
(they're specifically pinning "the version is what we think it is," so a
literal is the correct assertion, not a shortcut).

### Results

```
pytest tests/test_feature_engine.py -v
  41 passed in 74.33s
```

```
pytest tests/test_feature_engine.py tests/test_feature_engine_v02.py \
       tests/test_feature_engine_v03.py tests/test_p2_3_vcp_sequencing.py \
       tests/test_p2_4_shares_market_cap.py tests/test_schema_manager.py \
       tests/test_step3_universal_eligibility.py tests/test_m14_setup_validators.py -v
  541 passed in 257.66s
```

Before these fixes, the same run surfaced exactly the 6 version-pin failures
listed above (confirmed via an initial background run) — all six are now
fixed and green; no other test in this scope was affected.

---

## Anomalies (verbatim)

- `CLAUDE.md` item 1's three named metrics
  (`false_breakout_rate`/`target_hit_rate`/`pullback_failure_rate`) do not
  correspond to any computed field, column, or report output anywhere in the
  current codebase — not even under different names in the funnel
  diagnostics module. If this precondition is meant to gate the breakout
  `relative_strength_score` work indefinitely until literally these three
  named rates exist, that's a real, currently-unstarted piece of work (an
  M16/M17 outcome-queue-based report), distinct from both the funnel
  diagnostics module and the simulation/config-recommender's
  `target_hit_rate`.
- `app/services/debug/debug_mode.py` still carries its own independent,
  already-stale `STEP_NAMES` copy (flagged in the prior insider-flag
  decoupling report) — unrelated to this note, not touched, still open.

## No commit

Diffs delivered as described above; no commit made, per current policy.
