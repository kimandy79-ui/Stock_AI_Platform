# M15 — Step 5 Proposal Engine — Module Spec

Module-specific source of truth for Module 15. Derived from the frozen split
Project Files and the frozen Module 14 style. Where this spec and a
higher-priority Project File disagree, the Project File wins; this spec only
fills gaps the Project Files leave open.

## Purpose & pipeline position

Module 15 runs after Module 14 (Step 4 Setup Analysis) and before Module 16. It
reads the Step 4 analyses for one `signal_date` / `strategy_config_id`, joins
each to its Step 3 screening score and its ticker's sector / industry, scores and
raw-ranks every analyzable analysis, applies diversification (hard-cap or
soft-penalty), and appends one `step5_proposals` row per analyzable analysis in a
single transaction. (`01d_MODULES_AND_PIPELINE.md`; `AD-22.11`.)

## Public API

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

- `db_manager` defaults to `app.database.duckdb_manager`; a fake/wrapper may be
  injected for tests.
- `run_id` is minted (`uuid4`) when `None`; a supplied value is preserved.
- Returns `ServiceResult` with `rows_processed == metadata["proposals_written"]`
  on every return path.
- Package-level import available: `from app.services.proposal import Step5ProposalEngine`.

## Config-key naming (canonical vs legacy)

The canonical internal key names (used throughout this spec and the engine
internals) are:

| Section | Canonical key | Legacy alias (01c example blocks) |
|---|---|---|
| hard-cap | `max_sector_count` | `sector_max_positions` |
| hard-cap | `max_industry_count` | `industry_max_positions` |
| soft-penalty | `sector_penalty` | `sector_penalty_factor` |
| soft-penalty | `industry_penalty` | `industry_penalty_factor` |

`_normalise_diversification_block` transparently rewrites legacy names to canonical
before validation. Both naming conventions are accepted. If a config block contains
**both** a legacy name and its canonical equivalent for the same key (e.g.
`sector_max_positions` and `max_sector_count` together), `_ConfigError` is raised
before any DB access — this is treated as an ambiguous misconfiguration rather than
silently accepted. All other keys (`hard_cap_enabled`, `top_n`) have no legacy
alias.

## ServiceResult metadata (exact keys, every path)

```
db_role
signal_date                 # ISO string
strategy_config_id
run_id
analyses_read               # all step4_analysis rows read (analyzable or not)
proposals_written           # == rows_processed
raw_top_n_count             # rows with in_raw_top_n
diversified_top_n_count     # rows with in_diversified_top_n
hard_cap_rejections         # rows with a non-NULL rejection_reason
```

Exact set of 9 keys on every return path (guard failure, config failure, read
failure, write failure, empty, success). On guard / config failure all counts are
`0`. On write failure: `failed`, rollback, `proposals_written = 0`,
`rows_processed = 0`, `analyses_read` preserves the real read count.

## Read inputs & joins

For the given `signal_date` and `strategy_config_id`, one row per
`step4_analysis` row is read, `LEFT JOIN`ed to:

- `step3_candidates` on `candidate_id` → `screening_score`;
- `ticker_master` on `ticker` → `sector`, `industry`.

Selected fields: `analysis_id`, `candidate_id`, `ticker`, `setup_score`,
`timing_score`, `estimated_rr`, `screening_score`, `sector`, `industry`. The read
connection is opened read-only and closed before any computation.

### Analyzable filter & NULL handling

- NULL `setup_score` **or** NULL `screening_score` → not analyzable: counted in
  `analyses_read`, no proposal row written.
- NULL `timing_score` → `50.0`.
- NULL `estimated_rr` → `rr_score = 0`; sorts lowest in the RR tie-break
  (treated as `−inf`).
- NULL `sector` → `__UNKNOWN_SECTOR__`; NULL `industry` → `__UNKNOWN_INDUSTRY__`.

## Scoring

