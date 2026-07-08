# Note to Architect — Phases 0–5 Summary (for comparison against original CODER_NOTEs)

Covers everything executed in this working session, in commit order. Each
phase lists what was asked, what shipped, and — most importantly for
review — every deviation, judgment call, or unresolved gap that was
flagged rather than silently resolved. Commit hashes are on `main`.

---

## Phase 0 — `05b364c phase0_point_in_time_audit_stable`

**Ask:** Audit M11 (FeatureEngine) / M12 (MarketRegimeEngine) for look-ahead
leaks on non-price joins.

**Delivered:**
- **Real leak found and fixed**: the `earnings_calendar` SELECT in
  `feature_engine.py` had no `updated_at` filter, so a record *published*
  after `signal_date` could still be joined into that day's features.
  Fixed with `AND CAST(updated_at AS DATE) <= ?`, threaded through
  `_read_earnings(cutoff_date)`.
- Benchmark joins (M11 sector RS, M12 regime) confirmed already asof-safe
  (bounded by `date <= end_date`) — verified, not assumed.
- `ticker_master.sector` documented as **asof-blind** (no versioned sector
  history in the schema) — **flagged as a known, accepted, deferred
  limitation**, not fixed. Risk judged low (sector reassignment is rare).
- New `tests/test_point_in_time_integrity.py` (6 tests: 2 failed
  pre-fix/pass post-fix on the earnings leak, 2 asof-safe confirmations,
  1 documents the sector limitation).

**Open item for architect:** the `ticker_master.sector` gap is still
unresolved — closing it needs either a `sector_history` table or migrating
sector lookup onto `ticker_universe_snapshot`'s `snapshot_month` key. Not
attempted; accepted as low-risk.

---

## Phase 1 — `df9d4a8 phase1_replay_engine_stable`

**Ask:** Implement `SimulationEngine._replay_date` (M17) for real, replacing
a placeholder guard, so simulation sweeps can replay historical data across
`(setup_config_id, risk_label_config_id)` variants.

**Delivered:**
- Step 3 runs once per `sim_date`, shared across all variants; Step 4/5
  reuse the **pure functions directly**
  (`step3_universal_eligibility`, `m14_setup_validators.validate_setup`,
  `Step5ProposalEngine._build_rows`) — never their `.run()`/`.propose()`
  I/O wrappers — per the accepted `M17_SIMULATION_ENGINE_CONFIG_DELTA.md`.
- Added an embargo window (default 40 business days) excluding
  train-window signals near a walk-forward fold's test boundary, plus a
  swappable fold-planner constructor seam for future strategies.
- **Fixed pre-existing schema-parity bugs** the old stub never exercised:
  `sim_step4_analysis`/`sim_step5_proposals`/`sim_config_comparisons` were
  missing already-computed columns (`entry_price_raw`, `risk_score`, etc.)
  and had a `NOT NULL setup_type` violation waiting to happen the first
  time real data flowed through.
- Updated two `test_phase7_setup_mode.py` tests that had encoded the old
  stub's placeholder behavior as if it were correct.

**Prior context (from before this session, per memory):** the note that
triggered this had earlier been re-scoped once already (an initial
"N+1 query fix" framing turned out to have a stale premise — the real ask
was the replay engine itself).

---

## Phase 1.5 — `35e7953 phase1_5_preset_config_seed_stable`

**Ask:** Seed literature-anchored preset `setup_configs` so Phase 1's
replay engine has a defensible variant space instead of an arbitrary grid.

**Delivered:**
- 6 presets: canonical/strict breakout, strict `consolidation_base`,
  template `trend_continuation`, shallow/fib pullback.
- Always inserted **inactive** (`active_flag=FALSE`) via new
  `ConfigService.seed_preset_setup_configs`, same `ON CONFLICT DO NOTHING`
  idempotency as the existing v1 seeder — never touches the
  one-active-per-`setup_type` invariant.
- Every validation field name cross-checked against what
  `m14_setup_validators.py` actually reads — no invented fields.

**Flagged, not silently faked:** two criteria from the original request had
**no corresponding field or feature** in the current validators/schema —
breakout's RS filter, and `trend_continuation`'s "RS as a hard gate" +
150-day MA alignment. Both are documented in the presets as
**approximated, not implemented** rather than faked with a stand-in field.

**Open item for architect:** decide whether those two criteria are worth
adding new features/fields for, or whether the approximation is acceptable
long-term.

---

## Phase 2 — `23779ca phase2_config_recommender_stable`

**Ask:** New learning-layer module (M23) that recommends config changes
from realized outcomes, for human review — never auto-activating anything.

**Delivered:**
- `ConfigRecommenderService` aggregates realized outcomes from prod
  `signal_outcomes` and simulation `sim_signal_outcomes`, grouped by
  `(setup_type, regime, config_id)`, writes proposals to a new
  `config_recommendations` table.
