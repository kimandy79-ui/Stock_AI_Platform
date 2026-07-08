"""Tests for the SEC EDGAR fair-access hardening pass: compliant User-Agent
resolution, the rate-limited/retrying HTTP client, the on-disk
company_tickers.json TTL cache, and the yfinance fallback path.

Fully offline except for the ``requests_mock`` fixture (a well-established
test-only library that intercepts ``requests`` at the transport layer, so no
real network call ever happens — it just lets us assert on what *would have*
been sent, e.g. the outgoing ``User-Agent`` header).
"""

from __future__ import annotations

import json
import os
import time
from datetime import date
from pathlib import Path

import pytest

from app.providers import edgar_provider as ep
from app.providers.provider_interface import FundamentalSnapshot


# --------------------------------------------------------------------------- #
# resolve_sec_user_agent
# --------------------------------------------------------------------------- #
class TestResolveSecUserAgent:
    def test_explicit_value_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SEC_USER_AGENT", "EnvApp env@example.com")
        result = ep.resolve_sec_user_agent("ExplicitApp explicit@example.com")
        assert result == "ExplicitApp explicit@example.com"

    def test_env_var_used_when_no_explicit_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SEC_USER_AGENT", "StockAnalyzer kimandy79tr@gmail.com")
        result = ep.resolve_sec_user_agent(None)
        assert result == "StockAnalyzer kimandy79tr@gmail.com"

    def test_raises_when_neither_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SEC_USER_AGENT", raising=False)
        with pytest.raises(RuntimeError, match="SEC_USER_AGENT is not set"):
            ep.resolve_sec_user_agent(None)

    def test_empty_string_explicit_falls_back_to_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SEC_USER_AGENT", "EnvApp env@example.com")
        result = ep.resolve_sec_user_agent("")
        assert result == "EnvApp env@example.com"

    def test_whitespace_only_env_var_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SEC_USER_AGENT", "   ")
        with pytest.raises(RuntimeError, match="SEC_USER_AGENT is not set"):
            ep.resolve_sec_user_agent(None)


