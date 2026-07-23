# Insider Flag — Part B: SEC Bulk Insider Transactions Data Sets Loader

**Date:** 2026-07-21
**Scope:** New historical-only loader + tests + parity harness. No wiring into
the live daily step, no config change, no backfill run, no commit.
**Files:** `app/providers/edgar_insider_bulk_loader.py` (new),
`tests/test_edgar_insider_bulk_loader.py` (new),
`app/providers/edgar_provider.py` (one additive method)

---

## 1. What was built

`app/providers/edgar_insider_bulk_loader.py` — same DI shape as
`edgar_insider_provider` (injected `fetch_bytes` / `fetch_text`, no I/O of its
own, fully offline-testable).

Public surface:

```python
quarters_in_range(start_date, end_date) -> list[str]        # ["2025q4", "2026q1"]
resolve_quarter_zip_urls(quarters, fetch_text) -> dict[str, str]
candidate_zip_urls(quarter) -> tuple[str, ...]
parse_bulk_filing_date(raw) -> date | None                  # "09-JUN-2026"
qualifying_events_from_archive(archive, *, quarter, ...) -> dict[str, set[date]]
build_bulk_insider_index(start, end, fetch_bytes, ...) -> BulkInsiderIndex

class BulkInsiderIndex:
    qualifying_purchase_flag(cik, as_of_date, *, lookback_days) -> bool
```

### URL prefix handling — resolved dynamically, not hardcoded

The landing page is scraped and is authoritative; it lists all 82 quarters
(2006Q1–2026Q2) each with whichever hosting directory it actually lives under.
Verified live:

```
/files/datastandardsinnovation/data/insider-transactions-data-sets/2026q2_form345.zip
/files/structureddata/data/insider-transactions-data-sets/2026q1_form345.zip
/files/structureddata/data/insider-transactions-data-sets/2025q4_form345.zip
...
```

Two fallbacks if the landing page can't be read: the static prefixes are tried
in order at download time (`_download_quarter` probes the alternate on failure),
so a wrong guess surfaces as a retry rather than a silent miss. Requesting an
unpublished quarter (e.g. the current unclosed one) raises rather than guessing.

### Members read

Only `SUBMISSION.tsv`, `NONDERIV_TRANS.tsv`, `DERIV_TRANS.tsv`. `FOOTNOTES.tsv`
(44 MB of the ~91 MB set, anomaly A7) is never opened — asserted by a test that
instruments `ZipFile.open`. Parsing is `csv.DictReader(..., delimiter="\t")`,
streamed rather than materialized (`NONDERIV_TRANS` is ~100k rows × 28 columns
per quarter and only a small minority match a candidate accession).

### Predicate — identical to the live path

`DOCUMENT_TYPE == "4"` (excludes `4/A` amendments, matching the live `form ==
"4"`), whole filing dropped when `AFF10B5ONE` is truthy and `exclude_10b5_1`,
then any `TRANS_CODE == "P"` row in **either** transaction table with
`shares * price >= min_transaction_value_usd`.

The constants are **imported from `edgar_insider_provider`**
(`_TRUTHY_TOKENS`, `_TRANSACTION_CODE_PURCHASE`, `_FORM_TYPE_TRANSACTION`, the
three `DEFAULT_*`), not redefined — a duplicated literal is exactly how two
paths silently drift apart.

### Join key

`ISSUERCIK`, zero-padded to 10, never `ISSUERTRADINGSYMBOL`. As the coder note
required, and there is a test that constructs two filings sharing a symbol
across different CIKs to prove the join is CIK-based. The known CIK-resolution
issue (XOM-class holdco reorgs) is inherited identically to the live path;
untouched here, as instructed.

### Coverage guard

`qualifying_purchase_flag` **raises** if the requested `(as_of - lookback,
as_of]` window isn't fully inside the loaded quarters. An uncovered window is
indistinguishable from "no qualifying purchases" — the exact silent-false-
negative class Part A just fixed. This is not defensive padding; it caught a
genuine contract violation in my own first draft of the end-to-end test.

### Pre-2023 `AFF10B5ONE` guard

