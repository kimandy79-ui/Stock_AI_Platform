# SEC-EDGAR-Native `insider_trade_flag` Implementation — 2026-07-18

**Scope:** Replace the FMP-based approach entirely with a SEC-EDGAR-native one, computed at Step 4 scale (~1,500 tickers) alongside the other 5 EDGAR fundamentals fields. **No commit** — diff delivered for review, per current policy.

**Headline finding (Section 3 below): the real batch measurement came in at ~82 minutes extrapolated to full scale — about 1.8x the earlier arithmetic estimate (~46 minutes). Read that section before treating this as ready for unconditional full-scale rollout.**

---

## 1. Removal — FMP-based code

- Deleted `app/providers/fmp_insider_provider.py`.
- Deleted `tests/test_fmp_insider_provider.py` and `tests/test_pipeline_insider_flag_wiring.py`.
- `pipeline_orchestrator.py`: removed `insider_flag_lookup` constructor param, `_insider_flag_config`/`_resolve_insider_flag_lookup`/`_refresh_insider_flags` methods, `_SQL_SELECTED_STEP5_TICKERS`/`_SQL_INSIDER_FLAG_UPDATE` SQL constants, and the `_step_step5` restructuring that ran the Step-5-shortlist-only pass. `_step_step5` is back to its pre-insider-flag form (two optional passes: `fundamentals_scores`, `ai_review_scores`).
- `default_configs.py`: removed `DEFAULT_RUNTIME_CONFIGS["pipeline"]["auto_invoke_insider_flag"]`, replaced with `compute_insider_flag` (see §5) — same kill-switch *purpose*, now gating a Step-4-time computation instead of a Step-5-shortlist one.
- Confirmed via grep: zero remaining imports/references to the FMP module anywhere in `app/` or `tests/`; the only surviving mentions are in the historical `reports/fmp_*` files and this report's own explanatory docstrings (deliberate, for context).

## 2. New module: `app/providers/edgar_insider_provider.py`

`fetch_insider_purchase_flag(ticker, as_of_date, cik, fetch_json, fetch_filing_xml, *, lookback_days=90, min_transaction_value_usd=10_000.0, exclude_10b5_1=True, max_candidate_filings=50) -> bool | None`

Implementation, per the Track B investigation's confirmed findings:

1. **Cheap short-circuit**: fetches `data.sec.gov/submissions/CIK{cik}.json`, checks `insiderTransactionForIssuerExists`. `False` → immediate `False`, zero further requests.
2. **Exact `form == "4"` matching** against `filings.recent`'s parallel arrays — deliberately *not* the older `browse-edgar?type=4` interface, which the investigation confirmed does prefix-matching and pulls in `"425"`/`"424B2"` alongside real Form 4s.
3. **Point-in-time gate**: `filingDate` (SEC-filed date) in `(as_of_date - lookback_days, as_of_date]` — never `transactionDate`, mirroring `edgar_provider.py`'s `extract_annual_series` dual end/filed-date discipline.
4. **Per candidate Form 4**: fetches the raw XML, checks `transactionCoding/transactionCode == "P"` in both `nonDerivativeTable` and `derivativeTable` (FMP's flat response never distinguished the two, so neither does this), computes `shares * price`, checks the threshold.
5. **10b5-1 exclusion, implemented as a real filter this time** (not a documented caveat, per the architect's explicit ask): checks the filing's document-level `<aff10b5One>` field; a truthy value excludes the whole filing's transactions from counting. This is the concrete upgrade the SEC-native source enables over the FMP-based approach, which had no equivalent field anywhere in its response shape.
6. **Early exit** on the first qualifying purchase found (newest-first order) — doesn't fetch remaining candidates once satisfied.
7. **True/False/None trichotomy preserved**: `None` only on a genuine retrieval failure; a malformed/unparseable individual filing degrades to "no match in this filing," not a whole-ticker failure.
8. **`max_candidate_filings=50` safety cap** — bounds worst-case per-ticker request cost. See §6 anomaly notes: this cap was actually reached in the real batch run (WMT).

No on-disk cache (unlike the removed FMP module) — deliberately not added. SEC has no daily call quota (unlike FMP's 250/day), only a rate limit, so caching's main benefit (avoiding budget exhaustion) doesn't apply, and `_step_fundamentals`'s existing "already refreshed today" guard already prevents redundant same-day mass runs. Not adding an unrequested feature.

## 3. Rate limiting — shared client, and the required real measurement

**Shared client, as instructed.** `edgar_provider.py`'s `_SecHttpClient` gained a `get_text` method (raw-text GET, for Form 4 XML) alongside the existing `get_json`, refactored to share one `_get()` core so both request shapes go through the *same* throttle/retry state (`_TARGET_REQUESTS_PER_SEC = 6.0`, unchanged) on the *same* session. A new public factory, `build_sec_http_client(...)`, lets `pipeline_orchestrator.py` build exactly one client and hand its bound methods to both `EdgarFundamentalsProvider`'s `fetch_json` and the injected `insider_lookup` — confirmed by a dedicated test (`test_pipeline_fundamentals_insider_wiring.py::TestResolveFundamentalsProviderSharesOneHttpClient`) that both reach the identical fake client instance.

### Real ~50-ticker batch measurement (not the earlier arithmetic estimate)

Ran `fetch_insider_purchase_flag` against 50 real tickers (30 large-caps + 20 mid/small-caps, listed in the script) using the project's real `SEC_USER_AGENT`, real SEC EDGAR endpoints, real throttle (6 req/s). Full per-ticker output captured; summary:

```
TOTAL wall-clock: 164.34s for 50 tickers (0 no-CIK)
Average: 3.287s/ticker, 18.44 requests/ticker
Short-circuited (1 request, no Form-4 history): 1/50 (2.0%)
Extrapolated to 1,500 tickers @ this rate: 82.2 minutes
```

**This is materially worse than the earlier ~46-minute arithmetic estimate, for two measured reasons:**

1. **The `insiderTransactionForIssuerExists` short-circuit barely fires for real, actively-traded tickers.** Only 1/50 (XOM — see anomaly note below) short-circuited. The estimate's implicit assumption that a meaningful fraction of Step-4's ~1,500-ticker pool would skip at 1 request was wrong for large/mid-caps; it likely only helps genuinely dormant, newly-listed, or thinly-followed names, which are underrepresented in a 30-large-cap/20-mid-cap sample but may be more common across the platform's full universe (a mix the sample doesn't capture — see the "what wasn't measured" note below).
2. **Real Form-4-filing volume per ticker is much higher than assumed.** The estimate assumed ~10 filings/ticker in a 90-day window; the real average was **18.44 requests/ticker** (≈17.44 Form-4 fetches, since each ticker also spends 1 request on `submissions.json`). Some large-caps with many insiders filing routinely (RSU vesting, tax-withholding sales, etc.) hit far higher volumes: WMT (51 requests, hit the 50-filing cap), META (46), GOOGL (46), NFLX (44), ETSY (37).

**Extrapolating from real per-ticker averages, not the arithmetic model: ~82 minutes for the full ~1,500-ticker Step 4 pool.** This is a real, substantial addition to `fundamentals_refresh`'s runtime — previously a lightweight step (one `companyfacts` fetch per ticker, no comparable per-filing fan-out). **Flagging this as the "problematic" case the coder note anticipated**, since 82 minutes added to one pipeline step is a meaningful chunk of a daily run, not a rounding error.

**Proposed option, per the coder note's own suggestion — not implemented here, awaiting a decision:** decouple the insider-check from `fundamentals_refresh`'s critical path into a separate, later, lower-priority step that can be skipped or deferred without blocking the other 5 fundamentals fields (which remain fast, one-request-per-ticker). This was flagged as an acceptable option in the coder note itself; implementing it is a further architecture change beyond what Section 6 asked for ("measure and report"), so it is *not* done in this pass. The current diff still wires the lookup directly into `_step_fundamentals`'s existing per-ticker loop, exactly as Section 4 instructed — this is a known, reported tradeoff, not silently left unaddressed.

**What wasn't measured:** the 50-ticker sample deliberately skewed toward well-known, actively-traded names (to have >0 real Form 4 activity to observe) — it does not represent the platform's full ~1,500-ticker active universe, which likely includes a meaningfully higher share of small-caps/thinly-followed names that would short-circuit more often and pull the true full-scale average down somewhat. The 82-minute figure should be read as a plausible upper-middle estimate, not a worst-case ceiling (WMT-style high-Form-4-frequency names exist but aren't the majority) nor a confirmed lower bound (the sample's large-cap skew likely overstates the true average somewhat). A production dry run against the real active-ticker table would sharpen this further; not done here, out of scope for this pass.

## 4. Integration into `EdgarFundamentalsProvider`

Wired the way the *original* coder note asked, now that the Step-4-time call site is correct:

1. `EdgarFundamentalsProvider.__init__` gained `insider_lookup: Callable[[str, date, str], bool | None] | None = None` — same optional-injection shape as `price_lookup`, defaults to `None` (flag stays `None` unless explicitly wired).
2. `compute_fundamentals_from_companyfacts(...)` gained `insider_flag: bool | None = None`, threaded straight into `FundamentalSnapshot(insider_trade_flag=insider_flag, ...)`.
3. `get_fundamentals()` calls a new `_safe_insider_lookup(ticker, as_of_date, cik, log)` helper, **not** inlined directly in the same try block as `price_lookup`. This is a deliberate deviation from mirroring `price_lookup`'s exact call site, reasoned as follows: `price_lookup` raising already cascades into the yfinance fallback (existing P2.6 behavior) — acceptable there since valuation_band gracefully degrades to `"unknown"` either way. Doing the same for `insider_lookup` would mean a transient SEC EDGAR insider-check failure discards the other 5 *already-successfully-computed* EDGAR fields and forces a yfinance re-fetch that has **zero** `piotroski_f_score`/`altman_z_score` coverage — a strictly worse outcome caused by a failure in a field both coder notes are explicit is purely informational and must never fail anything. `_safe_insider_lookup` catches its own exceptions and returns `None`, isolated from the main SEC-path try/except.
4. Confirmed via test (`test_edgar_provider_insider_lookup.py::test_lookup_failure_does_not_fail_the_whole_fundamentals_call`) that a raising `insider_lookup` still yields a snapshot with `source_provider == "sec_edgar"` (not `"yfinance_fallback"`) and the other fields intact.
5. This runs within the *same* `fundamentals_refresh` step's existing per-ticker loop, per instruction — not a second pass. (See §3 for the runtime consequence of that choice.)
6. **Yfinance fallback path untouched** (still hardcodes `insider_trade_flag=None`) — not requested this time either; noted as an unchanged, pre-existing gap, not a new decision.

`app/providers/provider_interface.py` — no change needed; `FundamentalSnapshot.insider_trade_flag: bool | None` already existed.

## 5. Config + operational controls

1. `risk_label_config.fundamentals` kept `insider_purchase_lookback_days` (90) and `min_insider_transaction_value_usd` (10000.0) as-is, added `exclude_10b5_1: True` alongside them — all three read via `PipelineOrchestrator._insider_flag_config`.
2. **Runtime kill-switch, renamed and re-scoped**: `DEFAULT_RUNTIME_CONFIGS["pipeline"]["compute_insider_flag"]`, default `True`. Read via `_pipeline_flags` (extended from 2 to 3 flags) and consulted by a new `_build_insider_lookup(db_role, log, http_client)` helper, which returns `None` (disabling the lookup entirely — `EdgarFundamentalsProvider` then behaves exactly as if `insider_lookup` were never injected) when the flag is off. Given §3's runtime finding, this kill-switch is more load-bearing than originally anticipated — an operator now has a real, concrete reason to reach for it (SEC-side slowdowns, or wanting to shed ~82 minutes from a specific run) rather than a hypothetical one.

## 6. Testing

Three new test files, 37 tests total, all offline (fake `fetch_json`/`fetch_filing_xml`/config services throughout — no real network calls in automated tests):

**`tests/test_edgar_insider_provider.py`** (17 tests) — the core lookup function:
- `insiderTransactionForIssuerExists=False` → immediate `False`, zero filing fetches.
- Qualifying purchase, in-window, above threshold, non-10b5-1 → `True`.
- `aff10b5One=True` → excluded (and the inverse: `exclude_10b5_1=False` lets it through).
- `"425"`/`"424B2"` rows mixed with a real `"4"` row → don't corrupt the result, and never trigger a filing-content fetch (confirmed via fetched-URL count, not just the boolean result) — regression test for the confirmed `browse-edgar` prefix-matching anomaly, kept as a safety net even though `submissions.json` itself doesn't have that bug.
- `filingDate > as_of_date` excluded (point-in-time integrity).
- Outside-lookback-window excluded; below-threshold → `False`; non-`"P"` code excluded.
- Derivative-table purchases also count.
- `submissions.json` fetch failure, filing-XML fetch failure → `None`, not a propagated exception.
- Malformed XML → `False` for that filing (doesn't crash, doesn't abort the ticker).
- No Form-4 candidates at all → `False`, zero filing fetches.
- Early exit after the first qualifying purchase (fetched-count assertion).
- CIK zero-padding in the constructed URL.
- `max_candidate_filings` cap actually bounds the request count.

**`tests/test_edgar_provider_insider_lookup.py`** (10 tests) — `EdgarFundamentalsProvider` integration:
- `insider_flag` threads through `compute_fundamentals_from_companyfacts` (`True`/`False`/absent, `False` not coerced to `None`).
- Injected `insider_lookup` populates `FundamentalSnapshot.insider_trade_flag`, receives the correct `(ticker, as_of_date, cik)` args.
- Omitted `insider_lookup` preserves the pre-P2.7 `None` behavior (regression pin).
- A raising `insider_lookup` doesn't fail the whole `get_fundamentals()` call or trigger the yfinance fallback (§4 point 3, directly exercised).
- `build_sec_http_client` returns a working client; a `requests_mock`-backed test confirms `get_json`/`get_text` really do share one session (real HTTP-shaped test, not just a unit-level mock).

**`tests/test_pipeline_fundamentals_insider_wiring.py`** (10 tests) — orchestrator wiring:
- `compute_insider_flag` defaults `True` on a config-read failure; reads an explicit `False` override.
- `_insider_flag_config` reads all three thresholds from `risk_label_config`, falls back to module defaults on failure.
- `_build_insider_lookup` returns `None` when disabled; returns a working closure when enabled, confirmed to reach the *same* shared fake HTTP client instance.
- The closure passes configured thresholds through to `edgar_insider_provider.fetch_insider_purchase_flag` (verified via a monkeypatched spy).
- `_resolve_fundamentals_provider`: injected provider used verbatim (regression pin, mirrors the pre-existing test); default-built provider's `fetch_json` and `insider_lookup` both trace back to one shared client instance; `compute_insider_flag=False` leaves `provider._insider_lookup` as `None`.

**Full regression run** (all passing, zero failures beyond the same 3 pre-existing/unrelated ones already logged before this session — `test_data_validator.py::test_spec_documents_open_gaps_not_invented`, `test_mutation_detector.py::test_spec_documents_open_gap_g1`, `test_yahoo_provider.py::test_only_yahoo_provider_references_yfinance`):

```
tests/test_edgar_insider_provider.py .................              (17 passed)
tests/test_edgar_provider_insider_lookup.py ..........               (10 passed)
tests/test_pipeline_fundamentals_insider_wiring.py ..........        (10 passed)
tests/test_edgar_provider.py ................................       (32 passed)
tests/test_pipeline_fundamentals_step.py ............                (12 passed)
tests/test_p2_6_valuation_band_price_lookup.py .....................  (21 passed)
tests/test_edgar_provider_resilience.py .......................       (23 passed)
tests/test_p2_4_shares_market_cap.py ......................... x2     (57 passed)
tests/test_pipeline_orchestrator.py, test_phase6_orchestrator.py,
test_p2_5_orchestration_wiring.py, test_config_service.py             (all passed, 7 pre-existing unrelated skips)
```

All modules (`edgar_provider.py`, `edgar_insider_provider.py`, `pipeline_orchestrator.py`) confirmed to import cleanly.

## 7. Anomalies (verbatim, for review)

1. **The real batch measurement (~82 min extrapolated) is meaningfully worse than the earlier arithmetic estimate (~46 min)** — see §3 in full. This is the most important finding in this report; do not treat the earlier estimate as validated.
2. **`max_candidate_filings=50` was actually reached in the real run** (WMT: 51 total requests = 1 `submissions.json` + 50 Form-4 fetches, capped). Since results are newest-first, the cap only risks missing a qualifying purchase among the *oldest* filings still inside the lookback window for an unusually high-Form-4-frequency issuer — a bounded, directionally-favorable tradeoff (staler signal is lower priority to miss), but a real one, not zero-risk.
3. **XOM's resolved CIK looks suspicious**: `0002115436` — a very recently-assigned CIK number, inconsistent with ExxonMobil's actual real-world CIK (a low, decades-old number). This ticker was also the *only* one of 50 that short-circuited (`insiderTransactionForIssuerExists: False`), consistent with the resolved CIK belonging to a different, much younger entity than the real ExxonMobil. This points to a possible ticker-symbol collision or staleness in `EdgarFundamentalsProvider`'s existing `_default_ticker_to_cik`/`company_tickers.json` cache resolution — **pre-existing code, not introduced by this change**, and out of scope to fix here, but flagged since it directly affected this measurement's short-circuit-rate observation (the true short-circuit rate for real, correctly-resolved large-caps may be closer to 0/50 than 1/50 shown).
4. The short-circuit optimization (`insiderTransactionForIssuerExists`) provides much less benefit than hoped for the ticker mix that matters most (actively-traded names) — see §3. It's not worthless (a real cost-avoidance for genuinely dormant/new tickers), just not the load-bearing optimization the earlier estimate implicitly assumed.

No commit made. Diff is ready for review, pending a decision on §3's runtime finding.
