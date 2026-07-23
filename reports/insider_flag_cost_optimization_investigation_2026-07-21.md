# Insider-Flag Cost Optimization — Investigation

**Date:** 2026-07-21
**Scope:** Investigation + design proposal only. Nothing implemented, nothing committed.
No file under `app/`, `tools/`, or `tests/` was modified. No config changed. No backfill run.

---

## Headline

**Before any of A/B/C is worth considering: `fetch_insider_purchase_flag` is
structurally incapable of returning `True` today.** `submissions.json`'s
`primaryDocument` for Form 4 filings is the **XSL-rendered HTML** path
(`xslF345X05/form4.xml`), not the raw ownership XML. `ET.fromstring` raises
`ParseError` on every one of them, and `_is_qualifying_purchase` swallows
`ParseError` as "no qualifying purchase in this filing". Measured on **3,009
real filings across 50 tickers: 3,009/3,009 (100%) are XSL-rendered HTML and
100% fail to parse.**

Two consequences, both load-bearing for this investigation:

1. `insider_trade_flag` would be `False` for every ticker that has any Form 4
   history — a 100% false-negative rate, not a rare edge case. The
   2026-07-18 batch run's `flag=False` for **50/50 tickers, zero `True`** was
   read at the time as "qualifying purchases are rare." It was the parse bug.
2. **This is the primary reason the measured cost was 18.44 requests/ticker.**
   The early-exit (`return True` on first qualifying purchase) *never fires*,
   so every ticker burns its entire candidate list every time.

Verified by fetching the same filings with the `xslF345X0N/` prefix stripped:
they parse cleanly as `<ownershipDocument>` and return `True` where the SEC's
own bulk dataset says `True` (120-filing validation sample, 100% agreement).

Everything below reports both an **as-implemented** and a **corrected** figure
so the proposal is not built on the broken baseline.

---

## Method

All numbers are measured, not modelled from assumptions.

- **Live harvest (network, read-only):** the same 50-ticker sample as the
  2026-07-18 runtime measurement, verbatim. Fetched each ticker's
  `submissions.json` plus every Form 4 XML in the *union* of all 130 backfill
  dates' 90-day lookback windows. **3,059 requests, 914.5 s wall.**
- **Offline replay:** `fetch_insider_purchase_flag`'s exact control flow
  (window gate, newest-first order, 50-filing cap, early exit) replayed against
  the cached artifacts. The replay reproduces the 2026-07-18 measurement
  **exactly** — mean 18.44 requests/ticker, WMT at the 50-cap — which is the
  evidence that the replay is faithful.
- **Population model:** the same control flow driven by the SEC bulk
  Insider Transactions Data Sets (2025Q3–2026Q2), covering the **full 3,911-ticker
  active universe** rather than a 50-ticker sample.
- **Ground truth for "does this filing qualify":** bulk TSVs for filings
  ≤ 2026-06-30; raw ownership XML for the 137 filings after that. Validated
  100% equivalent on a 120-filing sample (§3.4).

Scratchpad scripts: `harvest_insider_artifacts.py`, `analyze_sample.py`,
`population_cost_model.py`, `validate_bulk_vs_rawxml.py`, `probe_sec_bulk*.py`.

---

## Part 1 — Actual cost distribution

### 1.1 The 50-ticker sample (as_of 2026-07-17) — as-implemented

```
total = 922 requests   mean = 18.44   median = 17
p10=6  p25=9  p75=22  p90=33  p95=46  max=51

     1 request :  1 ticker  ( 2.0%) =   1 req ( 0.1% of cost)
   2-5 requests:  4 tickers ( 8.0%) =  11 req ( 1.2%)
  6-10 requests:  8 tickers (16.0%) =  56 req ( 6.1%)
 11-20 requests: 19 tickers (38.0%) = 295 req (32.0%)
 21-40 requests: 14 tickers (28.0%) = 372 req (40.3%)
 41-51 requests:  4 tickers ( 8.0%) = 187 req (20.3%)

at the 50-filing cap: 1 (WMT)
top 10 tickers = 377/922 requests (40.9%)
outcomes: False 50, True 0, None 0
```

