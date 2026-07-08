# Phase 3 — AI Review Orchestration Upgrade: Design Note

## Frozen-module gate — resolved, no sign-off needed

M19 is **not** on CLAUDE.md's frozen-modules list (only M02/M04/M05/M06/M07/M08/M09/M10/M12 + `trading_calendar.py`/`service_result.py` are frozen). The note's own gate condition ("if this needs explicit sign-off before touching a currently-frozen M19, flag it") therefore doesn't trigger — proceeded without pausing.

## Where the 3 rows get created: M18, not M19 (as the note anticipated)

`ExportPackageEngine._write_ticker_review_rows` / `_write_sim_review_rows` now write 1 row (legacy, `review_kind=NULL`) or 3 rows (multi-pass) in a single transaction, driven by a new `ai_review_config` parameter on `export_ticker_review` / `export_simulation_review` (optional, defaults to `default_configs.DEFAULT_RUNTIME_CONFIGS["ai_review"]`, which has `multi_pass.enabled=False` — so omitting the parameter reproduces pre-Phase-3 behavior exactly, byte-for-byte). M19's `_send` flow required **no logic change** for the double-send guard — each `review_kind` is already its own row with its own `ai_review_id`, and `_send` already operates strictly per-row. Only M19's read/metadata surface grew (`review_kind` added to the SELECT and to `SEND_METADATA_KEYS`).

## Step 5 formula integration — exact mechanics

**Important correction to the note's premise:** the "existing four-term formula" is stale — a stop-distance-quality term was added in a prior session (2026-06-27 diagnostics fix) without updating `01c_FORMULAS_AND_CONFIGS.md`. The actual base is 5 terms (`0.40/0.25/0.15/0.10/0.10` for setup/RR/confirmation/market/stop-distance). Fixed the spec drift as part of this delta (both changes documented together, per the note's own instruction to treat formula changes as reviewed, not silent).

`contrarian_risk_score` / `audit_consistency_score` are **additive downgrade-only penalties** on top of the base, applied only when present:
```
proposal_score_raw = base
                   - contrarian_penalty_weight * contrarian_risk_score        (if present)
                   - audit_penalty_weight * (100 - audit_consistency_score)   (if present)
```
Both penalty weights default to 0.10, config-overridable via a new `risk_label_config.ai_review` block (`contrarian_penalty_weight`, `audit_penalty_weight`, `audit_consistency_min_for_buy` — default 40). When both scores are `None` (the overwhelming majority of proposals), the output is **byte-identical** to the pre-Phase-3 formula — verified by a direct equality test, not just an approximate one.

A separate **hard gate** (distinct from the score penalty): `audit_consistency_score < audit_consistency_min_for_buy` forces `disposition = WATCHLIST_ONLY` outright (`rejection_reason = "audit_consistency_below_threshold"`), independent of the RR/risk-label/resistance checks — same pattern as the existing `resistance_blocks` override.