```
rr_score = 100 if estimated_rr >= 3.0
           80  if 2.2 <= estimated_rr < 3.0
           60  if 1.8 <= estimated_rr < 2.2
           0   otherwise / NULL

proposal_score_raw = 0.40*setup_score + 0.25*screening_score
                   + 0.20*rr_score    + 0.15*timing_score
```

`proposal_score_raw` and `proposal_score_final` are clamped to `[0, 100]`.
(`01c_FORMULAS_AND_CONFIGS.md` §63.)

## Raw ranking

Sort analyzable rows by `proposal_score_raw` DESC, then `estimated_rr` DESC (NULL
lowest), then `ticker` ASC. Assign 1-based `raw_rank`;
`in_raw_top_n = raw_rank <= top_n`.

## Diversified ranking

### Hard-cap mode (`hard_cap_enabled = True`)

Process candidates in `raw_rank` order. `prior_sector` / `prior_industry` are the
counts of already-**accepted** candidates in that bucket.

- If adding the candidate would exceed `max_sector_count` or `max_industry_count`
  (i.e. a bucket is already full): the candidate is **rejected** but **still
  inserted** with `diversified_rank = NULL`, `in_diversified_top_n = False`,
  `selected_flag = False`, `proposal_score_final = proposal_score_raw` (no soft
  penalty — `AD-22.12`), and `rejection_reason = "sector_cap"` or
  `"industry_cap"`. If both caps are full, `"sector_cap"` takes priority.
- Otherwise accepted: next sequential `diversified_rank`,
  `proposal_score_final = proposal_score_raw`, `rejection_reason = NULL`, and the
  bucket counts increment.

### Soft-penalty mode (`hard_cap_enabled = False`)

No hard rejection. Processing in `raw_rank` order, `prior_sector_count` /
`prior_industry_count` are the counts of **earlier** (lower `raw_rank`)
candidates in the same bucket:

```
sector_multiplier    = sector_penalty   ** max(0, prior_sector_count)
industry_multiplier  = industry_penalty ** max(0, prior_industry_count)
proposal_score_final = clamp(proposal_score_raw * sector_multiplier
                                                 * industry_multiplier)
```

Then re-rank by `proposal_score_final` DESC, `ticker` ASC, and assign
`diversified_rank`. `rejection_reason = NULL` for every row.

### Shared semantics (both modes)

```
in_diversified_top_n = diversified_rank is not NULL and diversified_rank <= top_n
selected_flag        = in_diversified_top_n
selected_top_n       = in_raw_top_n OR in_diversified_top_n
```

One row is written for every analyzable Step 4 analysis, including hard-cap
rejected rows. `diversification_applied` is always `True`. `diversity_penalty` is
stored as `proposal_score_raw - proposal_score_final` (0 in hard-cap mode).
`rank_position` mirrors `raw_rank`. `mechanical_explanation` is a sorted-key JSON
payload carrying inputs, scores, both ranks, diversification mode, rejection
reason, bucket counts, and the preserved `candidate_id` / `analysis_id`.

## Config validation (before DB access)

All under the `diversification` section (legacy key names are normalised first):

- `hard_cap_enabled` — bool (required).
- `top_n` — int `> 0` (required).
- Hard-cap mode: `max_sector_count`, `max_industry_count` — int `>= 1`.
- Soft-penalty mode: `sector_penalty`, `industry_penalty` — float `0 < x <= 1`.

`strategy_config` must be a dict. `db_role` must be `prod` or `debug`
(`simulation` and any other value are rejected before any DB access). Validation
failure → `failed` with zero counts and no I/O.

## Transaction model

Read (read-only) → compute (pure Python) → single write transaction. All proposal
inserts run inside one `BEGIN TRANSACTION` / `COMMIT`, one `execute()` per row.
Any write or COMMIT error triggers `ROLLBACK`; if ROLLBACK itself raises, the
original error is re-raised unchanged (the rollback exception is suppressed so it
cannot mask the root cause). An empty plan (including when all read rows are
non-analyzable) performs no transaction. Reruns are **append-only**: a new
`run_id` creates a fresh set of rows; the module never updates or deletes.

