# Claude Coding Prompt — Module 15: Step 5 Proposal Engine

Use Project Instructions and Project Files.
Use `00_PROJECT_FILE_MAP.md` for targeted retrieval only.
Do not repeat or summarize global rules.
Work silently and return only actionable results.

## Attached

1. `stock_ai_platform_module14_stable.zip`

Current codebase:
Modules 01–14 are accepted and frozen. Use the zip as the implementation base.

## Task

Implement only **Module 15 — Step 5 Proposal Engine**.

Module 15 reads Step 4 analyses, Step 3 screening scores, and ticker sector/industry data; computes proposal scores; applies raw and diversified ranking; writes one append-only row per analyzable candidate into `step5_proposals`; and returns `ServiceResult`.

Do not implement Module 16 or later.

No separate Module 15 spec is provided. Create `M15_STEP5_PROPOSAL_ENGINE_SPEC.md` from Project Files, frozen Module 14 style, and this prompt.

## Source retrieval hints

Retrieve only what is needed:

- Step 5 scoring and diversification formulas → `01c_FORMULAS_AND_CONFIGS.md`
- `step5_proposals`, `step4_analysis`, `step3_candidates`, `ticker_master` schema → `01b_SCHEMA_AND_DATA.md`
- raw/diversified ranking and hard-cap/no-soft-penalty decisions → `02b_ARCHITECTURE_DECISIONS.md`
- Module 15 pipeline position → `01d_MODULES_AND_PIPELINE.md`
- implementation style / guard / transaction patterns → frozen Module 14 code

## Public API

Implement:

```python
class Step5ProposalEngine:
    def __init__(self, db_manager=None) -> None: ...

    def propose(
        self,
        signal_date: date,
        strategy_config: dict,
        strategy_config_id: str,
        db_role: str = "prod",
        run_id: str | None = None,
    ) -> ServiceResult: ...
```

`run_id` is minted when `None`.

## Scope and write ownership

Expected additions:

```text
app/services/proposal/__init__.py
app/services/proposal/step5_proposal_engine.py
tests/test_step5_proposal_engine.py
M15_STEP5_PROPOSAL_ENGINE_SPEC.md
README.md                          # short Module 15 note only, if project convention
```

Do not modify frozen Modules 01–14 unless required by a failing test or real integration blocker.

Module 15 writes only `step5_proposals` via INSERT. No UPDATE, DELETE, DDL, cleanup, providers, network, direct `duckdb`, `ATTACH`, or `print()`.

Reruns are append-only: a new `run_id` may create new proposal rows.

## Config validation

Validate before DB access. On failure return `failed` with zero counts.

Required paths:

- `diversification.hard_cap_enabled` — bool
- `diversification.top_n` — int > 0
- hard-cap mode:
  - `diversification.max_sector_count` — int >= 1
  - `diversification.max_industry_count` — int >= 1
- soft-penalty mode:
  - `diversification.sector_penalty` — float, `0 < x <= 1`
  - `diversification.industry_penalty` — float, `0 < x <= 1`

`db_role`: allow only `prod` and `debug`; reject `simulation` or other values before DB access.

## Read inputs

For `signal_date` and `strategy_config_id`, read:

- `step4_analysis`: `analysis_id`, `candidate_id`, `ticker`, `setup_score`, `timing_score`, `estimated_rr`;
- `step3_candidates`: `screening_score`;
- `ticker_master`: `sector`, `industry`.

Use the DB manager. Read phase is read-only.

Missing / NULL behavior:

- NULL `setup_score` or `screening_score` → not analyzable; count in `analyses_read`, skip proposal write.
- NULL `timing_score` → 50.0.
- NULL `estimated_rr` → `rr_score = 0` and sorts lowest for RR tie-break.
- NULL sector / industry → `__UNKNOWN_SECTOR__` / `__UNKNOWN_INDUSTRY__`.

## Scoring and raw ranking

Use Project Files formulas.

```text
proposal_score_raw =
  0.40*setup_score + 0.25*screening_score + 0.20*rr_score + 0.15*timing_score
```

`rr_score`:

```text
100 if estimated_rr >= 3.0
80  if 2.2 <= estimated_rr < 3.0
60  if 1.8 <= estimated_rr < 2.2
0   otherwise or if NULL
```

