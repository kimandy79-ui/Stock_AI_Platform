# Insider Flag — Part C: Incremental Daily State (design proposal)

**Date:** 2026-07-21
**Status:** **Design only. Nothing implemented.** Per the coder note, this is
reported for review before any code is written.

---

## 0. Read this first — the payoff is entirely prospective

Investigation anomaly A5 is load-bearing for whether Part C should be built at
all: `ticker_fundamentals` currently holds **46 rows for a single date**, and
`_step_insider_flag` deliberately skips tickers with no existing row. The
123-minute daily cost is a **projected** cost that has never actually been paid
in prod. Additionally, `SEC_USER_AGENT` is unset, so the step currently warns
and skips every ticker.

Part C is a 9.7×–68× reduction of a cost that is presently ~0. **Recommend
confirming `fundamentals_refresh` will run at 3,911-ticker scale daily before
committing to this work.** If the daily step is going to keep operating on tens
of rows, Part A + Part B are sufficient and Part C is premature.

Everything below assumes that confirmation is given.

---

## 1. Proposed schema

Two new tables. Both are per-DB-role (prod / debug / simulation each hold their
own state), created by `schema_manager.py` alongside the existing DDL.

### 1.1 `insider_purchase_events` — the evidence

```sql
CREATE TABLE IF NOT EXISTS insider_purchase_events (
    ticker                VARCHAR NOT NULL,
    cik                   VARCHAR NOT NULL,
    accession_number      VARCHAR NOT NULL,
    filing_date           DATE    NOT NULL,
    transaction_value_usd DOUBLE  NOT NULL,
    is_10b5_1             BOOLEAN NOT NULL,
    source_provider       VARCHAR NOT NULL,   -- 'sec_edgar_live' | 'sec_edgar_bulk'
    discovered_at         TIMESTAMP NOT NULL,
    PRIMARY KEY (ticker, accession_number)
);
CREATE INDEX IF NOT EXISTS idx_insider_events_ticker_filed
    ON insider_purchase_events (ticker, filing_date);
```

Field-by-field justification:

- **PK `(ticker, accession_number)`** — accession number is globally unique per
  filing, so it is the natural idempotency/dedup key: re-running a day, or
  re-discovering the same filing via both the daily index and a repair scan,
  converges rather than duplicating. Leading with `ticker` matches every other
  table in this schema and covers the (rare) case of one accession mattering to
  more than one universe ticker.
- **`cik` as a column, not a key** — needed to write results from Part B's
  CIK-keyed bulk path and to audit CIK-resolution changes (XOM-class holdco
  reorgs, investigation A2), but the pipeline's addressable unit is the ticker.
- **`filing_date`** — the point-in-time gate field, identical to the live path.
  Never transaction date.
- **`transaction_value_usd`** and **`is_10b5_1`** — these are the reason this is
  a real design decision and not a cache. See §1.3.
- **No pruning.** Volume is trivial (~660 issuers × a few qualifying filings per
  quarter). Retaining everything keeps historical/replay queries answerable as
  of any past date, which is exactly what M17 replay would need.

### 1.2 `insider_scan_watermark` — the per-ticker scan state

```sql
CREATE TABLE IF NOT EXISTS insider_scan_watermark (
    ticker                    VARCHAR NOT NULL,
    cik                       VARCHAR NOT NULL,
    last_scanned_filing_date  DATE    NOT NULL,
    last_scanned_at           TIMESTAMP NOT NULL,
    scanned_min_value_usd     DOUBLE  NOT NULL,
    scanned_exclude_10b5_1    BOOLEAN NOT NULL,
    source_provider           VARCHAR NOT NULL,
    PRIMARY KEY (ticker)
);
```

**Why a separate table rather than columns on `insider_purchase_events`:** the
watermark exists for **every scanned ticker**, including the ~83% that have no
qualifying purchase at all. Folding it into the events table would require a
sentinel "scanned, found nothing" row — which is the "store the answer, not the
evidence" mistake in a new costume, and would corrupt the events table's meaning
(every row is currently a real filing). The cardinalities differ too: one
watermark row per ticker, 0..N event rows per ticker.

### 1.3 The correctness lynchpin: stored evidence is only valid for the predicate that produced it

If `risk_label_config` changes `min_insider_transaction_value_usd` or
`insider_purchase_lookback_days`, previously-stored state was produced under a
different predicate. Silently reusing it is wrong. Two mechanisms:

