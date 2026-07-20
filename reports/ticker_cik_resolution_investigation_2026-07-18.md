# Ticker → CIK Resolution Investigation — XOM Anomaly

**Date:** 2026-07-20 (investigation of a 2026-07-18 coder note)
**Scope:** Read-only investigation. No code changes made. Independent of the `insider_trade_flag` implementation.

---

## 1. XOM anomaly — confirmed

| | CIK | Name | Exchanges | Tickers (per own submissions) | `insiderTransactionForIssuerExists` |
|---|---|---|---|---|---|
| **Real, exchange-listed ExxonMobil** | `0000034088` | EXXON MOBIL CORP | NYSE | XOM | `1` (true) |
| **What `company_tickers.json` resolves "XOM" to** | `0002115436` | ExxonMobil Holdings Corp | *(none)* | *(none)* | `0` (false) |

This project's `EdgarFundamentalsProvider._default_ticker_to_cik("XOM")` returns `0002115436` — confirmed wrong. Verified against SEC's own authoritative `data.sec.gov/submissions/CIK0000034088.json`, which unambiguously identifies CIK `34088` as the real, NYSE-listed, ticker-`XOM` filer with active insider-filing history.

## 2. Root cause — NOT a bug in our code

Ruled out, in order:

- **Stale local cache** — ruled out. The live, current `https://www.sec.gov/files/company_tickers.json` was re-fetched directly and returns the *same* wrong mapping (`XOM` → `2115436`, title "ExxonMobil Holdings Corp") as the on-disk cache (`data/cache/sec_company_tickers.json`, last written 2026-07-19). The error is not a caching artifact — it is present in SEC's live source file right now.
- **Parsing bug in `_parse_ticker_map`** — ruled out. Scanned every entry in the full ~10,000-entry `company_tickers.json` file for tickers with more than one candidate CIK (which would expose a last-write-wins collision bug in the dict-building loop at `edgar_provider.py:1102-1110`). **Zero tickers have duplicate entries**, XOM included. There is exactly one candidate CIK per ticker in the source file, so there is no collision for our code to resolve incorrectly — it faithfully reproduces whatever SEC publishes.
- **Ticker reused by an unrelated newly-registered filer** — ruled out. CIK `2115436` is not an unrelated company; its business address (22777 Springwoods Village Parkway, Spring, TX) is ExxonMobil's own corporate campus.

**Actual root cause: a live, in-progress ExxonMobil holding-company reorganization**, and SEC's `company_tickers.json` has been updated to point the ticker at the *new* holdco entity before that entity's own submissions/XBRL record has caught up. Filing evidence (SEC EDGAR, confirmed live):

- **2026-07-01** — CIK `34088` (real Exxon) filed an `8-K` and a `POSASR` (post-effective amendment to an automatic shelf registration — the filing type used specifically when converting to a holding-company structure), plus two `Form 4`s.
- **2026-07-01** (same day) — CIK `2115436` ("ExxonMobil Holdings Corp") filed **14 `S-8 POS`** filings (post-effective amendments carrying forward employee stock-plan registrations to a new parent — a classic holdco-reorg signature).
- **2026-07-02** — CIK `34088` filed **Form 25-NSE**, associated with removal of securities from exchange listing.
- **2026-07-06/07** — both CIKs still show activity (a `Form 4` under `34088`, an `8-K` under `2115436`), consistent with an overlap/transition window.

CIK `2115436`'s own submissions record has not yet caught up to reflect it as the live exchange-listed registrant: its `tickers` and `exchanges` arrays are still empty and `insiderTransactionForIssuerExists` is still `0` — because no Form 4s have been filed against it yet (they're still landing on the old CIK `34088` as of 2026-07-06). Its XBRL companyfacts (`data.sec.gov/api/xbrl/companyfacts/CIK0002115436.json`) contain **no `us-gaap` financial facts at all** — only shelf-registration fee metadata (`ffd:NetFeeAmt`, `TtlOfferingAmt`, etc.) from the `POSASR` filing. Interestingly, `companyfacts` for `2115436` reports `entityName: "EXXON MOBIL CORP"` while `submissions` for the same CIK reports `"ExxonMobil Holdings Corp"` — an internal SEC labeling inconsistency, noted verbatim as an anomaly but not further pursued.

In short: SEC's own ticker-symbol index has run ahead of the reorganization, associating `XOM` with the new shell entity before that entity has any real financial history, exchange listing, or insider-filing record. This is the exact upstream state our (correctly-behaving) resolver reflects.

## 3. Blast-radius spot-check

**Sample:** 20 well-known, long-established large caps (Dow-30-class): AAPL, MSFT, JNJ, PG, JPM, KO, IBM, CAT, GE, WMT, CVX, MMM, HD, DIS, BA, INTC, CSCO, PFE, MCD, NKE.

**Result: 20/20 matched their correct, well-established CIK** with company titles matching the real public issuer (e.g. AAPL → `320193` Apple Inc., CVX → `93410` Chevron Corp — Exxon's closest sector peer, unaffected). No anomalies in this sample.

**Full-file duplicate scan:** 0 of ~10,000 tickers have more than one candidate CIK entry in `company_tickers.json` — the resolution mechanism has no structural ambiguity to get wrong for the vast majority of the universe.

## 4. Severity assessment: ONE-OFF, not systemic

This is a rare-event, timing-driven anomaly tied to a live corporate action (an in-flight ExxonMobil holdco reorganization dated ~2026-07-01 to present), not a defect in `_default_ticker_to_cik`, `_parse_ticker_map`, or the disk cache. The resolution code is working exactly as designed — faithfully reproducing SEC's official ticker-to-CIK association — and the association it's reproducing is itself transiently inconsistent at the source, upstream of any code this project owns.

Practical consequence while the transition remains in-flight: EDGAR-sourced fundamentals specifically for `XOM` (`eps_growth_trend`, `leverage_ratio`, `valuation_band`, `piotroski_f_score`, `altman_z_score`, and `insider_trade_flag`) will resolve against an empty shell CIK with no real financial or insider history, producing nulls/short-circuits rather than wrong-but-plausible numbers (the CIK-`2115436` companyfacts payload has no `us-gaap` facts to silently miscompute from). This should self-resolve once SEC's submissions/XBRL record for the new entity catches up post-reorg (or reverses if `company_tickers.json` is corrected). No other ticker in either spot-check showed any sign of the same condition.

## 5. Recommendation (no action taken)

Do not modify `edgar_provider.py` or the CIK cache. If desired, a future narrowly-scoped mitigation could special-case a "resolved CIK has no `us-gaap` facts" condition to fall through to the yfinance fallback rather than reporting null EDGAR fundamentals — but that is a product decision for the architect, out of scope for this investigation, and would need its own review given `edgar_provider.py`'s relationship to frozen-module conventions.
