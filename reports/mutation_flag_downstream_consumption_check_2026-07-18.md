# Downstream Consumption Check — Does Anything Read `mutation_flag`?

**Date:** 2026-07-18
**Scope:** Read-only investigation. No code changes.
**No writes to `prod`.** No conclusion drawn on fixing the root cause
(separate, later decision) — this answers only "does anything downstream
care about this flag's value today."

**Clear conclusion, stated first:** **`mutation_flag` is inert
bookkeeping with zero behavioral consequence anywhere in the codebase
today.** No feature calculation, no eligibility/screening check, no
validator, no scoring path, and no automated process reads it (or the
`feature_rebuild_log`/`data_repair_queue` rows it drives) to change any
computation. The **one** genuine "read" found is a passive admin-dashboard
table display — cosmetically visible to a human, not consumed by any
pipeline logic. This is a data-integrity/reporting-cleanliness issue, not
an active-computation-affecting one; it does not need urgent priority on
correctness grounds (a human glancing at the admin dashboard could be
misled, which is the one real, if minor, consequence — see §3).

---

## 1. Every code location that reads `mutation_flag` — complete list

Repo-wide search across all of `app/` and `tools/` for the literal string
`mutation_flag` returns exactly 7 files. Read each in context:

| File | What it does with `mutation_flag` | Read or write? |
|---|---|---|
| `app/database/schema_manager.py:149` | Column DDL: `mutation_flag BOOLEAN NOT NULL DEFAULT FALSE` | Schema definition only |
| `app/providers/provider_interface.py:103` | Docstring note that this is a schema-only field, not vendor-supplied | Comment only |
| `app/services/benchmarks/benchmark_etf_loader.py:145,159` | UPSERT sets `mutation_flag = FALSE` on insert; `= excluded.mutation_flag` (still `FALSE`, since the VALUES list hardcodes it) on conflict | **Write** only |
| `app/services/ingestion/daily_price_ingestion.py:145,159` | Same UPSERT pattern — writes `FALSE` on every new row insert | **Write** only |
| `app/services/validation/data_validator.py:33` | Docstring confirming M09 does *not* touch `mutation_flag` (out-of-scope note) | Comment only |
| `app/services/mutation/mutation_detector.py` | `_PriceRow.mutation_flag` is read at `:513` (`if row.mutation_flag is False:`) to decide whether to include the row in `flag_rows` — the module's own no-downgrade idempotency check (avoid re-flagging an already-`TRUE` row) | **Read**, but self-referential — M10 reading its own prior output, not a downstream consumer |
| `app/services/mutation/__init__.py:6` | Docstring describing what the module does | Comment only |

`tools/` — zero matches for `mutation_flag` anywhere.

**No other module — `feature_engine.py`, `market_regime_engine.py`,
`step3_universal_eligibility.py`, `m14_setup_validators.py`,
`step4_setup_validation_engine.py`, `step5_proposal_engine.py`,
`pipeline_orchestrator.py`, `simulation_engine.py`, the dashboard, or
anything else — references `mutation_flag` at all.** The only true "read
for a decision" in the entire codebase is M10's own no-downgrade check on
its own field, which exists purely to make re-runs idempotent, not to let
any other module react to a row having been flagged.

---

## 2. `feature_engine.py` — confirmed: does not check the flag

`feature_engine.py`'s module docstring (`:4-5`) mentions "mutation-checked"
only to describe **pipeline position** ("Runs after Module 10 (Mutation
Detector)") — a sequencing note, not a data dependency. Checked its actual
read queries:

```python
_SELECT_DISTINCT_ELIGIBLE_TICKERS = (
    "SELECT DISTINCT ticker FROM daily_prices "
    "WHERE date >= ? AND date <= ? AND data_quality_status = ? "
    "ORDER BY ticker"
)
_SELECT_PRICE_COLUMNS = (
    "SELECT ticker, date, close_raw, high_raw, low_raw, "
    "close_adj, high_adj, low_adj, volume_raw "
    "FROM daily_prices "
    "WHERE data_quality_status = ? AND date >= ? AND date <= ? "
    "AND ticker IN ({placeholders}) ORDER BY ticker, date"
)
```

**`mutation_flag` is not in the `SELECT` list, not in any `WHERE` clause,
and not referenced anywhere else in the file.** Feature computation
filters exclusively on `data_quality_status`.

