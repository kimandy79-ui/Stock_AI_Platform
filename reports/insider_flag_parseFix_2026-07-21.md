# Insider Flag — Part A: XSL Parse Bug Fix

**Date:** 2026-07-21
**Scope:** Correctness fix only. No optimization, no config change, no backfill, no commit.
**Files changed:** `app/providers/edgar_insider_provider.py`, `tests/test_edgar_insider_provider.py`

---

## What was wrong

`submissions.json` reports an ownership filing's `primaryDocument` as the
**XSL-rendered HTML** view, not the raw ownership XML. The module fed that path
straight into `_FILING_XML_URL`, so every Form 4 fetch returned HTML,
`ET.fromstring` raised `ParseError`, and `_is_qualifying_purchase` returned
`False` — indistinguishable from "checked, no qualifying purchase".

`insider_trade_flag` was therefore structurally incapable of returning `True`.

## The fix

Strip the leading `xsl*/` renderer directory from `primaryDocument` before
building the URL:

```python
def _raw_ownership_document_path(primary_doc: str) -> str:
    head, sep, tail = primary_doc.partition("/")
    if sep and tail and head.lower().startswith(_XSL_RENDER_DIR_PREFIX):
        return tail
    return primary_doc
```

Matched on the `xsl` prefix case-insensitively rather than on the two observed
literals (`xslF345X05`, `xslF345X06`), so a future form-schema version needs no
code change. Paths that are already raw pass through untouched.

## Parse failures are now loud

`_is_qualifying_purchase` gained optional `ticker`/`url` keyword arguments used
for one thing: a `WARNING` on `ParseError`, distinct from the silent
"parsed fine, nothing qualified" path.

```python
    except ET.ParseError as exc:
        _LOG.warning(
            "edgar_insider_provider: unparseable filing document ticker=%s url=%s: %s",
            ticker or "?", url or "?", exc,
        )
        return False
```

The return value is unchanged (`False`, so the loop moves to the next
candidate) — only the visibility changes. This is the specific ambiguity that
let a 100% failure rate survive a real 50-ticker batch run and the existing
test suite.

---

## Live verification (read-only, 3 public GETs)

Against MSFT accession `0000789019-26-000028`, the exact case the investigation
named:

```
primaryDocument as reported : 'xslF345X05/form4.xml'
after strip                 : 'form4.xml'

  xslF345X05/form4.xml   head='<!DOCTYPE html PUBLIC "-//W3C//DTD HTML 4.01 Transitional//E'   qualifying=False
  form4.xml              head='<?xml version="1.0"?> <ownershipDocument>      <schemaVersio'    qualifying=True
```

The new warning fired on the pre-fix path, as intended:

```
WARNING edgar_insider_provider: unparseable filing document ticker=MSFT
url=.../000078901926000028/xslF345X05/form4.xml: mismatched tag: line 34, column 2
```

This reproduces the investigation's finding end-to-end: the reported path is
HTML and yields `False`; the stripped path parses as `<ownershipDocument>` and
yields `True`.

---

## Tests

Added to `tests/test_edgar_insider_provider.py` (7 new tests, 17 → 24):

1. **`test_xsl_render_prefix_is_stripped_to_reach_raw_ownership_xml`** — the
   regression test. Fixture uses the real-shaped `xslF345X05/wk-form4.xml`, and
   the fake fetcher mirrors the real server: HTML at the rendered path,
   parseable XML at the raw path. Asserts both the resulting flag (`True`) and
   the exact URL fetched.
2. **`test_raw_ownership_document_path`** (5 parametrized cases) — both observed
   renderer prefixes, a hypothetical future one, an already-raw path, and empty.
3. **`test_unparseable_filing_logs_a_warning_distinct_from_no_purchase`** —
   asserts the warning fires on unparseable content *and* that a clean
   no-purchase filing logs nothing.

All pre-existing tests still pass unmodified, including the ones using bare
`wk-form4.xml`-style fixtures — confirming the fix is a no-op for already-raw
paths.

```
tests/test_edgar_insider_provider.py                24 passed
tests/test_edgar_provider_insider_lookup.py         |
tests/test_pipeline_fundamentals_insider_wiring.py  | 54 passed total, 0 failed
```

**Full suite:** `pytest tests/` — **2,576 collected, 3 failed**, and those 3 are
exactly the known pre-existing failures logged 2026-07-08 as out-of-scope:

```
FAILED tests/test_data_validator.py::test_spec_documents_open_gaps_not_invented
FAILED tests/test_mutation_detector.py::test_spec_documents_open_gap_g1
FAILED tests/test_yahoo_provider.py::test_only_yahoo_provider_references_yfinance
```

The third is the edgar/yahoo overlap check; verified it predates this work —
`git grep yfinance HEAD -- app/providers/edgar_provider.py` returns the same
`compute_fundamentals_from_yfinance_info` references at HEAD. The other two are
the spec-path lookups. Not fixed, per that standing note.

---

## Correctness impact

`insider_trade_flag` moves from always-`False` to ~16.9% `True` across the
universe (investigation's measured figure). Re-confirmed the field's blast
radius: it is referenced only in `default_configs.py`, `schema_manager.py`,
`pipeline_orchestrator.py`, `provider_interface.py`, and the two provider
modules. **No validator, scoring, or routing path reads it.** No Step 3/4/5
output changes.

Prod is unaffected as of now: `ticker_fundamentals` holds 46 rows, all with
`insider_trade_flag = NULL` — nothing wrong has been persisted.

---

## Anomalies

**A1 — the previously-reported renderer prefix was `xslF345X0N`; the live path
for MSFT is `xslF345X05`, and the module now matches on `xsl` alone.** Not a
defect, but worth pinning: the fix deliberately does not hardcode either
observed literal. `xslF345X05` and `xslF345X06` were both observed in the
investigation's 3,009-filing harvest; treating the version suffix as opaque
avoids a re-break when SEC ships `xslF345X07`.

**A2 — a well-formed HTML stub does not reproduce the bug.** The first draft of
the parse-failure test used `"<!DOCTYPE html><html></html>"`, which
`ET.fromstring` parses *successfully* — it is valid XML. The test passed
vacuously until the fixture was replaced with realistic renderer output
containing an unclosed `<br>`. Flagging because any future test in this area
needs genuinely malformed HTML, not HTML-shaped XML.

**A3 — `SEC_USER_AGENT` is not set in the environment.** The live verification
above required setting it for the process; it is not present in the shell or
resolvable via `resolve_sec_user_agent()`. Per the orchestrator's own pre-run
check, `insider_flag_refresh` will warn and skip every ticker until this is
configured — the Part A fix is inert in prod until then. Not fixed here
(configuration, not code).

---

## Not done

- No commit.
- `max_candidate_filings` and every other threshold unchanged.
- No config change, no backfill run.
- The XOM/CIK-resolution issue (investigation A2) untouched, per the coder note.