- Reuses `simulation_engine.compute_metrics` for expectancy/win_rate/
  profit_factor rather than reimplementing it.
- **Statistical guardrails** against false positives in this
  multiple-comparison setting: a flat 30-sample floor on both candidate and
  incumbent, and a required improvement margin of ≥1 pooled standard error
  of the difference in sample means — a simple, stated heuristic, **not** a
  full Deflated Sharpe Ratio treatment (explicitly scoped down, documented
  as such rather than silently simplified).
- **Never activates a config** — verified with an AST-based static scan
  proving there is no code path reaching `activate_setup_config` (reads
  `setup_configs` via this module's own local SQL, never imports
  `ConfigService`) — a structural guarantee, not just a convention.

**No open items flagged for this phase.**

---

## Phase 3 — `2e39884 phase3_ai_review_multipass_stable`

**Ask:** Upgrade M19 AI review from one send/response overlay per export to
three distinct passes (thesis / contrarian / audit).

**Delivered:**
- Three `review_kind` rows (`thesis`/`contrarian`/`audit`) per export
  instead of one, via a new `review_kind` column. Row creation is M18's job
  (`ai_review_config` param, **disabled by default** — existing callers get
  byte-identical pre-Phase-3 single-row behavior).
- Contrarian pass resolves to a **different provider than thesis**, via
  config — never hardcoded.
- Audit pass returns structured JSON parsed by a new pure
  `parse_audit_response()` → grounded/speculative/unverifiable counts +
  thresholdable `audit_consistency_score`; `parse_contrarian_response()`
  mirrors this for a 0-100 risk score. Malformed AI output degrades to
  "absent" (not a penalty) — an AI response that failed to follow the
  requested format isn't itself evidence of a bad thesis.
- Step 5 scoring gains both scores as **additive, downgrade-only**
  penalties, byte-identical when absent (verified with an exact-equality
  test, not an approximation), plus a hard
  `audit_consistency_min_for_buy` disposition gate.
- **Fixed two things along the way, not part of the original ask:**
  (a) a stale spec/code drift in `01c_FORMULAS_AND_CONFIGS.md` — the
  documented proposal-score formula was missing a stop-distance term added
  in an earlier session; corrected alongside the new terms since formula
  changes are reviewed, not silent, per project rule.
  (b) a real pre-existing bug: the sim-table read path selected a
  `review_type` column `sim_ai_reviews` has never had — would raise against
  a real DuckDB connection; never caught because M19's test suite was
  entirely offline with a fake connection that ignored the actual SQL.

**Open item for architect (real architectural gap, explicitly flagged, not
closed):** Step 5 proposals are created *before* AI review runs (M18 exports
already-written proposals; M19 sends them later, on a human trigger). So
`propose()`/`_build_rows()` gained an optional `ai_review_scores` **pass-
through** parameter, but **no orchestration exists** that reads
`ai_reviews`, correlates to proposals by ticker, and re-invokes scoring with
the result. Judged larger than "small, additive" scope — needs an explicit
decision on timing/re-run semantics before it can go live.

---

## Phase 4 — `66dd389 phase4_fundamentals_events_layer_stable`

**Ask:** Compact fundamentals/events layer — 7 fields (EPS growth trend,
leverage ratio, valuation band, Piotroski F-Score, Altman Z-Score,
insider-trade flag, institutional-ownership delta) from Finnhub + SEC EDGAR,
mirroring the earnings ingestion pattern; plus a second OHLCV provider.

**Delivered:**
- New `FundamentalSnapshot` DTO + `get_fundamentals` capability on M04
  (additive, concrete-default method — **zero changes** to frozen M05
  `YahooProvider`, verified directly).
- New `ticker_fundamentals` companion table (prod/debug only) —
  **schema decision**: companion table over new `daily_features` columns,
  since fundamentals are quarterly-cadence and `daily_features` is a
  high-traffic daily table. `FEATURE_SCHEMA_VERSION` **not bumped**
  (confirmed, not assumed).
- New `fundamentals_refresh` pipeline step, mirroring `_step_earnings`
  exactly (same already-refreshed-today guard, same per-ticker-failure-is-
  a-warning semantics).
- Step 4 and Step 5 gained **optional, config-weighted** scoring
  adjustments from fundamentals — **disabled by default**, never a hard
  gate (verified byte-identical to prior behavior across the full
  pre-existing test suites when the new config keys are absent). Step 3
  **deliberately untouched**, mirroring the RVOL precedent (RVOL is
  setup-specific only, never a universal Step 3 gate).
- Dedicated Phase 0-style leak-test suite for all 7 fields
  (`test_fundamentals_point_in_time.py`).

