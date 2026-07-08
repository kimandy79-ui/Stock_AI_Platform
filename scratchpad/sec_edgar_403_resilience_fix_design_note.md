# SEC EDGAR 403 Fix + Resilience Hardening: Design Note

## Root cause

`app/providers/edgar_provider.py` sent a hardcoded, non-compliant
`User-Agent: "Stock_AI_Platform research (contact: local-only)"` on every
request to `sec.gov`/`data.sec.gov`. SEC EDGAR's fair-access policy rejects
(403 Forbidden) any request without a genuinely compliant
`"<App Name> <contact-email>"` header — so every ticker in every
`fundamentals_refresh` run failed identically.

## What changed

### 1. Compliant, configurable User-Agent

- New `resolve_sec_user_agent(explicit=None)` reads the `SEC_USER_AGENT`
  environment variable (via `app.config.env.get_str`, the project's existing
  typed env-getter, not hardcoded). Raises `RuntimeError` with an actionable
  message if unset — **checked before any network call is attempted**, so a
  missing config never produces a single wasted 403 round-trip.
- Documented in `.env.example` under a new section: this is public contact
  info, not a credential, so it belongs in `.env` (not keyring, per the
  project's existing credential-vs-config split).
- **Action needed from you:** no real `.env` file exists yet in this repo.
  Create one (`copy .env.example .env`) and set
  `SEC_USER_AGENT=StockAnalyzer kimandy79tr@gmail.com`, or set the OS
  environment variable directly, before the fundamentals step can reach SEC
  EDGAR at all (it will still run via the yfinance fallback either way, see
  below, but with only 5 of 5 currently-computed fields degrading to the
  reduced fallback set).

### 2. Shared session, never per-call headers

New `_SecHttpClient` class builds one `requests.Session()` lazily and calls
`session.headers.update({"User-Agent": ...})` exactly once — every
subsequent request through that client automatically carries the header;
there is no code path left where a call can "forget" to attach it.

### 3. Rate limiting + retry policy

- Throttled to 6 req/s (`_TARGET_REQUESTS_PER_SEC`), safely under SEC's
  published 10 req/s fair-access limit — a simple elapsed-time check before
  each request, sleeping only the remaining gap.
- Only 429 and 5xx are retried, with exponential backoff (1s/2s/4s, up to 3
  attempts). **403 is never retried** — it raises immediately with a message
  explaining why retrying won't help (bad/missing header or an IP-level
  block), rather than burning attempts on a request that can't succeed.

### 4. On-disk TTL cache for `company_tickers.json`

This file is ~large and effectively static. `EdgarFundamentalsProvider` now
reads/writes it under `settings.CACHE_DIR / "sec_company_tickers.json"`
(new `CACHE_DIR` constant added to `app/config/settings.py`, alongside the
existing `DATA_DIR`/`LOGS_DIR`/etc. — added to `REQUIRED_DIRECTORIES` too)
with a 24h TTL (`cache_ttl_seconds`, overridable). A fresh cache hit skips
the network call entirely; a stale/corrupt/missing cache falls through to a
normal fetch-and-write-through. Caching is best-effort — a disk write
failure (e.g. read-only filesystem) is swallowed, not fatal.

### 5. yfinance fallback with explicit source labeling

`get_fundamentals` now wraps the entire SEC path (CIK resolution +
companyfacts fetch + parsing) in one try/except. On **any** failure, it
attempts a yfinance-backed fallback
(`compute_fundamentals_from_yfinance_info`, pure function, reduced field
coverage — see its docstring for exactly what's computed vs. left `None`
and why) before giving up. Every returned `FundamentalSnapshot.source_provider`
is either `"sec_edgar"` or `"yfinance_fallback"` — **never silently blended**.
A fallback success returns `status="success_with_warnings"` with a warning
naming both the original SEC failure and that the fallback was used. If the
fallback also fails (or isn't available), the ticker returns a normal
`status="failed"` result — the pipeline's existing per-ticker
warning-not-crash handling in `_step_fundamentals` already treats this as
"unavailable, continue," unchanged.

**Known, documented coverage gap in the fallback path:** `piotroski_f_score`
and `altman_z_score` need two periods of full financial statements, which
`yfinance.Ticker.info` doesn't provide — both stay `None` from the fallback
rather than being approximated on partial data. `leverage_ratio` from
yfinance uses a **different basis** (debt/equity, from `debtToEquity`) than
the EDGAR path (debt/assets) — documented in the function's docstring, not
silently treated as the same metric.

### 6. Per-run source summary

`_step_fundamentals` (`pipeline_orchestrator.py`) now tracks a
`source_counts` dict (keyed by `source_provider`) plus an `unavailable`
count across all tickers in the run, and emits both a log line and
`ServiceResult.metadata["source_summary"]` /
`metadata["source_counts"]`, e.g.
`"Fundamentals: 42/50 from sec_edgar, 6/50 from yfinance_fallback, 2/50 unavailable"`.
This doesn't change the step's `status`/`warnings` semantics — it's
additive metadata, not a new failure condition.

## Testing

New `tests/test_edgar_provider_resilience.py` (26 tests): `User-Agent`
resolution (explicit/env/missing/whitespace-only), the actual outgoing
`User-Agent` header via `requests_mock` (a new dev-only dependency — added
to `requirements.txt` and `pyproject.toml`'s `dev` extra, chosen because
it's exactly the tool named in the request and avoids hand-rolling a fake
`requests` module), 403-never-retried, 429/5xx-retried-then-succeeds,
5xx-exhausts-retries, exponential backoff, throttle timing (both "sleeps
when too fast" and "doesn't sleep when enough time already passed"), the
on-disk cache (fresh hit / stale refetch+rewrite / corrupt-file graceful
refetch / missing-file creates it / in-memory cache shortcuts a second
call), the yfinance fallback pure function (full info / missing fields /
alternate growth field), and end-to-end fallback wiring via an injected
fake `yf_module` (mirrors `YahooProvider`'s existing injection pattern).

Updated `tests/test_edgar_provider.py`: the `fetch_json` injectable
signature changed from `(url, headers) -> dict` to `(url) -> dict` (headers
now live on the session, never threaded through call sites) — existing
tests updated to match. Tests that previously expected a bare SEC failure to
return `status="failed"` now explicitly inject
`yfinance_fallback=lambda ticker, as_of_date: None` to keep testing the
"both sources unavailable" path deterministically (no live network call);
new tests cover the "SEC fails, yfinance succeeds" path that wasn't
representable before this change existed.

Updated `tests/test_pipeline_fundamentals_step.py`: `_FakeFundamentalsProvider`
gained a `sources` param (per-ticker `source_provider` override); new
`TestStepFundamentalsSourceSummary` class covers all-sec_edgar,
mixed-sources-plus-unavailable, raised-exception-counts-as-unavailable, and
all-unavailable summary shapes.

Full pre-existing suites confirmed green (see full-run results).

## Non-goals confirmed unaffected

No changes to point-in-time discipline (`extract_annual_series` untouched),
Piotroski/Altman formulas (untouched), Step 4/5 fundamentals scoring
integration (untouched), or the `fundamentals_refresh` step's
already-refreshed-today guard / already-run semantics (untouched).