This sample is deliberately large-cap-heavy and is **not** representative of
the real population — see 1.2.

### 1.2 The full 3,911-ticker active universe (as_of 2026-06-30)

**As-implemented (parse always fails, early exit never fires):**

```
total = 44,296 requests   mean = 11.33   median = 10
p50=10  p75=16  p90=24  p95=30  p99=44  max=51

     1 request :   599 tickers (15.3%) =    599 req ( 1.4% of cost)
   2-5 requests:   678 tickers (17.3%) =  2,233 req ( 5.0%)
  6-10 requests:   817 tickers (20.9%) =  6,708 req (15.1%)
 11-20 requests: 1,261 tickers (32.2%) = 18,460 req (41.7%)
 21-40 requests:   491 tickers (12.6%) = 13,254 req (29.9%)
 41-51 requests:    65 tickers ( 1.7%) =  3,042 req ( 6.9%)

at the 50-filing cap: 25 tickers (0.64%)
outcomes: False 3,911 (100%), True 0, None 0
wall-clock: 123.0 min @ 6 req/s  |  153.8 min @ the 4.8 req/s actually observed
```

**Corrected (parse bug fixed):**

```
total = 39,691 requests   mean = 10.15   median = 8
outcomes: True 660 (16.88%), False 3,251
early-exit position among True results: median 2, mean 4.8, p90 12, max 31
wall-clock: 110.3 min @ 6 req/s
```

### 1.3 What the shape actually says

**The mean is not hiding a heavy tail — it is a broad middle.** This matters
because it rules out the cheap tweak:

- Only **0.64%** of tickers reach the 50-filing cap.
- The top 500 tickers by cost (12.8% of the universe) are only **34%** of total
  requests. The top 100 are **9.9%**.
- The 11–40-request band alone is **44.8% of tickers and 71.6% of cost**.

**Cap sensitivity (corrected parse, full universe):**

| `max_candidate_filings` | requests | vs cap=50 | outcome flips |
|---|---|---|---|
| 5  | 17,573 | −55.7% | 204 |
| 10 | 27,154 | −31.6% | 88 |
| 15 | 32,744 | −17.5% | 39 |
| 20 | 35,866 | −9.6%  | 21 |
| 25 | 37,583 | −5.3%  | 5 |
| 30 | 38,513 | −3.0%  | 1 |
| 50 | 39,691 | —      | 0 |

Lowering the cap is **not** the dominant simple tweak. Getting a meaningful
saving (cap=10, −32%) costs 88 wrong answers — 13% of all `True` results the
field would ever produce. Getting a safe cap (25–30) saves only 3–5%. Neither
end is worth doing.

### 1.4 Early-exit effectiveness

- **As-implemented: zero.** It never fires, because nothing ever parses.
- **Corrected:** fires for 660/3,911 tickers (16.9%). The qualifying purchase is
  usually near the front — **median position 2**, mean 4.8, p90 12 — so ordering
  is already right (newest-first) and no reordering work is warranted. Total
  saving from early exit alone: 4,605 requests, 10.4%.

### 1.5 Outcome distribution

| | as-implemented | corrected |
|---|---|---|
| `True`  | 0 (0.00%)      | 660 (16.88%) |
| `False` | 3,911 (100%)   | 3,251 (83.12%) |
| `None`  | 0              | 0 |

Zero `None` in the whole harvest — 3,059 live requests, **zero fetch failures**.
SEC EDGAR retrieval itself is not a problem; the parsing is.

---

## Part 2 — Cross-date redundancy

130 trading days, **2025-12-22 → 2026-06-30**. Union lookback window
**(2025-09-23, 2026-06-30]**. Full 3,911-ticker universe.

### As-implemented

| approach | requests | @ 6 req/s | @ 4.8 req/s (observed) |
|---|---|---|---|
| naive (recompute per date) | **5,775,143** | 267.4 h (11.1 days) | 334.2 h (13.9 days) |
| distinct artifact, fetched once | **123,704** (3,911 `submissions.json` + 119,793 Form 4 XML) | 5.7 h | 7.2 h |
| **ratio** | **46.7×** — 97.9% of all requests are re-fetches | | |

