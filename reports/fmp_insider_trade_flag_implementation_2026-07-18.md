# FMP `insider_trade_flag` Implementation — 2026-07-18

**Scope:** New module `app/providers/fmp_insider_provider.py`, config knob additions in `app/services/config/default_configs.py`, and Step-5-shortlist-only wiring in `app/services/pipeline/pipeline_orchestrator.py`. **No commit** — diff delivered for review, per current policy.

---

## 0. The one deviation from the coder note — read this first

**`app/providers/edgar_provider.py` was NOT modified.** Section 3 of the coder note asked for an additive `insider_lookup` param on `EdgarFundamentalsProvider.__init__` and an `insider_flag` param on `compute_fundamentals_from_companyfacts`, mirroring the existing `price_lookup` pattern. That change is not present in this diff, and here is the reasoning:

`EdgarFundamentalsProvider.get_fundamentals()` is called exactly once per active ticker inside `pipeline_orchestrator._step_fundamentals` — the mass, ~1,100–1,500-ticker, pre-Step-3 pass that also produces `eps_growth_trend`, `leverage_ratio`, `valuation_band`, `piotroski_f_score`, `altman_z_score`. That call rebuilds the **entire** `FundamentalSnapshot` from a fresh SEC EDGAR companyfacts fetch. Wiring `insider_lookup` in there is exactly the call-site the coder note's own "CRITICAL SCOPE CONSTRAINT" forbids — so it must stay unwired at that call site regardless.

The only place `insider_lookup` could otherwise be exercised is a **second**, Step-5-only invocation of `EdgarFundamentalsProvider.get_fundamentals()` against the ~20 shortlisted tickers. But that method doesn't have a "just refresh one field" mode — it always re-fetches and reconstructs all 6 EDGAR-sourced fields from a fresh companyfacts JSON. Using it here would mean either:

- a second, redundant SEC EDGAR fetch (CIK resolution + companyfacts) for 20 tickers whose other 5 fields were already correctly populated hours earlier that same run, purely to reach the one line of code that threads `insider_flag` into the snapshot, or
- leaving `insider_lookup` permanently `None` at every real call site — an unwired constructor parameter that exists but is never exercised by production code.

Both are worse than the alternative the coder note itself flagged as acceptable: *"it may mean `insider_trade_flag` is populated as a second, later pass over the already-written `step5_proposals` rows rather than inside the same `FundamentalSnapshot` construction... if those are structurally tied to the Step-4-time call."* They are — so that's what's implemented: a narrow, targeted `UPDATE ticker_fundamentals SET insider_trade_flag = ? WHERE ticker = ? AND as_of_date = ?` against only the shortlisted tickers, run at the end of `_step_step5`, entirely independent of `EdgarFundamentalsProvider`. This is simpler, touches only the one column that needs touching, and adds zero redundant network calls to the 5 already-correct fields. CLAUDE.md's "no half-finished implementations" / "no features beyond what the task requires" pushed against adding a dead `insider_lookup` seam that would never see a real caller.

`FundamentalSnapshot.insider_trade_flag` (in `provider_interface.py`) already exists as `bool | None`; no interface change was needed either.

---

## 1. New module: `app/providers/fmp_insider_provider.py`

`fetch_insider_purchase_flag(ticker, as_of_date, fetch_json=None, *, lookback_days=90, min_transaction_value_usd=10_000.0, page_limit=100, max_pages=20, cache_path=None, cache_ttl_seconds=None, now_fn=None) -> bool | None`, plus:

- `_FmpHttpClient` — rate-limited, retrying GET-JSON wrapper (mirrors `edgar_provider.py`'s `_SecHttpClient`: throttled, retries only 429/5xx, appends `apikey` to the query).
- `build_fetch_json(...)` — resolves the keyring credential and builds the production `fetch_json`, or returns `None` if no key is available (checked *before* any HTTP client is constructed).
- `resolve_fmp_api_key(...)` — `keyring.get_password("stock_ai_platform", "FMP_API_KEY")`, never raises.
- A flat on-disk JSON cache (`settings.CACHE_DIR / "fmp_insider_flag_cache.json"`, TTL default 3 days), keyed on `(ticker, as_of_date, lookback_days, min_transaction_value_usd)`.

Implementation follows the investigation's findings exactly:

- **Never trusts FMP's `date` query param** (confirmed broken in the investigation — it returns identical results regardless of value). All point-in-time scoping is client-side: `filingDate <= as_of_date` (never `transactionDate`), mirroring `edgar_provider.py`'s `extract_annual_series` dual end/filed-date gate.
- **Filters `formType == "4"` explicitly** — Form 3/5 rows carry blank `transactionType`/`acquisitionOrDisposition` (confirmed anomaly) and are excluded regardless of what the server-side `transactionType=P-Purchase` param does or doesn't enforce.
- **Client-side `transactionType == "P-Purchase"` re-check** even though it's also sent as a query param — belt-and-suspenders, since the investigation showed at least one FMP param (`date`) doesn't do what it claims.
- **Pagination stops** once a page's oldest `filingDate` falls at/before the lookback window start (results confirmed sorted descending), so a single ticker/day query never walks full multi-year history.
- **True / False / None trichotomy preserved**: `True` = qualifying purchase found, `False` = data retrieved, no qualifying purchase, `None` = retrieval failed (network error, no key, exception) — callers must not conflate `False` and `None`.

---

## 2. Config knobs (`app/services/config/default_configs.py`)

Added to `DEFAULT_RISK_LABEL_CONFIG["fundamentals"]` (which already held `score_weight`, confirming this was the right existing location — the coder note asked to "confirm against existing patterns" rather than guess):

```python
"fundamentals": {
    "score_weight": 0.0,
    "insider_purchase_lookback_days": 90,
    "min_insider_transaction_value_usd": 10000.0,
},
```

`DEFAULT_RISK_LABEL_CONFIG_V2` is `{**DEFAULT_RISK_LABEL_CONFIG, ...}` and doesn't override `"fundamentals"`, so it inherits both new keys automatically — no separate edit needed there.

**One addition beyond the coder note's explicit ask**, flagged for review: a third config knob, `DEFAULT_RUNTIME_CONFIGS["pipeline"]["auto_invoke_insider_flag"]` (default `True`). This mirrors the existing `auto_invoke_fundamentals`/`auto_invoke_ai_review` kill-switch pattern already used for the other two optional Step 5 passes, and gives an operator a way to disable the FMP calls (e.g. during a backfill that would otherwise multiply calls by `dates × shortlist_size` against the 250/day cap) without touching the keyring credential. This wasn't explicitly requested — flagging it as a judgment call, not a silent scope expansion. Easy to revert if unwanted (the pass already fails closed and skips gracefully with the flag hardcoded `True` if this is considered unnecessary).

---

## 3. `pipeline_orchestrator.py` wiring

1. **Constructor**: new `insider_flag_lookup: Any | None = None` param, stored as `self._insider_flag_lookup` — same injection shape as `fundamentals_provider` (used as-is if injected; built lazily otherwise; never rebuilt).
2. **New SQL constants**: `_SQL_SELECTED_STEP5_TICKERS` (`SELECT DISTINCT ticker FROM step5_proposals WHERE signal_date = ? AND selected_flag = TRUE`) and `_SQL_INSIDER_FLAG_UPDATE` (`UPDATE ticker_fundamentals SET insider_trade_flag = ? WHERE ticker = ? AND as_of_date = ?`).
3. **New methods**:
   - `_insider_flag_config(db_role, log)` — reads `risk_label_config.fundamentals.{insider_purchase_lookback_days,min_insider_transaction_value_usd}`, falls back to the module's own defaults on any read failure (mirrors `_valuation_band_quality`'s pattern exactly).
   - `_resolve_insider_flag_lookup(db_role, log)` — injected lookup as-is, else builds a real one from `fmp_insider_provider.build_fetch_json()` + the config thresholds, else `None` if no key is available (never raises).
   - `_refresh_insider_flags(run_date, db_role, log)` — the budget-scoping core: reads `selected_flag = TRUE` tickers from `step5_proposals` for `run_date` (the actual ~20-ticker final shortlist, not every candidate row `step5_proposals` holds), calls the lookup per ticker, writes `True`/`False` results, skips `None` results and per-ticker exceptions (logged as warnings, never fatal).