Clamp raw and final scores to `[0, 100]`.

Raw ranking:

```text
proposal_score_raw DESC
estimated_rr DESC, with NULL lowest
ticker ASC
```

Assign 1-based `raw_rank`; `in_raw_top_n = raw_rank <= top_n`.

## Diversified ranking

### Hard-cap mode

Process candidates in `raw_rank` order.

If adding a candidate would exceed sector or industry cap:

- still insert a proposal row;
- `diversified_rank = NULL`;
- `in_diversified_top_n = False`;
- `selected_flag = False`;
- `proposal_score_final = proposal_score_raw`;
- no soft penalty;
- `rejection_reason = "sector_cap"` or `"industry_cap"`;
- if both caps fail, use `"sector_cap"`.

If accepted:

- assign next sequential `diversified_rank`;
- `proposal_score_final = proposal_score_raw`;
- `rejection_reason = NULL`.

### Soft-penalty mode

No hard rejection. Process in `raw_rank` order and apply prior-count penalties:

```text
sector_multiplier   = sector_penalty ** max(0, prior_sector_count)
industry_multiplier = industry_penalty ** max(0, prior_industry_count)
proposal_score_final = proposal_score_raw * sector_multiplier * industry_multiplier
```

Then sort by:

```text
proposal_score_final DESC
ticker ASC
```

Assign `diversified_rank`. `rejection_reason = NULL` for all rows.

### Shared semantics

For both modes:

```text
in_diversified_top_n = diversified_rank is not NULL and diversified_rank <= top_n
selected_flag = in_diversified_top_n
selected_top_n = in_raw_top_n OR in_diversified_top_n
```

Write one proposal row for every analyzable Step 4 row, including hard-cap rejected rows.

## ServiceResult metadata

Exact metadata keys on every return path:

```text
db_role
signal_date
strategy_config_id
run_id
analyses_read
proposals_written
raw_top_n_count
diversified_top_n_count
hard_cap_rejections
```

`rows_processed == proposals_written`.

On guard/config failure before DB access, all count metadata is `0`.

On write failure, return `failed`, rollback, `proposals_written = 0`, `rows_processed = 0`, while preserving actual `analyses_read`.

## Transaction model

Use read → compute → single write transaction.

All proposal inserts occur in one transaction. On any write error, rollback so no partial proposals survive.

## Required tests

Create `tests/test_step5_proposal_engine.py`, fully offline with temp DB paths.

Cover:

- public API, `run_id`, exact metadata keys, `rows_processed == proposals_written`;
- `db_role` guards and all config validation before DB access;
- empty input;
- NULL handling for setup/screening/timing/RR/sector/industry;
- raw score formula, RR tiers/boundaries, clamping, raw ranking tie-breaks, `in_raw_top_n`;
- hard-cap accept/reject behavior, both-caps priority, rejected rows still inserted with `diversified_rank = NULL`;
- soft-penalty multiplier formula using prior raw-rank counts, final ranking and ticker tie-break;
- `selected_flag` and `selected_top_n` semantics;
- append-only behavior and `uuid4` proposal IDs;
- `candidate_id` and `analysis_id` preserved;
- write ownership: only `step5_proposals` inserted;
- rollback leaves no partial proposals;
- static scans: no direct `duckdb`, provider import, network, `print`, DDL/ATTACH/UPDATE/DELETE; INSERT target is only `step5_proposals`.

Existing Module 01–14 tests must pass unchanged.

## Module-specific source of truth

Create `M15_STEP5_PROPOSAL_ENGINE_SPEC.md` documenting:

- public API and metadata keys;
- input joins and NULL handling;
- scoring formulas and clamps;
- raw ranking;
- hard-cap and soft-penalty diversification;
- `selected_flag` / `selected_top_n`;
- config validation;
- append-only rerun assumption;
- transaction model;
- tests;
- assumptions/open gaps, especially soft-penalty multiplier and unknown sector/industry buckets.

Do not invent architecture or override higher-priority Project Files.

## Output

Follow Project Instructions. Return only actionable results:

- updated zip;
- added / changed files;
- `M15_STEP5_PROPOSAL_ENGINE_SPEC.md`;
- short design notes;
- test commands and results;
- assumptions/open gaps;
- suggested commit message: `module15_step5_proposal_engine_stable`.