### Corrected

| approach | requests | @ 6 req/s |
|---|---|---|
| naive | 5,187,342 | 240.2 h |
| distinct artifact | 118,778 | 5.5 h |
| **ratio** | **43.7×** (97.7% saving) | |

### Sample-level corroboration

The 50-ticker replay predicts 138,175 naive requests vs 3,059 distinct — 45.2×.
The live harvest **actually executed 3,059 requests**, matching the predicted
distinct-artifact count exactly.

The redundancy is structural, not incidental: adjacent dates' 90-day windows
overlap by 89 days, and Form 4 filings are immutable once filed, so ~98% of the
naive traffic re-downloads bytes that cannot have changed.

---

## Part 3 — SEC bulk Insider Transactions Data Sets

Landing page: `https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets`
(returns 403 to a generic User-Agent; fetched with this project's compliant
`SEC_USER_AGENT` via `build_sec_http_client`).

### 3.1 What's in it — every needed field is present

Downloaded and inspected **2026Q1** and **2026Q2**. Ten files per zip; the three
that matter:

| file | fields this module needs |
|---|---|
| `SUBMISSION.tsv` (14 cols) | `ACCESSION_NUMBER`, `FILING_DATE`, `DOCUMENT_TYPE`, **`ISSUERCIK`**, `ISSUERNAME`, `ISSUERTRADINGSYMBOL`, **`AFF10B5ONE`** |
| `NONDERIV_TRANS.tsv` (28 cols) | **`TRANS_CODE`**, **`TRANS_SHARES`**, **`TRANS_PRICEPERSHARE`**, `TRANS_DATE` |
| `DERIV_TRANS.tsv` (42 cols) | same three, for derivative transactions |

Nothing this module needs is missing:

- **Issuer identification** — `ISSUERCIK` *and* `ISSUERTRADINGSYMBOL`, so the
  per-ticker `submissions.json` request disappears entirely for historical dates.
- **`transactionCode`** — `TRANS_CODE`, same SEC single-letter vocabulary.
  2026Q1 `NONDERIV_TRANS` distribution: `F` 27,019, `A` 24,690, `S` 22,822,
  `M` 16,300, **`P` 5,935**, `D` 2,246, `J` 1,997, `G` 1,435, …
- **Shares / price** — `TRANS_SHARES`, `TRANS_PRICEPERSHARE`.
- **Filing date** — `FILING_DATE`, documented as "Filing date with the
  Commission; sourced from EDGAR", `DATE`, **not nullable**. This is the exact
  field the module's point-in-time gate uses. Format `DD-MON-YYYY`.
- **10b5-1 indicator — present.** `AFF10B5ONE` on `SUBMISSION`, documented as
  "The transaction was made pursuant to a contract, instruction or written plan …
  intended to satisfy the affirmative defense conditions of Rule 10b5-1(c)."
  **Not disqualifying — but see the 2023 boundary in 3.5.**

Observed `AFF10B5ONE` token values (2026Q1): `0` 42,435 / `false` 11,525 /
`""` 10,517 / `1` 3,620 / `true` 1,162. The module's existing
`_TRUTHY_TOKENS = {"1","true","True","TRUE"}` handles these correctly as-is.

### 3.2 Size and format

Plain **tab-separated UTF-8 TSV inside a zip**. 82 quarters available,
**2006Q1 → 2026Q2**. Per-quarter zip is **7.6–13.2 MB**; 2026Q1 uncompressed is
~91 MB total, of which `SUBMISSION` (7.5 MB) + `NONDERIV_TRANS` (11.5 MB) +
`DERIV_TRANS` (6.1 MB) = ~25 MB is all that's needed. `FOOTNOTES.tsv` is 44 MB
and irrelevant here. Parsing is `csv.DictReader(..., delimiter="\t")` — no
custom parser required.

A 90-day lookback spans at most **2** quarterly files; a 130-trading-day
backfill plus its lookback needs **3–4** files (~32–40 MB total download).