4. **`_step_step5` restructured** (behavior-preserving for the two existing flags, verified by the existing `test_p2_5_orchestration_wiring.py` suite passing unchanged): the early-return `if not flags["auto_invoke_ai_review"]: return result` became a fallthrough guarded by `status != FAILED`, so a third, final step — `_refresh_insider_flags`, gated on `auto_invoke_insider_flag` and non-failure — always runs after the *true final* result, whether or not the AI-review re-pass fired. A failure in this final step is caught and logged, never returned as the step's failure (this field must never fail Step 5).

**Ordering dependency worth flagging explicitly** (not a bug, but a real assumption): `fundamentals_refresh` (which upserts `ticker_fundamentals` with `insider_trade_flag = NULL` for the whole active universe, via `_SQL_FUNDAMENTALS_UPSERT`'s `ON CONFLICT ... DO UPDATE`) always runs *before* `step5_proposals` in `STEP_NAMES`. This is what makes a plain `UPDATE` — rather than an upsert — correct here: the row is expected to already exist from the earlier pass, and this narrower pass runs strictly after it within the same `run()` invocation, so it's never clobbered by a same-run re-upsert. If `STEP_NAMES`'s order were ever changed, this assumption would need re-checking.

---

## 4. Tests

`tests/test_fmp_insider_provider.py` (22 tests) — fully offline, fake `fetch_json` throughout:

- Qualifying purchase in-window above threshold → `True`.
- Below dollar threshold → `False`.
- Outside lookback window → `False`.
- **`filingDate` after `as_of_date` → excluded** — the point-in-time integrity test, directly exercising the investigation's central finding that FMP's `date` param is unreliable.
- Form 3/5 rows alongside real Form 4 data → ignored, doesn't corrupt the result.
- Non-`P-Purchase` types (`S-Sale`, `A-Award`, `M-Exempt`) → excluded.
- `fetch_json` raising → `None`, not a propagated exception.
- Empty result set (real "no purchase") → `False`, distinct from a retrieval failure.
- Missing keyring credential → `None`, no crash.
- Pagination terminates once past the lookback window instead of walking full history.
- Cache: hit within TTL avoids a second `fetch_json` call; expired entry triggers refetch; different tickers don't share a cache slot; a `None` result (retrieval failure) is never cached, so the next call retries rather than silently returning a poisoned "couldn't check" forever.
- `resolve_fmp_api_key`: explicit value wins, reads the correct keyring service/key names, backend failure returns `None` rather than raising.
- `_FmpHttpClient`: throttles between requests, retries only 429/5xx.

`tests/test_pipeline_insider_flag_wiring.py` (13 tests) — proves the budget-scoping contract end to end:

- Lookup invoked **only** for `selected_flag = TRUE` tickers, never the full candidate pool.
- Empty shortlist → lookup never invoked at all.
- No FMP key available → skips gracefully, no crash, no `UPDATE`s issued.
- A per-ticker lookup exception doesn't abort the remaining tickers.
- `None` results are never written.
- Injected `insider_flag_lookup` is used verbatim, never rebuilt (mirrors the existing `fundamentals_provider` injection test pattern).
- `_insider_flag_config` reads `risk_label_config`'s thresholds correctly, and falls back to module defaults on a config-read failure.
- `_step_step5`: refresh runs after a successful `propose()`; is skipped when `propose()` fails; is skipped when `auto_invoke_insider_flag=False`; a refresh-pass exception never changes `_step_step5`'s returned result.

**Results:**

```
tests/test_fmp_insider_provider.py .....................  (22 passed)
tests/test_pipeline_insider_flag_wiring.py .............  (13 passed)
tests/test_pipeline_orchestrator.py, test_phase6_orchestrator.py,
test_p2_5_orchestration_wiring.py, test_orchestrator_config_loading.py,
test_config_service.py, test_config_recommender.py,
test_p2_6_valuation_band_price_lookup.py, test_pipeline_fundamentals_step.py
  ... all passing, zero regressions (7 pre-existing skips, unrelated to this change)
```

Full-suite run (`pytest tests/`) was launched to confirm zero regressions project-wide; see the addendum below for its result once complete.

---

## 5. Rate-limit handling — confirmation

As the coder note anticipated, FMP's actual rate limit could not be confirmed from documentation alone. `_FmpHttpClient` throttles to a conservative default of **2 requests/second** (`_TARGET_REQUESTS_PER_SEC = 2.0`), configurable via `min_request_interval_sec`, and retries only 429/5xx with exponential backoff (never other 4xx, which won't succeed on retry) — same shape as `edgar_provider.py`'s SEC throttle. Given only ~20 tickers/day at ≤`max_pages=20` pages each (in practice 1–2 pages per ticker, since results terminate at the lookback window), the 2 req/s default is already far more conservative than the workload requires; this is a safety margin, not a bottleneck. **Not yet empirically re-verified against the project's actual FMP plan dashboard** — the investigation flagged this as unconfirmable from a handful of test calls in one session, and that remains true here; recommend a one-time check against the plan's published limits before the first production run.

