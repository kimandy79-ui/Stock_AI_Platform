# FMP `insiderTrades` Investigation — `insider_trade_flag`

**Date:** 2026-07-18
**Scope:** Investigation + access confirmation only, per coder note. No implementation code written. No commit.

---

## 1. API key / access status

**The local codebase has no FMP access configured.**

- `grep -ri FMP` across the entire repo: zero matches (no `.env` reference, no `config/provider` seed, no code reference).
- `.env` on disk is byte-identical to `.env.example` — it holds no real secrets at all today, only `DATA_DIR`, `LOG_LEVEL`, `LOG_TO_FILE`, `DEFAULT_STRATEGY`, `SEC_USER_AGENT`. `.env.example`'s own trailing comment states the project convention explicitly: *"Provider/API credentials are managed via keyring / Windows Credential Manager in later modules, NOT via `.env`. Do not place secrets here."*
- `app/config/env.py` (Module 01) has no FMP-specific getter; it's a generic typed `.env` reader.

**What ran the test calls below is the FMP connector available in this Claude conversation environment** (`mcp__claude_ai_FMP__insiderTrades`) — this is separate from the local codebase and does not imply the project has its own key. Per the coder note's instruction, I did not attempt to obtain a key myself. **Architect action needed:** sign up for an FMP API key and, per the project's existing convention, store it in keyring / Windows Credential Manager (not `.env`), mirroring how other provider credentials are meant to be handled (note `SEC_USER_AGENT` is the one exception already in `.env`, justified there as non-secret public contact info — an FMP key is a real credential and should not follow that exception).

Caveat: because I only have access to the environment's connector, I cannot confirm what plan tier a **project-owned, freshly-signed-up** FMP key would need to reproduce the access shown below. Re-verify tier/limits once a real project key exists.

---

## 2. Real test-call results

Ran against `mcp__claude_ai_FMP__insiderTrades` (endpoint `search-insider-trades` unless noted).

### Response shape (confirmed empirically, `search-insider-trades` / `latest-insider-trade`)

```json
{
  "symbol": "AAPL",
  "filingDate": "2026-06-17",
  "transactionDate": "2026-06-15",
  "reportingCik": "0001780525",
  "companyCik": "0000320193",
  "transactionType": "M-Exempt",
  "securitiesOwned": 210728,
  "reportingName": "Newstead Jennifer",
  "typeOfOwner": "officer: SVP, GC and Secretary",
  "acquisitionOrDisposition": "D",
  "directOrIndirect": "D",
  "formType": "4",
  "securitiesTransacted": 30104,
  "price": 0,
  "securityName": "Restricted Stock Unit",
  "url": "https://www.sec.gov/Archives/edgar/..."
}
```

`all-transaction-types` returned the full SEC transaction-code vocabulary (18 codes: `A-Award`, `C-Conversion`, `D-Return`, `E-ExpireShort`, `F-InKind`, `G-Gift`, `H-ExpireLong`, `I-Discretionary`, `J-Other`, `L-Small`, `M-Exempt`, `O-OutOfTheMoney`, `P-Purchase`, `S-Sale`, `U-Tender`, `W-Will`, `X-InTheMoney`, `Z-Trust`).

### Tier / access

Tested `AAPL`, `NVDA`, `GME` across `search-insider-trades` (unfiltered, `transactionType=P-Purchase` filtered), `latest-insider-trade`, `insider-trade-statistics`, `all-transaction-types`. **All calls returned real, populated data with no gating, no truncation notice, and no paid-tier error** — at least on the connector available in this environment. No evidence in the tool description or responses that this endpoint is paid-tier-only.

### Rate limits

**Not observable from this test.** A handful of sequential calls in one session tells you nothing about a per-minute/per-day ceiling. Not something to infer from documentation alone (the same instruction this note gave for confirming tier) — it needs to be checked against the actual plan dashboard once a project-owned key exists.

### Historical depth

