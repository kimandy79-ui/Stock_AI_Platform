# Part A — Decouple `insider_trade_flag` into a Separate Pipeline Step

**Date:** 2026-07-20
**Status:** Implemented, tested, not committed (per instruction).

---

## Summary

`insider_trade_flag` computation (SEC-EDGAR-native, ~82min at Step-4 scale) has
been moved out of `fundamentals_refresh`'s per-ticker loop into its own
pipeline step, `insider_flag_refresh`, placed immediately after
`fundamentals_refresh` and before `price_ingestion`. `fundamentals_refresh` no
longer wires a live `insider_lookup` into its `EdgarFundamentalsProvider` at
all, regardless of `compute_insider_flag`; that flag now gates the new step
directly.

## Placement decision

The note left exact placement open ("immediately after
`feature_calculation`/`fundamentals_refresh` — confirm exact placement"). I
placed it right after `fundamentals_refresh` (new step 5 of 16, before
`price_ingestion`), not after `feature_calculation` (step 9), because both of
the new step's real prerequisites are satisfied by then and nothing later:

- the active-ticker universe comes from `universe_ingestion` (step 2), and
- the `ticker_fundamentals` rows it `UPDATE`s come from `fundamentals_refresh`
  (step 4) itself.

`feature_calculation` computes unrelated price/technical features and has no
bearing on either prerequisite, so waiting for it would only delay the step
for no reason.

## Changes

**`app/services/pipeline/pipeline_orchestrator.py`**
- `STEP_NAMES`: new entry `"insider_flag_refresh"` between `"fundamentals_refresh"`
  and `"price_ingestion"` (16 steps total, was 15). Module docstring's numbered
  step list updated to match.
- `RECOVERABLE_STEPS`: `"insider_flag_refresh"` added. Not added to
  `CRITICAL_STEPS`, so `_safe_step`/`_classify` isolate any raised exception or
  `STATUS_FAILED` result from failing the overall run (same mechanism that
  already protects `fundamentals_refresh`/`earnings_calendar_refresh`).
- `run()`'s `linear_steps` tuple: `("insider_flag_refresh", self._step_insider_flag)`
  registered right after `("fundamentals_refresh", self._step_fundamentals)`.
- `_resolve_fundamentals_provider`: no longer calls `_build_insider_lookup` —
  the default `EdgarFundamentalsProvider` it builds for `fundamentals_refresh`
  now always has `insider_lookup=None`. Docstring updated.
- New SQL constants: `_SQL_INSIDER_FLAG_EXISTING_ROWS` (`SELECT ticker FROM
  ticker_fundamentals WHERE as_of_date = ?`) and `_SQL_INSIDER_FLAG_UPDATE`
  (`UPDATE ticker_fundamentals SET insider_trade_flag = ? WHERE ticker = ? AND
  as_of_date = ?`).
- New method `_step_insider_flag`: see "New step's implementation" below.
- `_check_sec_user_agent`'s pre-run warning text extended to also mention
  `insider_flag_refresh` (it has no yfinance-style fallback — an unset
  `SEC_USER_AGENT` means every ticker gets a per-ticker warning, not a crash).

**`app/providers/edgar_provider.py`**
- New public method `EdgarFundamentalsProvider.resolve_cik(ticker) -> str | None`
  — a thin wrapper around the existing (private) `_ticker_to_cik` resolver plus
  `normalize_ticker`, added so `_step_insider_flag` can reuse the same
  on-disk-cached CIK resolution `get_fundamentals` already does internally,
  without duplicating `_load_ticker_map`/`_parse_ticker_map`/disk-cache logic.
  Purely additive — nothing else changed in this file's core logic, thresholds,
  or fallback behavior.
- `insider_lookup` constructor-param docstring updated to reflect it's always
  `None` in production now (the parameter itself, and the pass-through to
  `compute_fundamentals_from_companyfacts`, are left in place — harmless, and
  still directly exercised by `test_edgar_provider_insider_lookup.py`).

## New step's implementation (`_step_insider_flag`)

1. Kill-switch: reads `compute_insider_flag` via the existing `_pipeline_flags`
   helper. Disabled → returns `SUCCESS_WITH_WARNINGS`, 0 rows, one warning,
   **zero DB or network calls** (the gate fires before any query).
2. Reads the same active-ticker population `fundamentals_refresh` uses
   (`_SQL_EARNINGS_TICKERS` — `ticker_master WHERE symbol_type='stock' AND
   active_flag=TRUE`). No active tickers → warning, 0 rows, not a failure.
3. Reads which of those tickers already have a `ticker_fundamentals` row for
   `run_date` (one query, `_SQL_INSIDER_FLAG_EXISTING_ROWS`) **before** doing
   any SEC EDGAR work. A ticker can be active but rowless if its
   `fundamentals_refresh` fetch failed that day — for those, `UPDATE` can
   never land, so this step skips the SEC EDGAR call entirely (no wasted
   request) and logs one aggregated warning (`"N/M ticker(s) had no
   ticker_fundamentals row ...; UPDATE would affect 0 rows, skipped"`).
4. Builds one `_SecHttpClient` (`build_sec_http_client()`) for this step and
   reuses it for **both** CIK resolution (`EdgarFundamentalsProvider(fetch_json=...).resolve_cik`)
   and the insider lookup itself (`_build_insider_lookup`'s closure) — see
   "Design note: client sharing" below.
5. Per ticker with an existing row: resolves CIK, calls the insider-lookup
   closure, and queues `(flag, ticker, run_date)` for a batched `UPDATE`
   (`True`/`False`/`None` are all legitimate values to persist — `None` means
   "genuine retrieval failure", matching the pre-existing semantics
   `fundamentals_refresh` already used for this same column). Any exception
   (CIK resolution raising, or anything else) is caught, turned into a
   per-ticker warning, and that ticker is skipped — the loop continues.
6. All queued `UPDATE`s run in one transaction (`BEGIN`/`COMMIT`/`ROLLBACK`,
   same pattern as `_step_fundamentals`'s batch upsert).
7. Returns `SUCCESS` if no warnings, else `SUCCESS_WITH_WARNINGS`; never
   `FAILED` from this step's own logic (and even a raised exception is caught
   by `_safe_step` and downgraded to a warning, since the step isn't in
   `CRITICAL_STEPS`).

### Design note: the "0-row UPDATE" defensive check

The note asked: *"if the UPDATE affects 0 rows, log a warning."* DuckDB's
Python connection doesn't expose a portable post-UPDATE affected-row count
across the fakes this test suite is built on — this is a **pre-existing,
already-documented limitation** in this codebase
(`config_recommender.py:561`'s own comment, which re-`SELECT`s to verify a
write landed rather than trusting `rowcount`). Rather than reactively
inspecting each `UPDATE`, `_step_insider_flag` predicts the 0-row case
up front (step 3 above) from a single query, and skips + warns for exactly
the tickers that would 0-row. This satisfies the requirement's intent (every
non-landing write is logged, never silently swallowed, never fails the step)
while also avoiding a wasted SEC EDGAR request for a ticker that has nowhere
to write its result — a small efficiency bonus, not just a mechanism swap. I
made this call unilaterally given the documented rowcount limitation; flagging
it explicitly in case a literal per-`UPDATE` check was intended instead.

### Design note: client sharing

The original (now-removed) comment on `_resolve_fundamentals_provider`
justified building exactly **one** `_SecHttpClient` because `fundamentals_refresh`
and the insider lookup used to run *concurrently within the same per-ticker
loop*, and two independently-throttled clients would have doubled the
effective request rate against SEC's shared fair-access budget. Now that
they're separate, sequential steps, that specific concern no longer applies
between them — so `_step_insider_flag` builds its own fresh client via the
same `build_sec_http_client()` function rather than trying to reach back into
`fundamentals_refresh`'s (already-discarded, previous-step) client. Within
`_step_insider_flag` itself, everything (CIK resolution + submissions +
filing-XML) still goes through that one client, so no *new* double-throttling
is introduced either. Noting this as a deliberate interpretation of "reusing
the existing shared `_SecHttpClient`" from the note — the mechanism (one
client per unit of concurrent SEC traffic) is preserved; the object identity
across steps is not, because it no longer needs to be.

