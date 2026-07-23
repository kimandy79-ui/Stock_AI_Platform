# Insider Flag — Fix B-A1: Live Path Must Verify Issuer CIK, Not Just Filer CIK

**Date:** 2026-07-21
**Scope:** Targeted correctness fix to `app/providers/edgar_insider_provider.py`
plus regression tests. Follow-up to the Part B parity harness, which surfaced
this as a **live-path defect**, not a bulk-data defect.
**Files:** `app/providers/edgar_insider_provider.py` (modified),
`tests/test_edgar_insider_provider.py` (4 new tests + fixture fidelity)
**Not touched:** `edgar_insider_bulk_loader.py`, `max_candidate_filings`, any
threshold, the XOM/CIK-resolution issue. **No commit.**

---

## 1. The defect

`submissions.json` for a CIK lists Form 4s where that CIK is the **issuer**
*and* Form 4s where it is merely a **reporting owner** of some other company.
The two roles have separate top-level flags (`insiderTransactionForIssuerExists`
vs `insiderTransactionForOwnerExists`, both can be set), and **the role is not
recoverable from the filing list at all** — only from the filing XML's
`<issuer><issuerCik>`. The live path read neither, so it treated every Form 4
under a ticker's CIK as that ticker's own.

Confirmed on `AEI` (Alset Inc., CIK `0001750106`), accession
`0001493152-26-027990`, filed 2026-06-09 — a real, well-formed, non-10b5-1
$500K open-market purchase (250,000 sh × $2.00) where the issuer is **HWH
International** (CIK `0001897245`) and Alset is the reporting owner. It was
AEI's *only* Form 4 in the 90-day window, so it alone produced
`insider_trade_flag = True` for AEI off another company's purchase.

**Class of affected tickers:** anything that is itself an insider of another
company — holdcos, parent/affiliate structures, 10%+ corporate owners.

---

## 2. The diff

Three changes in `app/providers/edgar_insider_provider.py`. (The working tree
also carries the uncommitted Part A / XSL-path fix; the hunks below are the
B-A1 delta only.)

**(a) `_is_qualifying_purchase` — new required keyword + the gate.** Made
keyword-**only** and required rather than defaulted, so the check cannot be
silently skipped by a future caller:

```python
 def _is_qualifying_purchase(
     xml_text: str,
     min_transaction_value_usd: float,
     exclude_10b5_1: bool,
     *,
+    expected_cik: str,
     ticker: str = "",
     url: str = "",
 ) -> bool:
```

```python
         return False
 
+    # Issuer-role gate. Zero-padded to 10 the same way the submissions URL's
+    # CIK is (``str(cik).zfill(10)``), so a filing writing the CIK unpadded
+    # still matches. A document with no <issuerCik> at all is not rejected:
+    # real Form 4s always carry one, so an absent value means a shape this
+    # module doesn't recognize, and the transaction gates below still apply.
+    issuer_cik = (root.findtext("issuer/issuerCik") or "").strip()
+    if issuer_cik and issuer_cik.zfill(10) != expected_cik:
+        _LOG.debug(
+            "edgar_insider_provider: skipping filing whose issuer is another CIK "
+            "ticker=%s expected_cik=%s issuer_cik=%s url=%s",
+            ticker or "?",
+            expected_cik,
+            issuer_cik,
+            url or "?",
+        )
+        return False
+
     if exclude_10b5_1:
```

**(b) Call site** in `fetch_insider_purchase_flag` passes the already-computed
`cik_padded` — the same `str(cik).zfill(10)` value used to build the
submissions URL. No second padding convention introduced:

```python
             if _is_qualifying_purchase(
                 xml_text,
                 min_transaction_value_usd,
                 exclude_10b5_1,
+                expected_cik=cik_padded,
                 ticker=ticker,
                 url=url,
             ):
```

**(c) Module docstring** corrected. Its step-1 paragraph previously asserted the
opposite of the truth — "*lists an issuer's recent filings … where this CIK is
the issuer*" — which is precisely the wrong belief that produced the bug. It now
states that the list mixes both roles and that the issuer check therefore lives
in step 2.

### Two judgement calls, stated explicitly