Deep. `search-insider-trades` with `transactionType=P-Purchase` for `AAPL` returned open-market purchases back to **2006-11-19** (filed 2007-10-26) with no apparent cutoff. `insider-trade-statistics` for `AAPL` returned quarterly aggregates back to **2003 Q2**. This comfortably covers the platform's current backfill window (2025-06-02 onward, per `prod_rebuild_2026_07_14` memory) and any plausible future backfill extension.

---

## 3. Anomalies (verbatim, flagged rather than papered over)

1. **The `date` query parameter does not filter results.** Calling `search-insider-trades` for `AAPL` with `date=2024-01-01` returned the *identical* top-5 rows (all `filingDate: 2026-06-17`) as the same call with no `date` param at all. This is significant: it means point-in-time scoping **cannot** be delegated to a server-side date filter and must be done client-side (see §5).

2. **`latest-insider-trade` returns `"symbol": "NONE"`** on every row tested (5/5), even though `companyCik` and `reportingCik` are populated with real values (e.g. CIK `0001567892`). This endpoint is unusable as a ticker-keyed source — do not use it for this feature; `search-insider-trades` filtered by `symbol` is the correct endpoint, which does correctly populate `symbol` (confirmed for `AAPL`, `NVDA`, `GME`).

3. **One `NVDA` row had `transactionType: ""` and `acquisitionOrDisposition: ""` (empty strings) with `formType: "3"`.** Form 3 is an *initial* statement of beneficial ownership (filed when someone becomes an insider) — it carries no transaction, so `transactionType`/`acquisitionOrDisposition` are legitimately blank. Any consumer of this data **must filter to `formType == "4"`** (the actual transaction-report form); Form 3/5 rows will otherwise silently corrupt transaction-type-based logic.

4. **No 10b5-1 plan indicator field exists anywhere in the observed response shape.** The coder note asked whether scheduled-plan trades are identifiable — they are not, from this endpoint. See §5 for how the proposed flag definition sidesteps this rather than fabricating a workaround.

---

## 4. Proposed integration architecture

**Recommendation: a narrow injected lookup, not a new full `MarketDataProvider`.**

Mirror the existing `price_lookup: Callable[[str, date], float | None] | None` pattern already used by `EdgarFundamentalsProvider` (`app/providers/edgar_provider.py:754-758, 788, 811, 891`) for `valuation_band`. Concretely:

- New module `app/providers/fmp_insider_provider.py` exposing one function, e.g. `fetch_insider_purchase_flag(ticker: str, as_of_date: date, fetch_json: Callable) -> bool | None`, doing the paginate-and-filter work described in §5. No class, no `MarketDataProvider` subclass — it has exactly one capability, and `MarketDataProvider` requires implementing `get_price_history`/`list_symbols`/`get_earnings`/`get_fundamentals`, three of which would be permanent `unsupported_capability` stubs for zero benefit (that's exactly the shape `EdgarFundamentalsProvider` already has to carry for being "fundamentals-only," and there's no reason to repeat that ceremony for a one-field lookup).
- `EdgarFundamentalsProvider.__init__` gains an `insider_lookup: Callable[[str, date], bool | None] | None = None` constructor parameter, injected exactly like `price_lookup` — omitted by default (`insider_trade_flag` stays `None`, current behavior unchanged unless explicitly wired).
- `compute_fundamentals_from_companyfacts(...)` gains an `insider_flag: bool | None = None` parameter and passes it straight to the `FundamentalSnapshot(insider_trade_flag=insider_flag, ...)` field, replacing the current hardcoded `None` — parallel to how `price` already flows into `compute_valuation_band`.
- `get_fundamentals()` calls `self._insider_lookup(ticker, as_of_date) if self._insider_lookup else None`, same shape as the existing `self._price_lookup(...)` call one line above it.

Rejected alternative: a second full `FMPInsiderProvider(MarketDataProvider)` with its snapshot merged into EDGAR's at the M20 orchestration layer. This would require new field-by-field snapshot-merge logic that doesn't exist anywhere in the codebase today, for no actual benefit — the callable-injection pattern is already established, proven (P2.4 shares_outstanding took the same injected-dependency shape, just via constructor field not callable), and keeps `compute_fundamentals_from_companyfacts` as the single point of snapshot assembly.

---