## Write ownership

Module 15 only ever `INSERT`s into `step5_proposals`. No `UPDATE`, `DELETE`, DDL,
`ATTACH`, cleanup, providers, network, direct `duckdb` import, or `print()`. All
DB access goes through the injected/real DuckDB manager.

## G-UNKNOWN-BUCKET decision (closed)

NULL `sector` / `industry` from `ticker_master` are mapped to the
`__UNKNOWN_SECTOR__` / `__UNKNOWN_INDUSTRY__` sentinel strings and participate in
hard-cap counts / soft-penalty multipliers **as a single shared bucket**. This is
intentional: unknown-sector tickers are genuinely uncategorised and should be
subject to the same concentration limits as any named bucket. For small caps
(e.g. `max_sector_count = 1`) only one unknown-sector ticker can be accepted per
run; callers that want to exclude classification limits for unclassified tickers
must ensure `ticker_master` is populated before running Step 5.

## Tests

`tests/test_step5_proposal_engine.py`, fully offline with temp DB paths. Coverage
includes all items from the original spec plus the additions from the compliance
pass:

**Config normalisation**: legacy hard-cap names (`sector_max_positions`,
`industry_max_positions`) and legacy soft-penalty names (`sector_penalty_factor`,
`industry_penalty_factor`) are accepted transparently; canonical names continue to
work; invalid values on legacy-named keys still fail validation;
`_normalise_diversification_block` is tested as a pure function (no mutation,
correct rewrites). Supplying both a legacy and canonical name for the same key
(e.g. `sector_max_positions` + `max_sector_count`) raises `_ConfigError` before
DB access — tested for both hard-cap and soft-penalty pairs, plus a no-false-
positive case.

**COMMIT-failure rollback**: `_CommitFailingConn` lets all INSERTs through but
raises on `COMMIT`; asserts `failed` result, `rolled_back=True` on the connection,
zero rows in DB, and `analyses_read` preserved.

**Rollback hardening**: `_RollbackFailingConn` verifies that a ROLLBACK failure
does not mask the original insert error; `_FailingConn.rolled_back` flag asserted
True after write failure.

**Metadata keys on all six paths**: guard failure, config failure, read failure,
write failure, empty, success — exact 9-key set verified on each.

**No write transaction when all non-analyzable**: `TrackingConn` asserts that
`BEGIN TRANSACTION` is never called when `_build_rows` returns `[]`.

**Package-level import**: `from app.services.proposal import Step5ProposalEngine`
resolves to the same class.

All original coverage from the first delivery is retained unchanged.

## Assumptions / open gaps (resolved)

- **`G-SOFT-PENALTY-PRIOR-COUNT`** — RESOLVED. Both naming conventions
  (`sector_penalty_factor`/`industry_penalty_factor` from `01c` example blocks,
  and `sector_penalty`/`industry_penalty` from the module prompt) are accepted via
  `_normalise_diversification_block`. Canonical names are the authoritative
  internal representation. Supplying both a legacy and canonical name for the same
  key is rejected as an ambiguous misconfiguration.
- **`G-UNKNOWN-BUCKET`** — RESOLVED (see section above). Sentinel-bucket
  behaviour is intentional and documented. No production behaviour change planned;
  callers must ensure `ticker_master.sector` / `industry` are populated if they
  want named-bucket semantics for every ticker.
- **`hard_cap` bucket counts stored** — `sector_count_at_selection` /
  `industry_count_at_selection` store the accepted-bucket count *after* accepting
  a row, and the *prior* (full) count for a rejected row. Soft-penalty rows store
  the 1-based occurrence index. Unchanged from V1.