- **Placement.** The gate cannot live in the candidate-selection loop: issuer
  identity is not present in `submissions.json`. It has to be after the XML
  parse, so the request is still spent on a rejected filing. The saving is
  correctness, not cost.
- **Absent `<issuerCik>` is *not* a rejection** (`if issuer_cik and ...`, as the
  coder note specified). Real Form 4s always carry one; treating "absent" as
  "mismatched" would convert an unrecognized document shape into a confident
  `False` — the same silent-false-negative failure mode the XSL parse bug just
  demonstrated. A document with no issuer block still faces every transaction
  gate below.

Logged at DEBUG, not WARNING: for a genuine holdco this fires on every run and
is normal, not anomalous. That log line is also what instrumented the parity
re-run below.

---

## 3. Tests

### New regression tests (4)

| test | asserts |
|---|---|
| `test_filing_where_queried_cik_is_reporting_owner_not_issuer_is_rejected` | The AEI/HWH case verbatim — real accession, filing date, issuer CIK `0001897245`, 250,000 sh × $2.00, `aff10b5One=0`. Qualifying in **every** other respect; result is `False`. |
| `test_matching_issuer_cik_still_qualifies_when_filing_writes_it_unpadded` | No false rejection from padding mismatch: filing writes `1326380`, query uses `0001326380` → still `True`. |
| `test_filing_without_an_issuer_block_is_not_rejected_by_the_issuer_gate` | Absent `<issuer>` falls through to the transaction gates (`True`), per the judgement call above. |
| `test_owner_role_filing_does_not_mask_a_later_real_issuer_purchase` | A rejected owner-role filing must not short-circuit the loop: 2 candidates, first is another issuer's, second is the ticker's own qualifying purchase → `True`, and **both** were fetched. |

That last one guards a real regression risk: the gate returns `False` from
inside the same predicate the early-exit loop calls, so an implementation that
mistook "reject this filing" for "reject this ticker" would pass the first three
tests and fail only here.

### Fixture fidelity change

The existing `_form4_xml` builder emitted no `<issuer>` block at all, so every
pre-existing test would have exercised the permissive absent-issuer branch and
proven nothing. It now emits a real `<issuer><issuerCik>` defaulting to the
queried CIK (`issuer_cik=None` opts out), matching real filings — so the 24
pre-existing tests now genuinely traverse the matching-issuer path.

### Results

```
tests/test_edgar_insider_provider.py .............................  28 passed
tests/test_edgar_insider_bulk_loader.py .......................     42 passed
                                                                    70 passed in 0.14s
```

No pre-existing test in the module changed behavior — 24 before, 28 after, all
passing.

**Full suite:** `pytest tests/` — **2,580 collected, 2,451 passed, 126 skipped,
3 failed.** Collection is 2,576 (the Part B baseline) + the 4 new tests, which
accounts for the delta exactly. The 3 failures are the known pre-existing set
logged 2026-07-08 and re-confirmed as B-A4 in the Part B report:

```
FAILED tests/test_data_validator.py::test_spec_documents_open_gaps_not_invented
FAILED tests/test_mutation_detector.py::test_spec_documents_open_gap_g1
FAILED tests/test_yahoo_provider.py::test_only_yahoo_provider_references_yfinance
```

**No new failures introduced.**

---

## 4. Parity harness re-run — 148/148

Same sample, same date, same lookback as the Part B run, so the numbers are
directly comparable. One instrument added: a DEBUG capture of the new gate, so
the run also reports *which* tickers had a filing rejected — measuring the fix's
reach without paying for a second unfixed live pass.

```
active universe: 3911 tickers
sample: 150 tickers (every 26th)
CIK resolved: 148/150
bulk: quarters=('2026q2',) issuers_with_purchases=27 in 4.0s
live: 299.7s

=== PARITY (post Fix B-A1) ===
compared      : 148
agree         : 148
disagree      : 0
live None     : 0
live  True    : 27
bulk  True    : 27

=== ISSUER-ROLE GATE ===
tickers with >=1 filing rejected as owner-role: 1
  AEI: 1 rejected; final live flag=False
      skipping filing whose issuer is another CIK ticker=AEI
      expected_cik=0001750106 issuer_cik=0001897245
      url=.../Archives/edgar/data/1750106/000149315226027990/ownership.xml

AEI live=False bulk=False
```