# --------------------------------------------------------------------------- #
# _SecHttpClient — User-Agent header (real request-mock assertion)
# --------------------------------------------------------------------------- #
class TestSecHttpClientUserAgent:
    def test_sends_configured_user_agent_on_every_request(self, requests_mock) -> None:
        url = "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"
        requests_mock.get(url, json={"facts": {}})
        client = ep._SecHttpClient(user_agent="StockAnalyzer kimandy79tr@gmail.com")
        result = client.get_json(url)
        assert result == {"facts": {}}
        assert requests_mock.last_request.headers["User-Agent"] == (
            "StockAnalyzer kimandy79tr@gmail.com"
        )

    def test_header_persists_across_multiple_requests_same_session(self, requests_mock) -> None:
        url1 = "https://data.sec.gov/x1.json"
        url2 = "https://data.sec.gov/x2.json"
        requests_mock.get(url1, json={"a": 1})
        requests_mock.get(url2, json={"b": 2})
        client = ep._SecHttpClient(
            user_agent="StockAnalyzer kimandy79tr@gmail.com", sleep_fn=lambda s: None
        )
        client.get_json(url1)
        client.get_json(url2)
        assert all(
            req.headers["User-Agent"] == "StockAnalyzer kimandy79tr@gmail.com"
            for req in requests_mock.request_history
        )

    def test_missing_user_agent_raises_before_any_request(
        self, requests_mock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SEC_USER_AGENT", raising=False)
        client = ep._SecHttpClient(user_agent=None)
        with pytest.raises(RuntimeError, match="SEC_USER_AGENT is not set"):
            client.get_json("https://data.sec.gov/x.json")
        # No mock registered for this URL -- if the client had tried to
        # actually send a request, requests_mock would raise NoMockAddress
        # instead of letting our RuntimeError propagate cleanly.
        assert requests_mock.call_count == 0


# --------------------------------------------------------------------------- #
# _SecHttpClient — retry / rate-limit policy
# --------------------------------------------------------------------------- #
class TestSecHttpClientRetryPolicy:
    def test_403_raises_immediately_no_retry(self, requests_mock) -> None:
        url = "https://data.sec.gov/x.json"
        requests_mock.get(url, status_code=403)
        client = ep._SecHttpClient(user_agent="A a@b.com", sleep_fn=lambda s: None)
        with pytest.raises(RuntimeError, match="403 Forbidden"):
            client.get_json(url)
        assert requests_mock.call_count == 1  # never retried

    def test_429_then_200_retries_and_succeeds(self, requests_mock) -> None:
        url = "https://data.sec.gov/x.json"
        requests_mock.get(
            url,
            [
                {"status_code": 429},
                {"status_code": 200, "json": {"ok": True}},
            ],
        )
        sleeps: list[float] = []
        client = ep._SecHttpClient(user_agent="A a@b.com", sleep_fn=sleeps.append)
        result = client.get_json(url)
        assert result == {"ok": True}
        assert requests_mock.call_count == 2
        assert len(sleeps) >= 1  # backoff slept at least once

    def test_5xx_exhausts_retries_then_raises(self, requests_mock) -> None:
        url = "https://data.sec.gov/x.json"
        requests_mock.get(url, status_code=503)
        client = ep._SecHttpClient(
            user_agent="A a@b.com", max_retries=2, sleep_fn=lambda s: None
        )
        with pytest.raises(Exception):  # requests.HTTPError once retries exhausted
            client.get_json(url)
        assert requests_mock.call_count == 3  # initial + 2 retries

    def test_backoff_is_exponential(self, requests_mock) -> None:
        url = "https://data.sec.gov/x.json"
        requests_mock.get(
            url,
            [
                {"status_code": 429},
                {"status_code": 429},
                {"status_code": 200, "json": {"ok": True}},
            ],
        )
        sleeps: list[float] = []
        # min_request_interval_sec=0 isolates the retry-backoff sleeps from
        # the (separate) rate-limit throttle sleep, which would otherwise
        # also land in this same list.
        client = ep._SecHttpClient(
            user_agent="A a@b.com", min_request_interval_sec=0.0, sleep_fn=sleeps.append
        )
        client.get_json(url)
        assert sleeps[1] > sleeps[0]  # each backoff longer than the last

    def test_throttle_sleeps_between_calls_under_min_interval(self, requests_mock) -> None:
        url1 = "https://data.sec.gov/x1.json"
        url2 = "https://data.sec.gov/x2.json"
        requests_mock.get(url1, json={"a": 1})
        requests_mock.get(url2, json={"b": 2})

        fake_now = [1000.0]

        def fake_time() -> float:
            return fake_now[0]

        sleeps: list[float] = []

        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            fake_now[0] += seconds

        client = ep._SecHttpClient(
            user_agent="A a@b.com",
            min_request_interval_sec=0.5,
            sleep_fn=fake_sleep,
            time_fn=fake_time,
        )
        client.get_json(url1)
        # No time passes between the two calls -> throttle must sleep ~0.5s.
        client.get_json(url2)
        assert sleeps and sleeps[-1] == pytest.approx(0.5)

    def test_no_throttle_needed_when_enough_time_already_elapsed(self, requests_mock) -> None:
        url1 = "https://data.sec.gov/x1.json"
        url2 = "https://data.sec.gov/x2.json"
        requests_mock.get(url1, json={"a": 1})
        requests_mock.get(url2, json={"b": 2})

        fake_now = [1000.0]

        def fake_time() -> float:
            return fake_now[0]

        sleeps: list[float] = []
        client = ep._SecHttpClient(
            user_agent="A a@b.com",
            min_request_interval_sec=0.1,
            sleep_fn=sleeps.append,
            time_fn=fake_time,
        )
        client.get_json(url1)
        fake_now[0] += 10.0  # plenty of time has passed
        client.get_json(url2)
        assert sleeps == []  # no throttle sleep needed


# --------------------------------------------------------------------------- #
# On-disk company_tickers.json TTL cache
# --------------------------------------------------------------------------- #
class TestOnDiskTickerMapCache:
    def _payload(self) -> dict:
        return {"0": {"ticker": "AAPL", "cik_str": 320193}}

    def test_fresh_cache_hit_skips_refetch(self, tmp_path: Path) -> None:
        cache_path = tmp_path / "cache.json"
        cache_path.write_text(json.dumps(self._payload()), encoding="utf-8")
        calls: list[str] = []

        def fake_fetch(url: str) -> dict:
            calls.append(url)
            raise AssertionError("should not be called -- cache is fresh")

        provider = ep.EdgarFundamentalsProvider(
            fetch_json=fake_fetch, cache_path=cache_path, cache_ttl_seconds=86400,
        )
        cik = provider._default_ticker_to_cik("AAPL")
        assert cik == "0000320193"
        assert calls == []

    def test_stale_cache_triggers_refetch_and_rewrite(self, tmp_path: Path) -> None:
        cache_path = tmp_path / "cache.json"
        cache_path.write_text(json.dumps({"0": {"ticker": "OLD", "cik_str": 1}}), encoding="utf-8")
        old_time = time.time() - 100_000  # older than any reasonable TTL
        os.utime(cache_path, (old_time, old_time))

        def fake_fetch(url: str) -> dict:
            return self._payload()

        provider = ep.EdgarFundamentalsProvider(
            fetch_json=fake_fetch, cache_path=cache_path, cache_ttl_seconds=3600,
        )
        cik = provider._default_ticker_to_cik("AAPL")
        assert cik == "0000320193"
        # Rewritten with the fresh payload.
        assert json.loads(cache_path.read_text(encoding="utf-8")) == self._payload()

    def test_corrupt_cache_file_triggers_graceful_refetch(self, tmp_path: Path) -> None:
        cache_path = tmp_path / "cache.json"
        cache_path.write_text("not valid json {{{", encoding="utf-8")

        def fake_fetch(url: str) -> dict:
            return self._payload()

        provider = ep.EdgarFundamentalsProvider(
            fetch_json=fake_fetch, cache_path=cache_path, cache_ttl_seconds=86400,
        )
        cik = provider._default_ticker_to_cik("AAPL")
        assert cik == "0000320193"

    def test_missing_cache_file_fetches_and_creates_it(self, tmp_path: Path) -> None:
        cache_path = tmp_path / "subdir" / "cache.json"

        def fake_fetch(url: str) -> dict:
            return self._payload()

        provider = ep.EdgarFundamentalsProvider(fetch_json=fake_fetch, cache_path=cache_path)
        provider._default_ticker_to_cik("AAPL")
        assert cache_path.exists()
        assert json.loads(cache_path.read_text(encoding="utf-8")) == self._payload()

    def test_second_call_uses_in_memory_cache_not_disk_or_network(self, tmp_path: Path) -> None:
        cache_path = tmp_path / "cache.json"
        calls: list[str] = []

        def fake_fetch(url: str) -> dict:
            calls.append(url)
            return self._payload()

        provider = ep.EdgarFundamentalsProvider(fetch_json=fake_fetch, cache_path=cache_path)
        provider._default_ticker_to_cik("AAPL")
        provider._default_ticker_to_cik("AAPL")  # in-memory cache short-circuits this one
        assert len(calls) == 1


# --------------------------------------------------------------------------- #
# yfinance fallback — pure computation
# --------------------------------------------------------------------------- #
class TestComputeFundamentalsFromYfinanceInfo:
    def test_full_info_computes_reduced_field_set(self) -> None:
        info = {
            "earningsQuarterlyGrowth": 0.25,
            "debtToEquity": 45.0,  # yfinance reports as a percentage
            "trailingPE": 18.0,
        }
        snapshot = ep.compute_fundamentals_from_yfinance_info(info, "AAPL", date(2024, 6, 1))
        assert snapshot.source_provider == "yfinance_fallback"
        assert snapshot.eps_growth_trend == pytest.approx(0.25)
        assert snapshot.leverage_ratio == pytest.approx(0.45)
        assert snapshot.valuation_band == "fair"
        assert snapshot.piotroski_f_score is None
        assert snapshot.altman_z_score is None
        assert snapshot.insider_trade_flag is None
        assert snapshot.institutional_ownership_delta is None

    def test_missing_fields_degrade_to_none_or_unknown(self) -> None:
        snapshot = ep.compute_fundamentals_from_yfinance_info({}, "AAPL", date(2024, 6, 1))
        assert snapshot.eps_growth_trend is None
        assert snapshot.leverage_ratio is None
        assert snapshot.valuation_band == "unknown"

    def test_falls_back_to_earnings_growth_when_quarterly_absent(self) -> None:
        snapshot = ep.compute_fundamentals_from_yfinance_info(
            {"earningsGrowth": 0.1}, "AAPL", date(2024, 6, 1)
        )
        assert snapshot.eps_growth_trend == pytest.approx(0.1)


# --------------------------------------------------------------------------- #
# End-to-end: EdgarFundamentalsProvider with a fake yfinance module injected
# (exercises _build_default_yfinance_fallback's real wiring, not just a fully
# injected yfinance_fallback callable).
# --------------------------------------------------------------------------- #
class TestYfModuleInjection:
    def test_yf_module_is_used_to_build_default_fallback(self) -> None:
        class _FakeTicker:
            def __init__(self, symbol: str) -> None:
                self.info = {"trailingPE": 10.0, "debtToEquity": 20.0}

        class _FakeYfModule:
            @staticmethod
            def Ticker(symbol: str) -> "_FakeTicker":
                return _FakeTicker(symbol)

        def fake_fetch(url: str) -> dict:
            raise RuntimeError("sec down")

        provider = ep.EdgarFundamentalsProvider(
            fetch_json=fake_fetch,
            ticker_to_cik=lambda ticker: "0000320193",
            yf_module=_FakeYfModule(),
        )
        result = provider.get_fundamentals("AAPL", date(2024, 6, 1))
        assert result.status == "success_with_warnings"
        snapshot = result.metadata["fundamentals"]
        assert snapshot.source_provider == "yfinance_fallback"
        assert snapshot.valuation_band == "cheap"  # PE=10