### 3.3 Coverage and lag — your reading is correct, but the lag is short

- Cadence per SEC: *"The data sets will be updated quarterly. Data contained in
  documents filed after 5:30PM Eastern on the last business day of a quarter
  will be included in the subsequent quarterly posting."*
- Filing dates align exactly with quarter boundaries (2026Q1: 61 distinct
  `FILING_DATE`s, 2026-01-02 → 2026-03-31). No spillover.
- **The lag is shorter than "one quarter" implies.** As of today (2026-07-21),
  **2026Q2 — the quarter that ended 21 days ago — is already published.** So the
  uncovered window is "since the last quarter close", i.e. 0–~92 days depending
  on where in the quarter you are, not a full quarter of blindness.
- Confirmed: **viable for historical backfill, not for live daily runs.** The
  hybrid split is the right reading.

Universe coverage, 2026Q1: **56,853 Form 4s market-wide, 49,590 belonging to our
3,911-ticker universe, across 3,306 distinct issuers** (matched on
`ISSUERTRADINGSYMBOL`). 2026Q2: 49,832 / 41,445 / 3,322.

### 3.4 Hybrid feasibility — measured, not assumed

Built the bulk-side determination with the module's own rules
(`DOCUMENT_TYPE == "4"`, `AFF10B5ONE` truthy → excluded, any `TRANS_CODE == "P"`
row in either table with `shares × price ≥ 10,000`) and compared against
`_is_qualifying_purchase` on the **raw** ownership XML for a deterministic
120-filing sample:

```
(raw_XML, xsl_HTML_as_fetched_today, bulk) -> count
  (False, False, False) -> 119
  (True,  False, True ) ->   1

raw-XML vs bulk agreement: 120/120 (100.00%)   disagreements: 0
AFF10B5ONE truthiness mismatches (raw XML vs bulk): 0
raw XML parse: ok=120  parse_error=0
```

Semantic differences found, all benign:

- **Derivative vs non-derivative:** the bulk set splits them into two files where
  the XML has two sibling tables. The module already checks both. Reading both
  TSVs reproduces it exactly — no difference.
- **Amendments:** bulk carries `4/A` as a distinct `DOCUMENT_TYPE` (904 of
  57,757 Form-4-family rows in 2026Q1). Filtering `DOCUMENT_TYPE == "4"`
  reproduces the module's exact `form == "4"` matching, including its exclusion
  of amendments. No behavior change.
- **`aff10b5One` casing:** bulk emits `true`/`false`/`0`/`1`; raw XML emits
  `0`/`1`. Both land inside the existing truthy set.

The one disagreement direction found is the **existing bug**, not a bulk defect:
where bulk and raw XML both say `True`, today's XSL-fetching path says `False`.

### 3.5 Bulk-path caveats worth pinning before implementation

1. **`AFF10B5ONE` only exists from 2023 onward.** SEC note: *"In July 2025, the
   2023-2025 data sets were updated to include the AFF10B5ONE element in the
   SUBMISSION file. Future data sets will be processed similarly."* A backfill
   earlier than 2023 cannot honor `exclude_10b5_1` from bulk. Irrelevant for the
   pending 2026 backfill; disqualifying for a multi-year one.
2. **Two different URL prefixes.** Quarters ≤ 2026Q1 live under
   `/files/structureddata/data/insider-transactions-data-sets/`; **2026Q2 is
   under `/files/datastandardsinnovation/data/...`**. A hardcoded prefix breaks
   on the newest quarter. Scrape the landing page or try both.
3. **Join on `ISSUERCIK`, not `ISSUERTRADINGSYMBOL`.** Symbol matching found
   3,306 of 3,911 universe tickers; the pipeline already resolves CIK, and CIK is
   the stable key. (Note this inherits the CIK-resolution bug in Anomaly A2.)

---

## Part 4 — Recommendation

### Step 0 (precondition, not an optimization): fix the XSL parse bug