## What was NOT changed (per instruction)

- `edgar_insider_provider.py`'s internal fetch logic, thresholds, and 10b5-1
  exclusion — untouched.
- The other 5 EDGAR fundamentals fields' computation or timing — untouched;
  `fundamentals_refresh`'s `upsert_rows` construction, source-lineage
  counting, and batch-upsert transaction are all unchanged.
- The ~82-minute cost itself — unchanged, just relocated to its own step.

## Testing

**`tests/test_pipeline_fundamentals_insider_wiring.py`** (updated + extended,
20 tests, all passing):
- `TestResolveFundamentalsProviderSharesOneHttpClient` renamed to
  `TestResolveFundamentalsProviderNoLongerWiresInsiderLookup` and its two
  tests rewritten: `provider._insider_lookup is None` now holds
  unconditionally (both `compute_insider_flag=True` and `False`), proving
  `fundamentals_refresh`'s SEC-request surface no longer includes any
  submissions/filing-XML calls.
- New `TestStepInsiderFlagRegisteredCorrectly`: `STEP_NAMES` placement
  (`fundamentals_refresh` < `insider_flag_refresh` < `price_ingestion`),
  `RECOVERABLE_STEPS` membership, `CRITICAL_STEPS` non-membership.
- New `TestStepInsiderFlagRefresh` (8 tests): kill-switch skip (zero DB
  calls), no-active-tickers warning, happy-path `UPDATE` (asserts the exact
  params and that both CIK-resolution and submissions requests went through
  the one client), missing-`ticker_fundamentals`-row skip+warning (asserts
  the SEC request for that ticker never happens), unresolvable-CIK warning,
  per-ticker exception isolation (one ticker raises, the other still lands),
  all-tickers-missing → `SUCCESS_WITH_WARNINGS`/0 rows/zero SEC requests, and
  a pinned assertion that the `UPDATE` touches only `insider_trade_flag` (not
  the other 5 EDGAR columns).