**(a) Record the predicate on the watermark.** `scanned_min_value_usd` /
`scanned_exclude_10b5_1` make a config drift *detectable*: on mismatch, that
ticker's watermark is reset and it is fully rescanned via the live path.

**(b) Store wide, filter narrow.** Scan with the *widest* predicate — record
10b5-1 filings too (tagged `is_10b5_1`), and record each filing's qualifying
transaction value rather than just the fact that it cleared the bar. Then:

| config change | re-evaluable from stored state? |
|---|---|
| `exclude_10b5_1` either direction | **yes** — filter on `is_10b5_1` |
| `min_transaction_value_usd` **raised** | **yes** — filter on `transaction_value_usd` |
| `lookback_days` **shortened** | **yes** — narrower window over same events |
| `min_transaction_value_usd` **lowered** | **no** — rescan (watermark mismatch) |
| `lookback_days` **lengthened** | **no** — rescan back to the new window start |

Only the two widening cases force refetching, and both are loudly detected
rather than silently wrong. The extra storage for the wider predicate is
negligible; 10b5-1 purchases are a minority of `P`-code rows.

`transaction_value_usd` stores the **maximum** qualifying transaction value in
the filing — the filing qualifies if any transaction does, so the max is the
sufficient statistic for any future threshold comparison.

---

## 2. How the daily step changes

### 2.1 Which variant to build first: **the daily full index (the stronger variant)**

The two candidates from the investigation:

| | discovery mechanism | requests/day | wall-clock | vs today |
|---|---|---|---|---|
| **C1** | per-ticker `submissions.json`, filter `filingDate > watermark` | ~4,557 | ~12.7 min | 9.7× |
| **C2** | SEC daily full index `form.YYYYMMDD.idx`, intersect issuer CIKs | ~650 | ~2 min | 68× |

**Recommend building C2 first, and skipping C1 entirely.** Three reasons:

1. **C1 leaves the dominant cost in place.** Of its 4,557 requests, **3,911
   (86%) are the per-ticker `submissions.json` calls** — one per ticker
   regardless of whether anything was filed. C1 optimizes the smaller half.
2. **The schema is identical for both.** The watermark and events tables do not
   care how a new filing is discovered. So C1 is not a stepping stone toward
   C2 — it is a discovery mechanism you would then delete.
3. **The cold-start primitive C1 would provide already exists.** It is
   `fetch_insider_purchase_flag` itself (see §2.3).

### 2.2 Proposed `_step_insider_flag` control flow under C2

The step restructures from "loop tickers, call a per-ticker lookup" to "advance
shared state once, then answer every ticker from state":

```
1. Load active universe + resolved CIKs (as today, no requests — cached map).
2. Read watermarks. Partition tickers into:
     COLD   — no watermark row, or watermark predicate ≠ current config
     WARM   — watermark present and predicate-compatible
3. Advance WARM state via the daily index:
     for each business day D in (min(WARM watermarks), run_date]:
         fetch /Archives/edgar/daily-index/YYYY/QTRn/form.D.idx   (1 request)
         parse Form 4 rows -> (issuer CIK, accession, filing date)
         keep only CIKs in the universe
     fetch each kept filing's raw ownership XML                   (~646/day total)
     -> INSERT ... ON CONFLICT DO NOTHING into insider_purchase_events
     -> advance every WARM ticker's watermark to run_date
4. Cold-start COLD tickers via the existing live path
   (fetch_insider_purchase_flag's full 90-day scan), writing events + watermark.
5. Compute each ticker's flag from insider_purchase_events over
   (run_date - lookback, run_date], filtered by the current predicate.
6. UPDATE ticker_fundamentals.insider_trade_flag — byte-identical to today.
```

Steady state: step 4 is empty, step 3 is 1 index request + the day's genuinely
new universe filings. Steps 5–6 are pure SQL, zero requests.

### 2.3 Missed-day and gap handling — the main correctness risk

C2's guarantee is only as good as "we processed every business day". Proposed
discipline, deliberately fail-loud:

- Process **every** business day in `(watermark, run_date]`, not just today —
  a pipeline that didn't run for three days catches up automatically.
- A 404 on a **known non-business day** (per `app/utils/trading_calendar.py`,
  which already hard-gates with `RuntimeError`) → treat as empty, advance.
- A 404 or fetch failure on a **business day** → **do not advance the
  watermark**, emit a warning, and fall back to the live per-ticker path for
  that run. A silently-skipped index day produces false negatives that look
  exactly like "no insider purchases", which is precisely the failure mode
  Part A just fixed. This must not be reintroduced.
