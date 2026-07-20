# SEC EDGAR Issuer-Keyed Ownership Lookup — Investigation

**Date:** 2026-07-18
**Scope:** Investigation only, per coder note. No code modified. Independent of Track A's FMP implementation — findings here do not imply or require any change to the delivered `fmp_insider_provider.py`/`pipeline_orchestrator.py` work; that's a separate future architect decision.

**Bottom line up front: GO.** SEC EDGAR's own `data.sec.gov/submissions/CIK##########.json` reliably returns issuer-keyed Form 4 filings — the exact thing `edgar_provider.py`'s original docstring said it couldn't verify with confidence. Tested against three real tickers with real, current filings; cross-checked against the FMP investigation's own captured data (same accession numbers, same transactions). Rate/scope arithmetic comfortably supports Step-4 scale (~1,500 tickers/day) within SEC's fair-access limit. This is a credible candidate to replace or supplement the FMP/Step-5-only approach — but that's the architect's call, not something implemented here.

---

## 1–2. `browse-edgar?owner=include` — works, but has a real quirk

Tested `https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=<issuer_CIK>&type=4&dateb=&owner=include&count=40&output=atom` for AAPL (CIK 0000320193), NVDA (CIK 0001045810), GME (CIK 0001326380) — the three tickers the original FMP investigation used, so results are directly cross-checkable — via the project's existing `resolve_sec_user_agent()` and a compliant `requests.Session` (same header discipline as `edgar_provider.py`'s `_SecHttpClient`, no bare requests).

**It works as issuer-keyed data.** AAPL's feed returned a Form 4 with `accession-number: 0001140361-26-025622`, `filing-date: 2026-06-17` — this is the **exact same filing** the FMP investigation captured for AAPL (`"url": ".../000114036126025622/..."`). NVDA and GME matched identically. This directly contradicts `edgar_provider.py`'s module docstring claim that resolving "which Form 4 filings reference this issuer" needs full-text search with unverifiable query semantics — `browse-edgar?owner=include` does exactly that, straightforwardly, and the results are provably correct against independently-sourced FMP data for the same filings.

**Real anomaly found:** `type=4` does **prefix matching, not exact matching**. GME's `type=4` query returned `Counter({'425': 24, '4': 15, '424B2': 1})` — form types `425` (business-combination prospectus) and `424B2` (prospectus supplement) both start with the character `4` and got pulled in alongside real Form 4s. Any consumer of this endpoint **must filter client-side to `term == "4"` exactly** — the same class of "don't trust the server-side filter" lesson the FMP investigation already taught with its `date` param.

## 3. `data.sec.gov/submissions/CIK##########.json` — the better of the two, no caveats

Tested the same three CIKs against `https://data.sec.gov/submissions/CIK0001326380.json` (GME shown; AAPL/NVDA checked too with the same shape). This is unambiguously the better approach:

- **Exact form-type matching, no prefix bug.** `filings.recent.form` is a plain array of exact strings — `Counter(...)` on GME's array shows `'4': 425` (real Form 4s) counted completely separately from `'425': 24` (business-combination filings) and `'4/A': 1` (Form 4 amendment) — no manual disambiguation needed beyond `form == "4"`.
- **Confirms issuer-keyed data directly**, same as `browse-edgar`: a Form 4 entry in GME's `filings.recent` (`accessionNumber: 0001990547-26-000014`, `filingDate: 2026-07-06`) matches the exact filing FMP's investigation captured for GME (`reportingCik: 0001990547`, same date).
- **A cheap top-level pre-check exists**: `insiderTransactionForIssuerExists` (boolean) is present at the JSON root — a company with zero insider-ownership history ever recorded can be skipped before spending any further requests. (Not exercised in this investigation since all 3 test tickers are large, active companies, but noted as a plausible per-ticker cost optimization for a real implementation.)
- **Clean, native JSON** — every field needed to construct the next fetch (`accessionNumber`, `filingDate`, `primaryDocument`) is already present, no HTML/atom parsing required at all.
- **One caveat, not a blocker**: SEC's own documentation describes `filings.recent` as covering roughly the most recent ~1,000 filings or one year (whichever is larger) before falling back to a paginated `filings.files` array. For a 90-day lookback window on a normal-filing-frequency company this is a non-issue; flagged for completeness, not as something that failed in testing.

**Conclusion for item 3: `data.sec.gov/submissions/CIK##########.json` is the right approach, not `browse-edgar`'s atom feed.** It's already the same JSON-API generation `edgar_provider.py` uses for `companyfacts` (`data.sec.gov/api/xbrl/companyfacts/...`), so it fits the existing code's established pattern (`_SecHttpClient`, `fetch_json` injection) with zero new HTTP idioms to introduce.

## 4. Extracting real fields from a raw Form 4 filing — confirmed, and better than FMP's shape

Followed one real filing end-to-end: GME's `accessionNumber: 0001990547-26-000014` → `https://www.sec.gov/Archives/edgar/data/1326380/000199054726000014/index.json` (SEC's own machine-readable directory listing, confirming exactly which files exist in the filing) → fetched the listed `wk-form4_1783383460.xml` directly (the raw ownership-document XML, not the human-readable XSLT-rendered version).

The raw XML is fully structured and gives every field `fmp_insider_provider.py` needs, under SEC's own (differently-named but directly mappable) schema:

| `fmp_insider_provider.py` field | SEC Form 4 XML equivalent |
|---|---|
| `formType` | `documentType` (top-level, e.g. `"4"`) |
| `symbol` | `issuer.issuerTradingSymbol` (reliably populated — no `"NONE"` bug) |
| `transactionType` | `transactionCoding.transactionCode` (single-letter SEC code, e.g. `"S"`, `"P"` — same vocabulary FMP's `P-Purchase`/`S-Sale` is built from, just without the trailing `-Purchase`/`-Sale` label) |
| `filingDate` | not in the transaction itself — comes from the parent `submissions.json`/`index.json` entry, same as FMP separates `filingDate` from the transaction record |
| `securitiesTransacted` | `transactionAmounts.transactionShares.value` |
| `price` | `transactionAmounts.transactionPricePerShare.value` |
| `acquisitionOrDisposition` | `transactionAmounts.transactionAcquiredDisposedCode.value` (`"A"`/`"D"`, identical vocabulary to FMP) |
| `directOrIndirect` | `ownershipNature.directOrIndirectOwnership.value` |
| `typeOfOwner` | `reportingOwner.reportingOwnerRelationship` (`isDirector`/`isOfficer`/`isTenPercentOwner`/`officerTitle` — actually **more structured** than FMP's single free-text string) |

**Bonus finding, directly relevant to the coder note's standing 10b5-1 gap:** the raw XML has a top-level `<aff10b5One>` field (`0` in the tested filing) — this is SEC's own **affirmative Rule 10b5-1(c) trading-plan indicator**, part of the official Form 4 XML technical schema. The tested filing also carried a `<footnotes>` block explicitly stating *"This sale does not represent a discretionary trade by the Reporting Person"* (a tax-withholding sale, unrelated to 10b5-1 specifically, but demonstrative of the kind of plain-language context SEC filings carry that a transaction-code-only feed like FMP's never exposes). **This means the SEC-native approach can solve the exact 10b5-1 caveat that both the original FMP investigation and the delivered implementation had to document as an unresolvable limitation** — FMP's response shape has no equivalent field anywhere. This alone is a meaningful data-quality argument for the SEC-native path beyond just cost/scale, independent of the FMP plan-access problem discovered in Track A.

## 5. Rate/scope arithmetic for Step-4 scale (~1,500 tickers/day)

Per ticker, two request types:

1. **One `submissions.json` fetch** to list recent filings and identify which accession numbers are Form 4s within the lookback window.
2. **One fetch per candidate Form 4's raw XML** to read its transaction details (submissions.json's `filings.recent` array does not carry transaction-level data — type/shares/price live only inside each filing's own document, same limitation FMP's aggregate list doesn't have, since FMP's `search` endpoint returns transaction-level rows directly in one paginated call). Based on the original investigation's real AAPL/GME data, an active large-cap issuer files roughly 5–20 Form 4s across all its insiders in a 90-day window; call it **~10 average**.

**Total ≈ 11 requests/ticker.** At Step-4 scale: `1,500 × 11 ≈ 16,500 requests/day`. SEC's published fair-access ceiling is ~10 req/s; this project's own `edgar_provider.py` already throttles to a conservative `_TARGET_REQUESTS_PER_SEC = 6.0` for other EDGAR calls. At that same throttle: `16,500 / 6 ≈ 2,750 seconds ≈ 45.8 minutes` — a substantial but entirely reasonable batch-job runtime, comparable to or shorter than other daily pipeline steps, and **nowhere close to SEC's rate ceiling** (running at 6 req/s uses 60% of the 10 req/s budget, with headroom to spare; even the full 10 req/s would finish in ~27.5 minutes).

This comfortably clears the coder note's ask: SEC-native, issuer-keyed insider data at full Step-4 scale is arithmetically feasible within SEC's own published limits, using the exact throttle pattern already proven in this codebase.

## 6. Go/no-go conclusion

**GO — with a caveat about where the original skepticism came from.**

`edgar_provider.py`'s existing docstring skepticism ("cannot verify with confidence from documentation alone") does **not** hold up under actual testing. Both the older `browse-edgar?owner=include` atom interface and the modern `data.sec.gov/submissions/...json` REST interface correctly return issuer-keyed Form 4 data — verified against three real, current, cross-checkable filings, not assumed from documentation. The `submissions.json` path is unambiguously the one to prefer if this is ever built: exact form-type matching (no prefix bug), native JSON, fits the existing `_SecHttpClient`/`fetch_json`-injection pattern already proven in this codebase, and — as a genuine bonus — exposes an explicit 10b5-1 indicator FMP's data structure lacks entirely.

The practical case for pursuing this now is stronger than it would have been in isolation, given Track A's parallel finding that the FMP `insider-trading/search` endpoint the current implementation depends on returns HTTP 402 on this project's actual key (see the Track A report's addendum, same date) — but per this coder note's explicit instruction, that connection is noted, not acted on. Whether to replace the FMP-based Step-5-only approach with this SEC-native, Step-4-scale-capable one is an architect decision for a future coder note, not something implemented here.

## What was NOT done (per scope)

- No provider code written.
- No changes to `fmp_insider_provider.py`, `pipeline_orchestrator.py`, or `edgar_provider.py`.
- No commit.