Strip the `xslF345X0N/` directory prefix from `primaryDocument` before building
the filing URL. One line. **Zero cost reduction — it is not an optimization.**
But every option below is otherwise optimizing the cost of computing a constant
`False`, so nothing else should be sequenced ahead of it.

Correctness impact of fixing it: `insider_trade_flag` goes from always-`False`
to ~16.9% `True`. Confirmed safe — the field is referenced only in
`default_configs.py`, `schema_manager.py`, `pipeline_orchestrator.py`,
`provider_interface.py`, and the two provider modules. **No validator, scoring,
or routing path reads it**, consistent with the informational-only scope both
coder notes pinned. No Step 3/4/5 output changes.

Consider also making `ParseError` visible rather than silent — the bug survived a
real 50-ticker batch run and a 37-test suite precisely because a 100% parse
failure is indistinguishable from "no purchases found."

### Recommended: **B for the backfill, C for the daily run.** Not A.

**(B) SEC bulk quarterly dataset — for historical dates.**

- **Cost reduction: 5,775,143 requests → 4 zip downloads (~40 MB).** 267 hours
  of throttled traffic → seconds. That is a ~99.99% reduction, and it is a
  measured comparison, not an estimate.
- Complexity: moderate. A TSV loader (`csv.DictReader`, tab-delimited), a
  quarter-file resolver handling the two URL prefixes, and the same
  qualification predicate expressed over three columns. No new dependency.
- Correctness risk: **low, and measured** — 120/120 agreement with raw-XML
  parsing, 0 `AFF10B5ONE` mismatches, amendments and derivative/non-derivative
  both reproducible exactly. It also *sidesteps* the parse bug entirely rather
  than inheriting it, since it never touches `primaryDocument`.
- Residual risk: it is a second code path. Recommend a parity harness at
  implementation time that runs both paths over a few hundred filings on a
  recent date and asserts identical flags — the 120-filing sample here is
  evidence, not a regression test.

**(C) Incremental daily state — for the recurring cost.**

- **Cost reduction: 44,296 → ~4,557 requests/day** (3,911 `submissions.json`
  + a measured mean of 646 genuinely-new universe Form 4s per filing date).
  **123 min → 12.7 min, 9.7×.**
- A stronger variant: replace the 3,911 per-ticker `submissions.json` calls with
  SEC's **daily full index**
  (`/Archives/edgar/daily-index/YYYY/QTRn/form.YYYYMMDD.idx` — verified live:
  795 KB, 1,289 Form-4 rows / 630 distinct accessions for 2026-07-17, with
  issuer-CIK rows present). That drops the daily run to **~650 requests, ~2 min**.
- Complexity: highest of the three, and it needs a persistence decision.
- **Schema implication, flagged rather than assumed:** `ticker_fundamentals` is
  keyed `(ticker, as_of_date)` and stores the *answer*, not the *evidence*.
  Answering "was there a qualifying purchase in the last 90 days?" from state
  requires storing qualifying-purchase dates plus a per-ticker
  `last_scanned_filing_date` watermark. That is a new table
  (`insider_purchase_events` or similar), not a column. This is an architecture
  decision and should be ratified before implementation, not folded in.

**(A) Cross-date artifact cache — recommend against, on its own.**

- It works (46.7×, 5,775,143 → 123,704 requests) and it is the smallest change.
  But 123,704 requests is still **5.7 hours** of throttled traffic for one
  backfill, against **seconds** for B on the same data — B dominates it by three
  orders of magnitude for the exact use case A targets, and A does nothing for
  the recurring daily cost.
- It also *preserves* the parse bug, since it caches the same wrong documents.
- Worth keeping in mind only as a fallback if B is rejected for reasons outside
  cost.

### Sequencing

1. Fix the parse bug (precondition; no cost change).
2. Implement B for the historical backfill.
3. Implement C for the daily run — after B, and after the storage decision is
   ratified.
4. Do **not** touch `max_candidate_filings`. §1.3 shows the distribution does
   not reward it: safe caps save 3–5%, and the caps that save meaningfully
   corrupt 13%+ of the field's `True` answers.

---

## Anomalies (verbatim)