**Runtime-baseline confirmation (requirement #3):** can't literally time an
82-minute network run offline. Structural proof instead:
`provider._insider_lookup is None` (proven above) means
`EdgarFundamentalsProvider._safe_insider_lookup` returns `None` immediately
(`if self._insider_lookup is None: return None`) without reaching the
submissions/filing-XML call sites — those are the *only* call sites
responsible for the ~82-minute cost. Combined with
`test_edgar_provider_insider_lookup.py::test_without_a_lookup_the_flag_stays_none`
(pre-existing, unchanged, still passing), this proves the cost is now
structurally unreachable from `fundamentals_refresh`.

**Regression run** (as specified):
```
pytest tests/test_pipeline_orchestrator.py tests/test_pipeline_fundamentals_step.py \
       tests/test_edgar_provider_insider_lookup.py tests/test_pipeline_fundamentals_insider_wiring.py -v
```
→ **106 passed.**

**Broader regression** (orchestrator/edgar-adjacent, not requested but run for
safety given `pipeline_orchestrator.py`'s wide usage):
```
pytest tests/test_phase6_orchestrator.py tests/test_orchestrator_config_loading.py \
       tests/test_p2_6_valuation_band_price_lookup.py tests/test_p2_5_orchestration_wiring.py \
       tests/test_phase6_diagnostics.py tests/test_phase7_setup_mode.py tests/test_debug_mode.py \
       tests/test_run_debug_pipeline.py tests/test_edgar_provider.py tests/test_edgar_provider_resilience.py -v
```
→ **373 passed, 7 skipped** (pre-existing legacy strategy-mode skips,
unrelated to this change).

Full-suite run (`pytest tests/`) completed: **3 failures, all pre-existing and
unrelated** — `test_data_validator.py::test_spec_documents_open_gaps_not_invented`,
`test_mutation_detector.py::test_spec_documents_open_gap_g1` (both spec-path
`FileNotFoundError`s), and `test_yahoo_provider.py::test_only_yahoo_provider_references_yfinance`
(edgar/yahoo provider overlap). These match a known, already-logged,
out-of-scope failure set dated 2026-07-08, predating this change entirely —
none touch `pipeline_orchestrator.py` or `edgar_provider.py`'s insider-flag
code paths. No new failures anywhere else in the suite.

## Anomalies noted (not fixed — out of scope)

`app/services/debug/debug_mode.py` maintains its **own, independent** local
copy of `STEP_NAMES` (comment: `"Local copy of orchestrator step names
(avoids importing orchestrator → duckdb)"`), which was **already** out of
sync with the real one before this change: it's missing `fundamentals_refresh`
entirely and contains a `"backup"` step that doesn't exist in the real
`pipeline_orchestrator.STEP_NAMES` I read. This pre-existing drift is
unrelated to and unaffected by this change (the two tuples are independent
objects; `debug_mode.py`'s tests reference only its own copy), but it means
`insider_flag_refresh` is also absent from the debug copy. Not touched here —
out of scope for this note and worth its own scoped fix.