If `SUBMISSION.tsv` has no `AFF10B5ONE` column and `exclude_10b5_1=True`, the
loader **raises** with an actionable message. `allow_missing_aff10b5one=True`
downgrades it to a warning for a caller who deliberately accepts the looser
predicate. Detected from the actual header, not from the year, so it stays
correct if SEC backfills the column further.

### One additive change outside the new module

`edgar_provider._SecHttpClient` gained `get_bytes()` (4 lines, mirrors
`get_text`). A zip cannot go through `response.text` without corruption, and
the throttle/retry/User-Agent state must stay on the one shared session rather
than a second independently-rate-limited client. `edgar_provider.py` is not on
CLAUDE.md's frozen list, but flagging the change explicitly.

---

## 2. Parity harness — real data, real network

**Setup:** `as_of = 2026-06-30`, `lookback = 90` (window `2026-04-01` →
`2026-06-30`, covered exactly by the published 2026Q2 set). 150 tickers sampled
deterministically across the 3,911-ticker active universe (every 26th, so the
sample spreads across the alphabet rather than clustering on large caps).
148 resolved to a CIK.

```
active universe: 3911 tickers
sample: 150 tickers (every 26th)
CIK resolved: 148/150
bulk: quarters=('2026q2',) issuers_with_purchases=27 in 3.4s
live: 335.2s

=== PARITY ===
compared      : 148
agree         : 147
disagree      : 1
live None     : 0
live  True    : 28
bulk  True    : 27
  MISMATCH AEI: live=True bulk=False
```

**147/148 (99.3%) agreement. Zero retrieval failures on either path.**

Cost comparison on the identical question, same 148 tickers, same date:
**bulk 3.4 s (1 zip) vs live 335.2 s (~1,600 requests) — 99×**, and the bulk
side answers *every other date in the quarter* at no additional cost, which is
where the 267-hour → seconds backfill figure comes from.

### The single disagreement is a **live-path defect, not a bulk defect**

Investigated to root cause. AEI's live `True` comes from accession
`0001493152-26-027990`, filed 2026-06-09. The raw ownership XML says:

```xml
<issuer>
    <issuerCik>0001897245</issuerCik>
    <issuerName>HWH International Inc.</issuerName>
    <issuerTradingSymbol>HWH</issuerTradingSymbol>
</issuer>
<reportingOwner>
    <rptOwnerName>Chan Heng Fai Ambrose</rptOwnerName>
```

The bulk row agrees exactly, and correctly attributes it to HWH:

```
ACCESSION_NUMBER 0001493152-26-027990  FILING_DATE 09-JUN-2026  DOCUMENT_TYPE 4
ISSUERCIK 0001897245  ISSUERNAME HWH International Inc.  ISSUERTRADINGSYMBOL HWH
AFF10B5ONE 0
NONDERIV_TRANS: TRANS_CODE P, TRANS_SHARES 250000.0, TRANS_PRICEPERSHARE 2.0
```

So this is a real $500k open-market insider purchase — **of HWH stock, not AEI
stock.** AEI (Alset Inc., CIK 0001750106) had exactly one Form 4 in the window
and it was this one. Bulk's `False` for AEI is the correct answer.

**Root cause:** the live path assumes every Form 4 in a CIK's `submissions.json`
has that CIK as the *issuer*. It does not. Confirmed on AEI's own submissions
payload:

```
AEI CIK 0001750106 name: Alset Inc.
insiderTransactionForIssuerExists: 1
insiderTransactionForOwnerExists : 1
```

`submissions.json` mixes filings where the CIK is the **issuer** with filings
where it is the **reporting owner**. Alset is a reporting owner (10% holder /
affiliated director) of HWH, so HWH's Form 4 appears under Alset's submissions
and the live path credits the purchase to AEI. See anomaly B-A1.

Adjusting for this, bulk is right on 148/148 and the live path is right on
147/148. That is stronger than the investigation's 120/120 sample, not weaker.

---

## 3. Tests

`tests/test_edgar_insider_bulk_loader.py` — **42 tests, all passing, fully
offline** (in-memory zips of fixture TSVs, injected fetchers, no downloads).