**A1 — `primaryDocument` is the XSL-rendered HTML; 100% of Form 4 parses fail
silently.** Measured across 3,009 cached filings from 50 tickers: prefixes
`xslF345X05` (1,774) and `xslF345X06` (1,235), **3,009/3,009 raise
`ET.ParseError`**, which `_is_qualifying_purchase` converts to `False`.
Confirmed against the raw path — e.g. `0000789019-26-000028` (MSFT):
`xslF345X05/form4.xml` → HTML → `False`; `form4.xml` → `<?xml version="1.0"?>
<ownershipDocument>` → **`True`**. Same for `0001108524-26-000066` (CRM) and
`0001868288-26-000002` (GME). The 2026-07-18 batch's `flag=False` on 50/50
tickers with 0 `True` is fully explained by this. **Nothing bad has landed in
prod** — `ticker_fundamentals` currently holds 46 rows, all with
`insider_trade_flag = NULL`.

**A2 — `XOM` resolves to the wrong CIK, and the failure is silent.** SEC's own
`company_tickers.json` maps `XOM` → CIK `0002115436` *"ExxonMobil Holdings
Corp"*, whose `submissions.json` reports `insiderTransactionForIssuerExists = 0`,
`tickers = []`, and **0 Form 4s**. The actual issuer is CIK `0000034088`
*"EXXON MOBIL CORP"*, `tickers = ['XOM']`, `insiderTransactionForIssuerExists = 1`,
**304 Form 4s**. The module short-circuits to `False` in one request and looks
like a cheap, healthy ticker. This is in `edgar_provider`'s CIK resolution, so it
affects **all six** EDGAR fundamentals fields for XOM, not just the insider flag,
and it will affect the bulk path identically if that path joins on CIK.
Holding-company reorgs are a class, not a one-off — worth a scoped audit.

**A3 — the "~1,500 tickers / ~82 minutes" baseline understates the real
population by ~2.6×.** `_step_insider_flag` reads `_SQL_EARNINGS_TICKERS`
(`ticker_master WHERE symbol_type='stock' AND active_flag=TRUE`), which is
currently **3,911 tickers**, not ~1,500. At the real population the
as-implemented single-date cost is **44,296 requests = 123 min @ 6 req/s**, not
82 min. Every prior estimate anchored on 1,500 should be rescaled.

**A4 — observed SEC throughput is well below the 6 req/s throttle ceiling.**
The harvest executed 3,059 requests in 914.5 s = **3.35 req/s** end-to-end;
measured over the artifact-write span alone it was 4.83 req/s. Round-trip
latency, not the throttle, is the binding constraint. Wall-clock figures quoted
at 6 req/s are optimistic by roughly 1.3–1.8×; the 4.8 req/s column above is the
more honest one.

**A5 — `fundamentals_refresh` has never run at scale in prod.**
`ticker_fundamentals` contains **46 rows for a single date (2026-06-23)**. Since
`_step_insider_flag` deliberately skips tickers with no existing row, the insider
step would currently update at most 46 rows regardless of what is fixed. The
82/123-minute cost is a *projected* cost that has not yet been paid in prod.

**A6 — `insiderTransactionForIssuerExists` is a weaker short-circuit than the
sample suggested.** In the large-cap sample it fired for 1/50 (2%). Across the
full universe it fires for **599/3,911 (15.3%)** — better than believed, but
those tickers are only **1.4% of total cost**, so it remains a rounding error
against the real spend.

**A7 — `FOOTNOTES.tsv` dominates the bulk zip and is not needed.** 44 MB of the
~91 MB uncompressed 2026Q1 set. Any implementation should read only
`SUBMISSION` / `NONDERIV_TRANS` / `DERIV_TRANS` from the archive rather than
extracting it wholesale.

---

## What was not done

- No implementation of A, B, or C.
- `edgar_insider_provider.py`, `pipeline_orchestrator.py`, and all config
  untouched — including the parse bug, which is reported here as a proposal.
- `max_candidate_filings` and every other threshold unchanged.
- No historical backfill run.
- No commit.