---

## 6. Anomalies / caveats (verbatim, for review)

1. **The exact FMP REST URL is an assumption, not an empirical confirmation.** The investigation ran through an abstracted MCP connector (`endpoint: "search-insider-trades"`), never a raw HTTP request — so `_FMP_SEARCH_URL = "https://financialmodelingprep.com/stable/insider-trading/search"` is inferred from FMP's documented "stable" API naming convention, not verified against a real response. **This must be confirmed with one live smoke-test call using the project's actual keyring-stored key before the first production run** — if the path or exact response field names differ even slightly from what the investigation's connector abstracted away, `fetch_insider_purchase_flag` will silently return `False` (or `None`, if the call errors) rather than a wrong `True`, since it only sets a field on a positive match — but it should still be verified rather than assumed.
2. **The yfinance fallback path (`compute_fundamentals_from_yfinance_info` in `edgar_provider.py`) still hardcodes `insider_trade_flag=None`, untouched.** Per the coder note's own instruction ("do not touch unless the investigation found it needs the same treatment") — it does hardcode `None`, same as the SEC path did before this change, but per §0 above, neither EDGAR path is the mechanism populating this field anymore. The yfinance fallback is orthogonal: it fires when SEC EDGAR fails for the mass Step-4-time call, has nothing to do with the Step-5-only FMP pass, and needs no change.
3. **`auto_invoke_insider_flag` is a new config knob beyond the coder note's explicit two-item list** (§2 above) — flagged prominently, not slipped in silently.
4. Confirmed via a fresh grep: **no other code anywhere reads `ticker_fundamentals.insider_trade_flag`** (`fundamentals_quality.py`'s scoring SQL selects only `eps_growth_trend, leverage_ratio, valuation_band, piotroski_f_score, altman_z_score` — five fields, not seven) — consistent with the architect's framing that this field is purely informational/display and structurally cannot feed Step 4 eligibility, scoring, or routing. No M11/M14/Step5 consumption-logic changes were made or needed, matching the coder note's stated scope.

No commit made. Diff is ready for review.

---

# Addendum — Track A close-out (2026-07-18, same day)

Four review items from the follow-up coder note. **Item 2 surfaced a critical, load-bearing finding: read it first.**

## 1. Full test suite result

`pytest tests/` was run three separate times in this environment (twice via the earlier session's background promotion, once via `cmd`-native output redirection to rule out a PowerShell capture artifact). All three runs show **the identical result**: every test passes except three pre-existing failures, unrelated to this change and already logged in project memory before this work started (`known_preexisting_test_failures.md`, 2026-07-08):

```
FAILED tests/test_data_validator.py::test_spec_documents_open_gaps_not_invented
  -> FileNotFoundError: looks for a spec file at the project root instead of specs/
FAILED tests/test_mutation_detector.py::test_spec_documents_open_gap_g1
  -> same spec-path lookup bug, different file (M10_MUTATION_DETECTOR_SPEC.md)
FAILED tests/test_yahoo_provider.py::test_only_yahoo_provider_references_yfinance
  -> edgar_provider.py's yfinance fallback trips a test asserting only
     yahoo_provider.py may reference yfinance (a real overlap, pre-existing,
     unrelated to insider_trade_flag)
```

Confirmed via isolated `-v` run (`pytest tests/test_data_validator.py::... tests/test_mutation_detector.py::... tests/test_yahoo_provider.py::... -v`): all three failure causes are exactly what's described above and traced to code/spec-path issues that predate this session entirely.

**Caveat on the exact pass count:** none of the three full-suite attempts printed a final `=== N passed, M failed in Xs ===` summary line — each run's captured output ends right after the three `FAILED` lines. Given this reproduced identically across three independent invocations (including one via `cmd`-native redirection, ruling out a PowerShell-specific pipe-capture bug), the most likely explanation is the machine's near-full `C:` drive (**~1.04 GiB free** at time of this check) — this exact class of "spurious full-suite behavior on a full C: drive" was already logged in project memory on 2026-07-04. It does not appear to affect correctness (all three runs agree on the same 3 failures and nothing else, and every targeted subset re-run below completed cleanly with a proper summary line), only the terminal's very last summary print. Freeing disk space is outside this task's scope; flagging rather than working around it.

Targeted subset runs (all completed cleanly, full summary lines, zero regressions beyond the 3 pre-existing failures above): `test_fmp_insider_provider.py` (22→24 after item 4's addition), `test_pipeline_insider_flag_wiring.py` (13→14), `test_pipeline_orchestrator.py`, `test_phase6_orchestrator.py`, `test_p2_5_orchestration_wiring.py`, `test_orchestrator_config_loading.py`, `test_config_service.py`, `test_config_recommender.py`, `test_p2_6_valuation_band_price_lookup.py`, `test_pipeline_fundamentals_step.py`.

## 2. Live smoke test — CRITICAL FINDING: the endpoint is not accessible on this project's actual key

Using the real keyring-stored key (`stock_ai_platform` / `FMP_API_KEY`, confirmed present, 32-char value) and a raw `requests.get()` call (bypassing the MCP connector entirely):

```
GET https://financialmodelingprep.com/stable/insider-trading/search?symbol=AAPL&transactionType=P-Purchase&page=0&limit=5&apikey=***
-> HTTP 402
-> body: "Restricted Endpoint: This endpoint is not available under your
   current subscription please visit our subscription page to upgrade your
   plan at https://financialmodelingprep.com/"
```

This is **not** a URL-guessing miss — the URL is confirmed correct. Follow-up probes on the same key:

| URL | Status | Meaning |
|---|---|---|
| `/stable/insider-trading/search` | **402** | Route exists, gated behind a higher plan |
| `/stable/insider-trading/statistics` | **402** | Same |
| `/stable/acquisition-of-beneficial-ownership` | **402** | Same |
| `/stable/insider-trading/latest` | 200 | Works, but returns the already-confirmed-broken `"symbol": "NONE"` global feed — unusable for a ticker-keyed lookup (per the original investigation's anomaly #2) |
| `/stable/insider-trading-transaction-type` | 200 | Metadata-only, works |
| `/stable/profile` | 200 | Confirms the key itself is valid and general-tier endpoints work |
| `/api/v3/quote/AAPL`, `/api/v4/insider-trading` | 403 | Legacy endpoints, deprecated project-wide by FMP since 2025-08-31, unrelated to this key's tier |

**Conclusion: the specific endpoint `fmp_insider_provider.py` is built around (`insider-trading/search`) is not included in this project's actual FMP plan.** The original investigation's "confirmed real access, no gating observed" finding was obtained through this Claude environment's own FMP MCP connector — which is evidently on a different (higher) plan tier than the project's self-service key, and that distinction was not visible from inside the connector's abstraction. This was flagged as a possible gap in the original investigation report ("this doesn't imply the project has its own key") but the severity — full 402 lockout, not just an unconfirmed tier — was not something either investigation could have caught without this exact test.

**Consequence for the implementation:** the code already degrades correctly and safely — `_FmpHttpClient.get_json()` calls `response.raise_for_status()`, which raises on 402 (not in `_RETRYABLE_STATUS_CODES`, so it's not retried), the exception is caught by `fetch_insider_purchase_flag`'s try/except, logged as a warning, and `None` is returned per-ticker. **Nothing crashes.** But functionally, `insider_trade_flag` will never populate real data against this key today — every Step 5 run will log one warning per shortlisted ticker and leave the field `NULL`. This is a plan/business decision for the architect (upgrade the FMP plan, or reconsider the approach — see Track B below), not a code defect. No code was changed in response to this finding, per Track A's "no new code expected" framing and Track B's explicit "do not make real changes to Track A based on Track B's findings."

Field-name verification (deliverable (b) of item 2) could not be completed as originally scoped, since the endpoint never returned a body to inspect — but the field names were already independently cross-checked against a real raw SEC Form 4 XML in Track B (§B.4 below) and match what `fetch_insider_purchase_flag` expects conceptually (transaction type, form type, filing date, shares, price, acquired/disposed code all present under different but mappable names).

## 3. FMP rate limit — still unconfirmed, and now unreachable via headers too

No `X-RateLimit-*`, `Retry-After`, or similar headers were present on any response (successful or 402) from this key. FMP does not appear to expose rate-limit state in response headers. Actually confirming the plan's request-per-minute ceiling requires logging into the account dashboard at financialmodelingprep.com with the project owner's credentials — outside what this session can access (no browser session, no account login). **Unchanged from the original investigation: still unconfirmed.** Given item 2's finding, this is now moot for the `insider-trading/search` endpoint specifically until/unless the plan is upgraded to include it.

## 4. Defensive check on the ordering assumption — added

**Before this fix:** `_SQL_INSIDER_FLAG_UPDATE` was a plain `UPDATE ... WHERE ticker = ? AND as_of_date = ?` with no check on whether it actually matched a row. A ticker whose `ticker_fundamentals` row didn't exist yet (e.g. a per-ticker `fundamentals_refresh` failure earlier in the same run) would silently no-op — the lookup would run (spending one of the daily FMP calls), but the result would vanish with no trace.

**Fix applied:** confirmed DuckDB (project version 1.5.3, `duckdb>=1.0` per `requirements.txt`) supports `UPDATE ... RETURNING` — verified with a standalone script before touching production code. Changed `_SQL_INSIDER_FLAG_UPDATE` to `UPDATE ticker_fundamentals SET insider_trade_flag = ? WHERE ticker = ? AND as_of_date = ? RETURNING ticker`, and `_refresh_insider_flags` now checks `.fetchall()`: an empty result logs a warning (`"insider_trade_flag computed for ticker=%s but no ticker_fundamentals row exists..."`) and moves on to the next ticker, rather than disappearing silently. Still never fails Step 5 — this remains purely informational.

New test `test_zero_row_update_logs_warning_not_silent` in `tests/test_pipeline_insider_flag_wiring.py` covers this directly (a `_FakeDb` extension simulates a missing row for one ticker in a two-ticker batch, confirms the warning fires for exactly that ticker and the other ticker's update still succeeds). Full updated test files: 24 + 14 = 38 tests, all passing.

No commit made for this addendum either — the `RETURNING` fix is a real code change (small, defensive, as scoped) sitting in the working tree alongside everything else, awaiting review together.