| group | covers |
|---|---|
| `TestQuartersInRange` | quarter arithmetic, year boundary, reversed range |
| `TestFilingDateParsing` | real `DD-MON-YYYY` format, ISO tolerance, malformed |
| `TestUrlResolution` | **both hosting prefixes**, unpublished quarter, landing-page failure fallback, candidate probing |
| `TestQualificationPredicate` | purchase/threshold/code, 10b5-1 both directions and both token casings, derivative table, `4/A` amendments excluded, Form 3/5 excluded, orphan transaction rows, unparseable numerics, **FOOTNOTES never read** |
| `TestCikJoin` | **CIK-based join proven not symbol-based**, zero-padding, universe filter, malformed CIK |
| `TestPre2023Aff10b5OneGuard` | raises / warns-when-allowed / irrelevant-when-not-excluding |
| `TestBulkInsiderIndex` | window semantics incl. exclusive lower bound, unknown CIK, **uncovered-window refusal** |
| `TestBuildBulkInsiderIndex` | end-to-end offline, prefix fallback on 404, all-prefixes-fail, multi-quarter merge |

```
tests/test_edgar_insider_bulk_loader.py  42 passed
```

**Full suite:** `pytest tests/` — 2,576 collected, 3 failed, all three the known
pre-existing failures (see B-A4). No new failures introduced.

---

## 4. Anomalies

**B-A1 — NEW, and it is a live-path correctness defect: `submissions.json`
returns Form 4s where the CIK is the *reporting owner*, not the issuer, and the
live path counts them.** Confirmed on AEI (§2). `insiderTransactionForOwnerExists`
is a separate flag from `insiderTransactionForIssuerExists` and the module reads
neither when filtering candidates. Consequence: **false-positive
`insider_trade_flag` for any company that is itself an insider of another
company** — holdcos, parent/affiliate structures, 10%-owner corporates. In this
sample it affected **1 of 28 live `True` results (3.6%)**; n=1, so treat the
rate as indicative, not measured.

The fix is small and sits in the function Part A already touched — reject a
filing whose `<issuer><issuerCik>` does not match the requested CIK:

```python
    issuer_cik = (root.findtext("issuer/issuerCik") or "").strip()
    if issuer_cik and issuer_cik.zfill(10) != expected_cik:
        return False
```

**Not implemented.** It is outside the coder note's Part A scope, it changes
flag values (some `True` → `False`), and the note says deliver and stop.
Recommend authorizing it as a Part A follow-up. Note the bulk path is
**immune** by construction, since it joins on `ISSUERCIK`.

**B-A2 — the guard in §1 caught a real contract violation during development.**
The first draft of the end-to-end test loaded only 2026Q1 and then asked a
90-day window reaching into 2025Q4. Without the coverage guard that would have
returned a confident `False`. Recording it because it is direct evidence the
guard is load-bearing, not ceremonial — and because any caller must pass the
*lookback window* start to `build_bulk_insider_index`, not the first signal date.

**B-A3 — `SEC_USER_AGENT` is not set in this environment.** All live work here
required setting it for the process. Both the live path and this loader are
inert in prod until it is configured (orchestrator pre-run check already warns).

**B-A4 — three pre-existing test failures, unrelated and untouched.**
`test_yahoo_provider.py::test_only_yahoo_provider_references_yfinance` fails
because `edgar_provider.py` contains `compute_fundamentals_from_yfinance_info`;
verified present at HEAD via `git grep yfinance HEAD -- app/providers/edgar_provider.py`,
so it predates and is unaffected by the `get_bytes` addition. Plus the two
spec-path lookups, `test_data_validator.py::test_spec_documents_open_gaps_not_invented`
and `test_mutation_detector.py::test_spec_documents_open_gap_g1`. All three are
the known pre-existing set logged 2026-07-08 as out-of-scope. Not fixed, per
that standing note.

**B-A5 — bulk throughput is not the constraint; the live path is.** The bulk
side answered 148 tickers in 3.4 s including download and parse. The live side
took 335.2 s for the same 148 at ~4.8 req/s. Any future comparison should not
quote 6 req/s for the live path (investigation A4 stands).

---

## 5. Not done, per the coder note

- **Not wired into the live daily `insider_flag_refresh` step.** Historical use
  only; the daily path stays on `edgar_insider_provider` (or moves to Part C).
- No backfill run.
- `max_candidate_filings` and every other threshold unchanged.
- XOM/CIK-resolution issue untouched (inherited, not introduced).
- No commit.
