"""P2.6 — wiring `price_lookup` so `valuation_band` stops being permanently "unknown".

`EdgarFundamentalsProvider` has always accepted a `price_lookup` callable and
threaded it into `compute_valuation_band`. Nothing ever passed one, so on the SEC
path the band was always `"unknown"` — a value `VALUATION_BAND_QUALITY` excludes,
so it silently contributed nothing to the fundamentals quality mean. Phase 4
quality was running on 4 of its 5 inputs.

What is proven here:

1. The lookup is **point-in-time**: bounded by `date <= run_date`, so a later
   price cannot leak in, and the callable refuses to answer for any other date.
2. It reads **close_raw**, never `close_adj` (as-reported EPS pairs with an
   unadjusted price; the adjusted series is retro-restated).
3. `valuation_band` is actually populated end-to-end.
4. Degradation is graceful: no price, or no `daily_prices` at all, yields
   `"unknown"` rather than an error.
5. An injected provider is never rebuilt (tests and DI keep working).

Fully offline.
"""

from __future__ import annotations

import logging
from datetime import date

import pytest

from app.providers import edgar_provider as ep
from app.services.pipeline.pipeline_orchestrator import (
    _SQL_LATEST_CLOSE_AS_OF,
    PipelineOrchestrator,
)

RUN_DATE = date(2026, 6, 15)


def _log():
    return logging.getLogger("test_p2_6")


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConnection:
    def __init__(self, db):
        self._db = db

    def execute(self, sql, params=None):
        self._db.executed.append((sql, params))
        if "daily_prices" in sql:
            if self._db.raise_on_prices:
                raise RuntimeError("no daily_prices table")
            return _FakeCursor(self._db.price_rows)
        return _FakeCursor([])

    def close(self):
        pass


class _FakeDb:
    def __init__(self, price_rows=None, raise_on_prices=False):
        self.price_rows = price_rows or []
        self.raise_on_prices = raise_on_prices
        self.executed: list[tuple] = []

    def connect(self, db_role, read_only=False):
        return _FakeConnection(self)


def _orchestrator(db, **kw) -> PipelineOrchestrator:
    return PipelineOrchestrator(
        db_manager=db,
        provider=object(),
        benchmark_loader=object(),
        universe_engine=object(),
        ingestion_engine=object(),
        validation_engine=object(),
        mutation_engine=object(),
        feature_engine=object(),
        regime_engine=object(),
        eligibility_engine=object(),
        setup_validation_engine=object(),
        proposal_engine=object(),
        outcome_creator=object(),
        outcome_processor=object(),
        config_service=object(),
        diagnostics_service=object(),
        **kw,
    )


# --------------------------------------------------------------------------- #
# 1 + 2. Point-in-time, and close_raw not close_adj.
# --------------------------------------------------------------------------- #
class TestPriceLookupIsPointInTime:
    def test_query_is_bounded_by_run_date_and_selects_close_raw(self):
        assert "date <= ?" in _SQL_LATEST_CLOSE_AS_OF
        assert "close_raw" in _SQL_LATEST_CLOSE_AS_OF
        assert "close_adj" not in _SQL_LATEST_CLOSE_AS_OF

    def test_lookup_is_parameterised_with_run_date(self):
        db = _FakeDb([("AAPL", 190.0)])
        orch = _orchestrator(db)
        orch._make_price_lookup("prod", RUN_DATE, _log())

        reads = [c for c in db.executed if "daily_prices" in c[0]]
        assert reads and reads[0][1] == [RUN_DATE]

    def test_lookup_refuses_a_date_it_was_not_built_for(self):
        """A silent answer for the wrong date is exactly how look-ahead creeps in."""
        orch = _orchestrator(_FakeDb([("AAPL", 190.0)]))
        lookup = orch._make_price_lookup("prod", RUN_DATE, _log())

        assert lookup("AAPL", RUN_DATE) == 190.0
        with pytest.raises(ValueError, match="bound to"):
            lookup("AAPL", date(2026, 6, 16))

    def test_unknown_ticker_yields_none(self):
        orch = _orchestrator(_FakeDb([("AAPL", 190.0)]))
        lookup = orch._make_price_lookup("prod", RUN_DATE, _log())
        assert lookup("ZZZZ", RUN_DATE) is None

    @pytest.mark.parametrize("bad", [None, 0.0, -1.0])
    def test_unusable_close_is_dropped(self, bad):
        orch = _orchestrator(_FakeDb([("AAPL", bad)]))
        lookup = orch._make_price_lookup("prod", RUN_DATE, _log())
        assert lookup("AAPL", RUN_DATE) is None

    def test_missing_daily_prices_degrades_to_no_lookup(self):
        """No price source must mean valuation_band='unknown', never a crash."""
        orch = _orchestrator(_FakeDb(raise_on_prices=True))
        assert orch._make_price_lookup("prod", RUN_DATE, _log()) is None