- If the gap exceeds the lookback window, treat every ticker as COLD rather than
  replaying a long index range.

Open question for the reviewer: SEC daily-index availability horizon. Verified
live for 2026-07-17 (795 KB, 1,289 Form-4 rows, 630 distinct accessions, issuer
CIKs present); the retention limit for older dates has not been probed.

### 2.4 Relationship to Part B

Part B's bulk loader is the natural **cold-start bulk-loader** for step 4 when
many tickers are COLD at once (initial rollout, or a config widening that
invalidates every watermark). It writes the same `insider_purchase_events` rows
with `source_provider = 'sec_edgar_bulk'`. That is a natural extension, not a
requirement, and is **not** proposed for the live daily path — consistent with
the coder note.

---

## 3. Does `fetch_insider_purchase_flag`'s public contract change?

**No. Recommend leaving it exactly as it is** — same signature, same
`bool | None` trichotomy, same statelessness, no DB access.

Justification:

- It becomes the **cold-start and repair primitive** (§2.2 step 4) and the
  **fallback** when state is unavailable, stale, or a gap was detected. Those
  are its current semantics, unchanged.
- It is the **parity reference**. The only way to validate an incremental,
  stateful path is to compare it against a stateless one that recomputes from
  scratch. Folding state into it would delete the thing that can check it —
  the same reason Part B kept it untouched and validated against it.
- It has no DB dependency, which is why it is trivially testable offline.
  Adding persistence would drag `DuckDBManager` into a provider module, against
  the project's provider/service separation.

New surface is added **alongside** it, not inside it. Proposed placement, a new
service module rather than a provider (it owns persistence and orchestration
policy, not a data source):

```
app/services/fundamentals/insider_event_store.py

  parse_daily_index(index_text, universe_ciks) -> list[FilingRef]   # pure
  events_from_filing(xml_text, ...) -> PurchaseEvent | None         # pure
  flag_from_events(events, as_of_date, *, lookback_days,
                   min_value_usd, exclude_10b5_1) -> bool           # pure
  # plus thin DuckDBManager-backed read/write helpers
```

The pure functions carry all the logic and stay offline-testable, matching the
shape of `edgar_insider_provider` and `edgar_insider_bulk_loader`.

**What does change:** `_build_insider_lookup` currently returns a per-ticker
callable `(ticker, run_date, cik) -> bool | None`. Under C2 the natural unit is
per-run, not per-ticker, so that helper is replaced by the §2.2 flow. The
per-ticker `bool | None` result reaching `ticker_fundamentals` is preserved
exactly — `None` now additionally covers "state stale for this ticker and the
live fallback also failed".

---

## 4. Summary of what needs ratifying before implementation

1. **Is the daily step actually going to run at 3,911-ticker scale?** (§0) If
   not, Part C should be deferred.
2. Two new tables (`insider_purchase_events`, `insider_scan_watermark`) in
   `schema_manager.py`, created for all three DB roles.
3. Build **C2 (daily full index)** first and skip C1 entirely (§2.1).
4. The store-wide/filter-narrow predicate policy (§1.3) — this is what makes
   config changes safe, and it costs a slightly wider scan.
5. Fail-loud gap handling (§2.3): a business-day index failure must **not**
   advance the watermark.
6. `fetch_insider_purchase_flag` stays unchanged; new logic lands in a new
   service module (§3).

---

## Anomalies

**C-A1 — the daily-index retention horizon is unverified.** The investigation
confirmed one date live (2026-07-17). If SEC prunes older daily indices, the
catch-up path in §2.3 has a bounded reach, and the "gap exceeds lookback → treat
as COLD" rule becomes load-bearing rather than defensive. Worth probing before
implementation.

**C-A2 — `SEC_USER_AGENT` is unset in this environment.** Both the live path and
any Part C work are inert until it is configured. Not a code issue; flagged
because it means Part C cannot currently be validated end-to-end in prod even if
built.

**C-A3 — the existing step's `_SQL_EARNINGS_TICKERS` / existing-row gate would
still bound Part C's usefulness.** `_step_insider_flag` only updates tickers
that already have a `ticker_fundamentals` row for `run_date`. Part C reduces
*discovery* cost across the universe, but if only 46 rows exist to update, the
saving is theoretical. This is the same point as §0, restated at the code level.