This is also confirmed by an existing, already-passing test —
`tests/test_feature_engine.py::test_only_daily_features_written`
(`:606-624`) — which explicitly snapshots row counts across a `forbidden`
table list including `data_repair_queue` and `feature_rebuild_log` before
and after `FeatureEngine().calculate()`, asserting they're unchanged. The
same pattern exists in `tests/test_market_regime_engine.py:525-528` for
M12. **`feature_engine.py` never reads `feature_rebuild_log` either** — no
module anywhere consumes that queue to decide "this ticker needs
recomputation." A flagged ticker's features are computed on exactly the
same schedule and with exactly the same logic as an unflagged one.

---

## 3. Eligibility / `data_quality_status` — confirmed: separate, unrelated gate

Re-confirmed the prior investigation's note: `MutationDetector.is_eligible()`
(`mutation_detector.py:262-264`) checks `data_quality_status == 'ok'` —
`mutation_flag` plays no role in *this* eligibility check either; it's
purely which rows M10 itself is willing to process, unrelated to any
downstream screening.

Checked Step 3 eligibility (`step3_universal_eligibility.py`) and M14
setup validators (`m14_setup_validators.py`) directly — neither appears in
the repo-wide `mutation_flag` search results at all (§1), confirming
`mutation_flag` has no separate gating logic anywhere in the screening/
validation chain. The **only** quality gate any downstream module checks
is `data_quality_status`, which M10 never writes to (`mutation_detector.py`'s
own docstring, `:27-36`, explicitly lists `data_quality_status` as
out-of-scope for M10 — it's an M09-owned column). The two concepts —
"data quality" and "mutation flag" — are completely independent columns
with independent write-owners and no downstream code that joins or
cross-references them.

---

## 4. The one real consumption point — dashboard display, not computation

`app/dashboard/data_access.py:578-587`:
```python
def load_repair_queue(self, limit: int = DEFAULT_REPAIR_LIMIT) -> list[dict[str, Any]]:
    """Recent ``data_repair_queue`` rows, newest first."""
    return self._fetch_dicts(
        "SELECT repair_id, ticker, repair_date, repair_reason, attempts, "
        "max_attempts, status, created_at "
        "FROM data_repair_queue ORDER BY created_at DESC LIMIT ?",
        [int(limit)],
    )
```
Called from `streamlit_app.py:1546` (`st.dataframe(loader.load_repair_queue() or [], ...)`)
in an admin/diagnostics panel labeled "Repair queue," `DEFAULT_REPAIR_LIMIT = 50`.
This is the **only** place in the entire codebase that reads either
`data_repair_queue` or `feature_rebuild_log` back out for any purpose
beyond writing to them or blanket-deleting them (`reset_pipeline_data.py`,
a full-table wipe utility) or denylisting them (`import_legacy_prices.py`'s
`FORBIDDEN_TABLES` guard, which never reads their contents either — it
only refuses to touch them). No filtering, branching, or computation is
based on the fetched rows — it's a passive `st.dataframe` render.

**Practical consequence, though not a computational one:** because the
query orders `created_at DESC LIMIT 50`, a human operator opening this
dashboard panel today would see **only today's 2026-07-18 backfill's
spurious `mutation` entries** (all 3,785 are newer than everything else
in the table) — potentially reading as "50 tickers just had a real price
mutation," which is misleading. This is the one place the over-flagging
is actually *visible* to anyone, but it's a human-facing display artifact,
not a behavior-affecting read.

---

## 5. Anomalies (verbatim)

- None beyond what's already reported: the repo-wide search was exhaustive
  (`mutation_flag` — all of `app/` and `tools/`, 7 files, all accounted
  for above; `feature_rebuild_log` — 15 files including specs/tests,
  all production-code occurrences accounted for above) and consistent —
  every non-comment, non-DDL occurrence is either a write, a self-referential
  idempotency read inside M10 itself, or the one passive dashboard display.
  No hidden consumer was found.

## No writes, no code changes

Read-only investigation only: `mutation_detector.py`, `feature_engine.py`,
`data_validator.py`, `benchmark_etf_loader.py`, `daily_price_ingestion.py`,
`provider_interface.py`, `schema_manager.py`, `data_access.py`,
`streamlit_app.py`, `reset_pipeline_data.py`, `import_legacy_prices.py`,
and the relevant test files were read, none modified. No commit.