## 5. Proposed "flag" definition

**Flag = at least one open-market insider *purchase* in the trailing N days as of `as_of_date`, above a minimum dollar threshold.**

- **Transaction types included:** `formType == "4"` AND `transactionType == "P-Purchase"` only. Excludes every other code (`A-Award`, `F-InKind`, `M-Exempt`, `G-Gift`, `C-Conversion`, `S-Sale`, etc.) — these are grants, tax-withholding share surrenders, option exercises, gifts, and sales, none of which carry the same real-money conviction signal as an open-market buy. This matches the coder note's own citation of the literature (sales are noisy/routine/plan-driven; purchases are the supported signal).
- **Lookback window:** propose a new config knob (no existing lookback constant to reuse — nothing comparable exists in `fundamentals_quality.py` today), not a hardcoded value. A 90-day trailing window is a reasonable starting default per common swing-trading literature use, but per this project's own standing rule ("Config threshold tuning — deferred until diagnostics provide empirical signal"), it should ship as a config value, not tuned now.
- **Minimum dollar threshold:** propose `securitiesTransacted * price >= min_transaction_value_usd`, also a config knob (not hardcoded), to exclude token/optics purchases (e.g., a director buying 100 shares).
- **10b5-1 exclusion:** **not directly identifiable** — confirmed anomaly #4 above, no plan-indicator field exists in this data. However, restricting to `P-Purchase` substantially sidesteps the problem by construction: 10b5-1 scheduled-plan mechanics are overwhelmingly used for *sales* (predictable diversification), not purchases — an insider rarely pre-schedules an open-market buy months in advance. This is a mitigation, not a guarantee; document it as a known limitation rather than claiming false precision.

---

## 6. Point-in-time integrity

The `date` param cannot be trusted (anomaly #1), so scoping must be entirely client-side, mirroring `edgar_provider.py`'s existing filed-date discipline (`extract_annual_series`, `extract_shares_outstanding`):

1. Call `search-insider-trades` with `symbol=<ticker>`, `transactionType=P-Purchase`, paginating via `page`.
2. Results are observed sorted descending by `filingDate`/`transactionDate` (confirmed across all test calls — newest first, consistently, back through 20 years of AAPL history).
3. Filter client-side to rows where **`filingDate <= as_of_date`** — use `filingDate` (SEC-filed date), not `transactionDate`, as the knowability gate. A Form 4 must be filed within 2 business days of the transaction, but "within 2 days" is a ceiling, not a guarantee; a transaction dated before `as_of_date` may not have been *filed* (and thus knowable) until after it. This exactly parallels the dual `end`/`filed` check `extract_annual_series` already applies in `edgar_provider.py:301-303`.
4. Stop paginating once `filingDate` falls below `as_of_date - lookback_window_days` — no need to walk a ticker's entire multi-year history on every call.
5. Historical depth (§2) confirms this works retroactively for the platform's full backfill range, unlike the yfinance fallback's current-only restriction (`fallback_can_serve`) — no equivalent staleness gate is needed here.

---

## 7. Summary for architect review

| Question | Answer |
|---|---|
| Local FMP key exists? | No — zero references anywhere in repo/`.env` |
| Real access confirmed? | Yes, via this environment's connector; re-verify tier once a project key is issued |
| Response shape usable? | Yes, `search-insider-trades` (not `latest-insider-trade` — broken `symbol` field) |
| Historical depth sufficient? | Yes, back to 2003-2006 for AAPL, well past current backfill needs |
| `date` param filters as expected? | **No — confirmed broken, client-side filtering required** |
| Integration shape | Narrow injected `Callable`, mirroring `price_lookup`, not a new full provider |
| Flag definition | `P-Purchase` + `formType=4` only, trailing-N-day window, min $ threshold (both config, not hardcoded) |
| 10b5-1 exclusion | Not directly identifiable in the data; `P-Purchase`-only restriction mitigates but doesn't guarantee |
| Point-in-time safe? | Yes, via `filingDate <= as_of_date` client-side filtering — same discipline as existing EDGAR code |

No code written. Awaiting architect review before any implementation coder note is issued.