**A real architectural gap surfaced and how it's resolved:** Step 5 proposals are created *before* AI review happens (M18 exports already-written `step5_proposals` rows; M19 sends them even later, on a human's explicit trigger). So `_build_rows`/`propose()` cannot consume contrarian/audit scores at their *original* creation time — there's no live data yet. Resolved by making `ai_review_scores` an **optional pass-through parameter** on both `_build_rows` and the public `propose()` (keyed by ticker), consumed only when a caller already has it (e.g., has read and parsed the relevant `ai_reviews` rows itself via `parse_audit_response`/`parse_contrarian_response`). **Not built:** the orchestration that reads `ai_reviews`, correlates rows to proposals by ticker, and re-invokes `propose()`/`_build_rows` with the result — that's real new pipeline wiring (timing, re-run semantics, whether it's a fresh `propose()` call or an in-place update), materially larger than "small, additive," and the note's own scope explicitly defers comparable wiring elsewhere (M23's scheduling, M21's dashboard surfacing). Flagging this as the follow-up decision needed before contrarian/audit scores can actually reach a live proposal.

## Audit threshold configuration — exact location

`risk_label_config.ai_review.audit_consistency_min_for_buy` (default 40.0), parsed in `_parse_risk_label_config` via an explicit `None`-check (not `value or default`) so a deliberate `0` (e.g. "disable this gate") is honored rather than silently replaced — this pattern is applied to all three new `ai_review` config values, tested explicitly.

## Structured audit/contrarian output format

Defined and implemented (not just described) in `ai_review_engine.py`:
- **Audit:** `{"claims": [{"claim": str, "classification": "grounded"|"speculative"|"unverifiable"}, ...]}` → `parse_audit_response()` → `{grounded, speculative, unverifiable, total, audit_consistency_score}` (`100 * grounded / total`).
- **Contrarian:** `{"risk_score": 0-100, "concerns": [...]}` → `parse_contrarian_response()` → `{risk_score, concerns}` (clamped to `[0, 100]`).
- Both are pure, no-I/O functions. A malformed/unparseable response returns `None` (treated as "absent," not a hard penalty) rather than raising or defaulting to a worst-case score — an AI response that failed to follow the requested format isn't itself evidence of a bad thesis.

## Bug fixed along the way (not part of the original ask, discovered while touching the exact code)

`_read_review_row`'s sim-table branch selected `review_type` from `sim_ai_reviews`, a column that table has never had (confirmed against the DDL). This would raise `BinderException` against a real DuckDB connection — never caught because M19's test suite is entirely offline with a fake connection that ignores the actual SQL column list. Fixed while adding `review_kind` to the same query (same lines, same PR). Added a regression test (`test_sim_select_does_not_reference_review_type_column`) plus fixed the test fixture itself (`_ROW_COLUMNS` was missing `review_kind` entirely, meaning `row.get("review_kind")` was silently always `None` in every existing test before this session).

## What shipped

- Schema: `review_kind VARCHAR` added to `ai_reviews` and `sim_ai_reviews` (schema-manager pattern, nullable, no NOT NULL — legacy rows/exports are `NULL`).
- `app/services/export/export_package_engine.py`: `ai_review_config` param on both export methods; `_write_ticker_review_rows`/`_write_sim_review_rows` (renamed from singular, now transactional multi-row); new `ai_review_rows` metadata key (always a list).
- `app/services/config/default_configs.py`: `ai_review.multi_pass` block added (disabled by default).
- `app/services/ai_review/ai_review_engine.py`: `review_kind` threaded through read/metadata; `parse_audit_response`/`parse_contrarian_response` pure functions; sim-table `review_type` bug fixed.
- `app/services/proposal/step5_proposal_engine.py`: `_proposal_score_raw` gains 4 new optional params (2 scores + 2 configurable weights); `_parse_risk_label_config` gains the `ai_review` block; `_build_rows`/`propose()` gain `ai_review_scores`; new `WATCHLIST_AUDIT_INCONSISTENT` hard-gate constant.
- `specs/01c_FORMULAS_AND_CONFIGS.md`, `specs/M19_AI_REVIEW_ENGINE_SPEC.md`, `specs/M18_EXPORT_PACKAGE_ENGINE_SPEC.md`: updated (01c also fixes the pre-existing stop-distance-term drift).

## Testing

New tests: 6 in `test_export_package_engine.py` (legacy/multi-pass/disabled/rollback-atomicity, ticker + sim), 23 in `test_ai_review_engine.py` (review_kind metadata, per-row double-send independence, the sim bug-fix regression, 13 parser edge cases), 12 in `test_step5_proposal_engine.py` (back-compat byte-identity, penalty-only-lowers, config override incl. explicit-zero, end-to-end disposition downgrade + per-ticker scoping), 4 in `test_schema_manager.py` (column presence + idempotency). All existing suites for every touched module (M15/M17/M18/M19/M23/schema) confirmed green, plus a full repo-wide run.

## Exit criterion status

Three distinct `review_kind` rows created and independently sendable per proposal/sim export (verified); contrarian resolves to a different provider than thesis (verified, config-driven not hardcoded); audit pass produces structured, thresholdable output (verified, both clean and high-unverifiable fixtures); Step 5 scoring consumes both new scores without breaking existing formula behavior for reviews that haven't run all three passes (verified byte-identical when absent). Full suite green.