| | before fix | after fix |
|---|---|---|
| agree | 147 | **148** |
| disagree | 1 (AEI) | **0** |
| live `True` | 28 | **27** |
| bulk `True` | 27 | 27 |
| live `None` | 0 | 0 |

**148/148 (100%) agreement. Zero retrieval failures on either path.** The bulk
side reproduced its earlier run exactly (27 issuers, same quarter), so the
convergence is the live path moving onto bulk, not both drifting.

The gate instrument is the stronger evidence: it fired **exactly once in 148
tickers**, on the one filing already known to be misattributed, and changed
exactly one flag. The fix is not broadly suppressing `True` results — it
removed one wrong one and touched nothing else. **1 of 28 live `True` results
(3.6%) was a false positive**; n=1, so the rate stays indicative, not measured.

---

## 5. Anomalies

**C-A1 — the previously-reported live-path timing is not reproducible to within
10%.** Same 148 tickers, same work: Part B measured **335.2s**, this run
**299.7s** (−10.6%). The fix can only *remove* work (one early-exit `True`
became a full candidate-list walk for AEI — i.e. it should be marginally
*slower*), so the delta is network/throttle variance on SEC's side, not a code
effect. Consequence: **do not quote live-path wall-clock to 3 significant
figures.** The bulk-vs-live ratio should be stated as ~75–100×, not the precise
99× in the Part B report. The order-of-magnitude conclusion is unaffected.

**C-A2 — the pre-fix per-ticker results were not retained, so the "exactly one
flag flipped" claim rests on the gate instrument plus the aggregate, not on a
per-ticker before/after diff.** The evidence is strong and mutually
corroborating (gate fired once → on AEI; live `True` 28→27; AEI was the sole
prior mismatch), but it is inference, not a stored diff. A second unfixed live
pass to make it direct would cost ~5 minutes and ~1,600 SEC requests; judged
not worth it. Recording the reasoning rather than the certainty.

**C-A3 — `SEC_USER_AGENT` is still not set in this environment** (B-A3 stands,
unchanged). It had to be set for the process to run this harness. Both the live
path and the bulk loader remain inert in prod until it is configured.

**C-A5 — `pytest` does not emit its final counts line in this environment.**
Neither piped nor redirected; the short summary (`FAILED`/`SKIPPED` lines) is
present but `"N passed, M failed in Xs"` is absent, and `--collect-only -q`
likewise omits its total. The §3 totals were therefore derived — collection
summed from the per-file counts (2,580), skips counted from the `SKIPPED`
summary lines (126), failures from the `FAILED` lines (3), passes by
subtraction (2,451). Derived, not read off a summary line; flagging so the
numbers aren't over-trusted, and because it will bite anyone who greps for
`"passed"` to gate a run here.

**C-A4 — the fixture blind spot was pre-existing and would have hidden this
class of bug indefinitely.** `_form4_xml` never emitted an `<issuer>` element,
so no test — before or after Part A — asserted anything about issuer identity.
Any issuer-role logic would have been untested by construction. Fixed in §3;
noting it because it is the same shape as the Part A finding (fixtures using
`wk-form4.xml` where the real payload was `xslF345X05/wk-form4.xml`), and two
instances is a pattern: **this module's fixtures were built from the schema, not
from captured real payloads.** Worth a look at the remaining fixture fields
before the next change here.

---

## 6. Not done, per the coder note

- `edgar_insider_bulk_loader.py` untouched — immune by construction (joins on
  `ISSUERCIK`), re-confirmed by this run's 148/148.
- `max_candidate_filings` and every other threshold unchanged.
- XOM/CIK-resolution issue untouched — a different class (wrong CIK resolved for
  a ticker, vs. right CIK in the wrong role within a filing).
- No commit.

## 7. Downstream note (not acted on)

Any `insider_trade_flag` value already persisted in `ticker_fundamentals` by
the live path predates this fix and can carry the false positive. The field is
informational/display only and feeds no Step 4 eligibility, scoring, or routing
(scope unchanged), so nothing is mis-trading on it — but a stored `True` on a
holdco-class ticker is not trustworthy until recomputed. Flagging for the
backfill decision; **no cleanup performed.**