**Reversed the note's own stated approach — flagged, not silent:**
- **EDGAR chosen as primary source, not Finnhub** — Finnhub's live free-tier
  behavior couldn't be verified from this environment; SEC EDGAR's XBRL API
  is free, keyless, and fully documented. No Finnhub client was built.
- **Altman Z-Score uses the Z'-Score variant** (book value of equity, not
  market value) so the provider needs no price-feed dependency — a
  standard alternate formulation, not invented, but different from the
  "classic" public-company Z-Score.
- **Piotroski F-Score computed on 8 of the standard 9 signals** — the
  share-issuance signal was dropped (its XBRL tag is too filer-inconsistent
  to trust), score scaled to preserve the 0-9 range.
- **"Second OHLCV provider (yfinance fallback)" reinterpreted as Stooq** —
  `YahooProvider` already *is* the yfinance-backed provider, so a literal
  "yfinance fallback for yfinance" would be circular.

**Blocking gaps — explicitly flagged rather than substituted, per the
note's own instruction:**
- **`insider_trade_flag`: always `None`.** SEC EDGAR Form 4s are filed
  under the insider's own CIK, not the issuer's, so the issuer's own
  submissions feed can't surface them without EDGAR full-text search
  (not attempted — confidence too low to build against undocumented query
  semantics).
- **`institutional_ownership_delta`: always `None`.** True institutional
  ownership needs 13F aggregation across *all* institutional filers — a
  substantial data-engineering project, not a per-ticker API call.

**Open item for architect (same shape as Phase 3's gap):** Step 5's
`fundamentals_scores` is also a pass-through-only parameter — no
orchestration reads `ticker_fundamentals`, computes a quality score, and
calls `propose(fundamentals_scores=...)` automatically. (Step 4, by
contrast, *does* read `ticker_fundamentals` itself — this is a deliberate
asymmetry, documented, to avoid coupling Step 5 to Step 4's opt-in state.)

---

## Phase 5 — `edc2b29 phase5_ops_scheduling_run_id_stable`

**Ask:** (1) Move `run_prod_pipeline.py` to a real Windows Task Scheduler
job. (2) Audit that a single `run_id` correlates `pipeline_runs`, step
logging, `pipeline_run_diagnostics`, and step-engine `ServiceResult`s
end-to-end.

**Delivered:**
- **No code changes to `run_prod_pipeline.py` or `pipeline_orchestrator.py`
  at all** — both tasks turned out to be pure verification.
- Task 1: delivered `ops/StockAIPlatform_DailyPipeline.xml` (importable
  Task Scheduler definition) + `ops/README_task_scheduler_setup.md`.
  Daily at 23:30 local time (user's explicit choice), account
  `DESKTOP-10TI0F5\kiman`, runs `--run-type scheduled` with no `--date`
  (defaults to today) and no `--force-rerun`/`--resume-from`.
- Task 2: **audited every `_step_*` method and every engine it calls** —
  confirmed the orchestrator's `run_id` is threaded correctly everywhere
  already (via the pre-existing codebase-wide
  `run_id = run_id if run_id is not None else str(uuid.uuid4())`
  convention). Added one regression test
  (`TestRunIdCorrelation`) proving it end-to-end; it passed on the first
  run — confirming there was nothing to fix.

**Open item for architect — this is real, not a formality:** the Task
Scheduler task has **not actually been registered on the machine**.
Creating/importing it (and entering the account password for "run whether
logged on or not") was deliberately left as a manual follow-up action,
since it's a real OS-state change requiring the user's presence. **The
pipeline is not yet running on a schedule** — only the artifact to do so
exists.

---

## Cross-phase patterns worth the architect's attention

1. **Two "pass-through, not wired" gaps, same shape, both explicitly
   flagged rather than silently left implicit:** Phase 3's
   `ai_review_scores` and Phase 4's `fundamentals_scores` on
   `Step5ProposalEngine`. Both let a caller supply pre-computed scores, but
   neither has the orchestration that reads the underlying data, computes
   the score, and calls `propose()` with it automatically. If either is
   meant to be live before the next phase, that orchestration is the
   remaining work — not a Step 5 code change.
2. **Two permanently-`None` fundamentals fields** (`insider_trade_flag`,
   `institutional_ownership_delta`) and **one deferred schema limitation**
   (`ticker_master.sector` asof-blindness) are the three concrete "blocking
   issue, not silently substituted" items across all six phases.
3. **The Task Scheduler task is not registered yet** — Phase 5's ops
   deliverable exists as a file, not as running infrastructure.
4. Every phase's full test suite was run repo-wide before commit; the only
   consistently-recurring failures across all runs are the two known
   pre-existing `test_data_validator.py`/`test_mutation_detector.py`
   spec-file-path issues (missing `M09_DATA_VALIDATOR_SPEC.md`/
   `M10_MUTATION_DETECTOR_SPEC.md` at the project root) — unrelated to any
   of this work, present before Phase 0 and never touched.