# --------------------------------------------------------------------------- #
# 3. valuation_band actually gets populated now.
# --------------------------------------------------------------------------- #
class TestValuationBandIsPopulated:
    def _facts(self, eps: float) -> dict:
        return {
            "facts": {
                "us-gaap": {
                    "EarningsPerShareDiluted": {
                        "units": {
                            "USD/shares": [
                                {"end": "2025-12-31", "filed": "2026-02-01",
                                 "val": eps, "form": "10-K"},
                            ]
                        }
                    }
                }
            }
        }

    @pytest.mark.parametrize(
        "price,eps,expected",
        [
            (100.0, 10.0, "cheap"),      # P/E 10
            (200.0, 10.0, "fair"),       # P/E 20
            (400.0, 10.0, "expensive"),  # P/E 40
        ],
    )
    def test_band_buckets_from_the_supplied_price(self, price, eps, expected):
        snap = ep.compute_fundamentals_from_companyfacts(
            self._facts(eps), "AAPL", RUN_DATE, price=price
        )
        assert snap.valuation_band == expected

    def test_absent_price_still_yields_unknown(self):
        snap = ep.compute_fundamentals_from_companyfacts(
            self._facts(10.0), "AAPL", RUN_DATE, price=None
        )
        assert snap.valuation_band == "unknown"

    def test_loss_making_company_is_unknown_even_with_a_price(self):
        """A P/E on negative EPS is meaningless, not 'cheap'."""
        snap = ep.compute_fundamentals_from_companyfacts(
            self._facts(-5.0), "AAPL", RUN_DATE, price=100.0
        )
        assert snap.valuation_band == "unknown"

    def test_provider_threads_the_lookup_into_the_band(self):
        """End-to-end through EdgarFundamentalsProvider with an injected lookup."""
        provider = ep.EdgarFundamentalsProvider(
            fetch_json=lambda url: self._facts(10.0),
            ticker_to_cik=lambda t: "0000320193",
            price_lookup=lambda ticker, as_of: 100.0,
        )
        result = provider.get_fundamentals("AAPL", RUN_DATE)
        assert result.metadata["fundamentals"].valuation_band == "cheap"

    def test_without_a_lookup_the_band_stays_unknown(self):
        """The pre-P2.6 production behaviour, pinned so the regression is visible."""
        provider = ep.EdgarFundamentalsProvider(
            fetch_json=lambda url: self._facts(10.0),
            ticker_to_cik=lambda t: "0000320193",
        )
        result = provider.get_fundamentals("AAPL", RUN_DATE)
        assert result.metadata["fundamentals"].valuation_band == "unknown"


# --------------------------------------------------------------------------- #
# 4 + 5. Provider resolution.
# --------------------------------------------------------------------------- #
class TestProviderResolution:
    def test_injected_provider_is_used_verbatim(self):
        sentinel = object()
        orch = _orchestrator(_FakeDb(), fundamentals_provider=sentinel)
        assert orch._resolve_fundamentals_provider("prod", RUN_DATE, _log()) is sentinel

    def test_injected_provider_triggers_no_price_read(self):
        db = _FakeDb([("AAPL", 190.0)])
        orch = _orchestrator(db, fundamentals_provider=object())
        orch._resolve_fundamentals_provider("prod", RUN_DATE, _log())
        assert not [c for c in db.executed if "daily_prices" in c[0]]

    def test_default_provider_is_built_with_a_price_lookup(self):
        db = _FakeDb([("AAPL", 190.0)])
        orch = _orchestrator(db)
        provider = orch._resolve_fundamentals_provider("prod", RUN_DATE, _log())

        assert isinstance(provider, ep.EdgarFundamentalsProvider)
        assert provider._price_lookup is not None
        assert provider._price_lookup("AAPL", RUN_DATE) == 190.0

    def test_no_provider_is_constructed_at_init(self):
        """__init__ cannot build the default provider: db_role/run_date arrive later."""
        orch = _orchestrator(_FakeDb())
        assert orch._fundamentals_provider is None


# --------------------------------------------------------------------------- #
# Documented limitations — pinned so nobody 'fixes' them by accident.
# --------------------------------------------------------------------------- #
class TestDocumentedLimitations:
    def test_band_is_a_trailing_fy_pe_not_ttm(self):
        """extract_annual_series filters to 10-K, and the band reads index 0, so
        the divisor is the last FULL-YEAR diluted EPS -- up to ~15 months stale.
        Coarse by construction; documented, not fixed here."""
        assert ep._ANNUAL_FORM == "10-K"

    def test_one_day_staleness_is_structural(self):
        """fundamentals_refresh runs before price_ingestion, so run_date's own bar
        is not yet ingested and the lookup resolves to the prior close."""
        from app.services.pipeline.pipeline_orchestrator import STEP_NAMES

        assert STEP_NAMES.index("fundamentals_refresh") < STEP_NAMES.index("price_ingestion")
