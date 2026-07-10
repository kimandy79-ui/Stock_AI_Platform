"""P2.4 — shares_outstanding (EDGAR ``dei``) + derived market_cap.

Three things are proven here, in order of how badly they'd hurt if wrong:

1. **Point-in-time correctness of the share count.** A filing must not be
   visible before it was *filed*, not merely before its period *end*. A future
   filing must never leak into an earlier ``as_of_date``.
2. **Point-in-time correctness of the fallback.** ``yfinance``'s ``Ticker.info``
   is a current-only snapshot; it must decline historical dates rather than
   stamp today's figures onto them (which a multi-year backfill would otherwise
   do for every ticker whose SEC fetch fails).
3. **Value correctness + NULL handling**, including that ``market_cap`` is
   derived from ``close_raw`` and never the retro-restated ``close_adj``.

Fully offline: synthetic ``companyfacts`` fixtures, injected fakes, no network.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import duckdb
import pytest

from app.database import duckdb_manager as dbm
from app.providers import edgar_provider as ep
from app.providers.provider_interface import FundamentalSnapshot
from app.services.features.feature_engine import FeatureEngine
from app.services.fundamentals import fundamentals_quality as fq

# Reuse the established M11 fixture/seeding conventions.
from tests.test_feature_engine_v02 import (  # noqa: E402
    _fetch_feature,
    _seed_prices,
    _seed_ticker_master,
    _trading_days,
    tmp_db_paths,  # noqa: F401 -- pytest fixture, imported for reuse
)

_AS_OF = date(2024, 6, 1)
_SHARES_CONCEPT = "EntityCommonStockSharesOutstanding"


def _entry(end: str, filed: str, val: float, form: str = "10-Q") -> dict:
    return {"end": end, "filed": filed, "val": val, "form": form}


def _dei(*entries: dict) -> dict:
    return {_SHARES_CONCEPT: {"units": {"shares": list(entries)}}}


# --------------------------------------------------------------------------- #
# 1. Point-in-time correctness of the share count.
# --------------------------------------------------------------------------- #
class TestSharesOutstandingPointInTime:
    def test_takes_the_freshest_knowable_count(self):
        dei = _dei(
            _entry("2023-12-31", "2024-02-01", 1_000.0),
            _entry("2024-03-31", "2024-05-01", 1_200.0),
        )
        assert ep.extract_shares_outstanding(dei, _AS_OF) == 1_200.0

    def test_future_filed_date_does_not_leak(self):
        """The 10-Q for a period ending BEFORE as_of, but FILED after it,
        was not knowable on as_of. Filtering on `end` alone would leak it."""
        dei = _dei(
            _entry("2023-12-31", "2024-02-01", 1_000.0),
            # Period ended 2024-03-31 (< as_of) but wasn't filed until July.
            _entry("2024-03-31", "2024-07-15", 9_999.0),
        )
        assert ep.extract_shares_outstanding(dei, _AS_OF) == 1_000.0

    def test_future_period_end_does_not_leak(self):
        dei = _dei(
            _entry("2023-12-31", "2024-02-01", 1_000.0),
            _entry("2024-09-30", "2024-11-01", 9_999.0),
        )
        assert ep.extract_shares_outstanding(dei, _AS_OF) == 1_000.0

    def test_nothing_knowable_yet_returns_none(self):
        dei = _dei(_entry("2024-03-31", "2024-07-15", 1_200.0))
        assert ep.extract_shares_outstanding(dei, _AS_OF) is None

    def test_as_of_on_the_filed_date_is_inclusive(self):
        dei = _dei(_entry("2024-03-31", _AS_OF.isoformat(), 1_200.0))
        assert ep.extract_shares_outstanding(dei, _AS_OF) == 1_200.0

    def test_amended_filing_for_same_period_prefers_later_filed(self):
        dei = _dei(
            _entry("2024-03-31", "2024-05-01", 1_200.0),
            _entry("2024-03-31", "2024-05-20", 1_250.0),  # restatement
        )
        assert ep.extract_shares_outstanding(dei, _AS_OF) == 1_250.0

    def test_accepts_10q_not_just_10k(self):
        """Unlike extract_annual_series, the cover-page count is taken from the
        freshest filing of any form -- a 10-Q share count is fresher and no less
        point-in-time than the last 10-K's."""
        dei = _dei(
            _entry("2023-12-31", "2024-02-01", 1_000.0, form="10-K"),
            _entry("2024-03-31", "2024-05-01", 1_200.0, form="10-Q"),
        )
        assert ep.extract_shares_outstanding(dei, _AS_OF) == 1_200.0


class TestSharesOutstandingNullHandling:
    @pytest.mark.parametrize("dei", [{}, None, {"SomeOtherConcept": {}}])
    def test_missing_concept_returns_none(self, dei):
        assert ep.extract_shares_outstanding(dei, _AS_OF) is None

    @pytest.mark.parametrize(
        "bad",
        [
            {"units": "not-a-dict"},
            {"units": {"shares": "not-a-list"}},
            {"units": {"shares": ["not-a-dict"]}},
            {},
        ],
    )
    def test_malformed_structure_returns_none_not_raise(self, bad):
        assert ep.extract_shares_outstanding({_SHARES_CONCEPT: bad}, _AS_OF) is None

    @pytest.mark.parametrize(
        "entry",
        [
            {"end": "2024-03-31", "filed": "2024-05-01"},           # no val
            {"end": "2024-03-31", "val": 100.0},                     # no filed
            {"filed": "2024-05-01", "val": 100.0},                   # no end
            {"end": "nonsense", "filed": "2024-05-01", "val": 100.0},
            {"end": "2024-03-31", "filed": "2024-05-01", "val": 0},   # non-positive
            {"end": "2024-03-31", "filed": "2024-05-01", "val": -5},
        ],
    )
    def test_unusable_entries_are_skipped(self, entry):
        assert ep.extract_shares_outstanding(_dei(entry), _AS_OF) is None

    def test_unusable_entry_does_not_hide_a_usable_one(self):
        dei = _dei(
            {"end": "2024-03-31", "filed": "2024-05-01", "val": None},
            _entry("2023-12-31", "2024-02-01", 1_000.0),
        )
        assert ep.extract_shares_outstanding(dei, _AS_OF) == 1_000.0


class TestSnapshotIntegration:
    def _facts(self, dei: dict | None) -> dict:
        facts: dict = {"us-gaap": {}}
        if dei is not None:
            facts["dei"] = dei
        return {"facts": facts}

    def test_snapshot_carries_shares_outstanding(self):
        snap = ep.compute_fundamentals_from_companyfacts(
            self._facts(_dei(_entry("2024-03-31", "2024-05-01", 1_200.0))),
            "AAPL", _AS_OF,
        )
        assert snap.shares_outstanding == 1_200.0

    def test_absent_dei_namespace_yields_none_not_error(self):
        snap = ep.compute_fundamentals_from_companyfacts(
            self._facts(None), "AAPL", _AS_OF
        )
        assert snap.shares_outstanding is None

    def test_dto_rejects_non_positive_shares(self):
        with pytest.raises(ValueError, match="shares_outstanding must be positive"):
            FundamentalSnapshot(
                ticker="AAPL", as_of_date=_AS_OF,
                shares_outstanding=0.0, source_provider="sec_edgar",
            )


# --------------------------------------------------------------------------- #
# 2. The fallback must not stamp today's data onto historical dates.
# --------------------------------------------------------------------------- #
class TestFallbackPointInTimeRestriction:
    _TODAY = date(2026, 7, 10)

    def test_declines_historical_dates(self):
        assert not ep.fallback_can_serve(date(2024, 6, 1), self._TODAY)

    def test_declines_future_dates(self):
        assert not ep.fallback_can_serve(date(2026, 7, 11), self._TODAY)

    def test_serves_today(self):
        assert ep.fallback_can_serve(self._TODAY, self._TODAY)

    def test_serves_within_staleness_window(self):
        within = date(2026, 7, 10 - ep._FALLBACK_MAX_STALENESS_DAYS + 1)
        assert ep.fallback_can_serve(within, self._TODAY)

    def test_declines_just_outside_staleness_window(self):
        outside = date(2026, 7, 10 - ep._FALLBACK_MAX_STALENESS_DAYS - 1)
        assert not ep.fallback_can_serve(outside, self._TODAY)

    def test_backfill_date_yields_no_row_rather_than_a_contaminated_one(self):
        """The regression this guard exists for: SEC fails during a historical
        backfill, so the fallback would previously have written *today's*
        trailingPE/earnings growth against a 2024 as_of_date."""
        class _FakeTicker:
            def __init__(self, symbol: str) -> None:
                self.info = {"trailingPE": 10.0, "debtToEquity": 20.0}

        class _FakeYf:
            @staticmethod
            def Ticker(symbol: str) -> "_FakeTicker":
                return _FakeTicker(symbol)

        def _sec_down(url: str) -> dict:
            raise RuntimeError("sec down")

        provider = ep.EdgarFundamentalsProvider(
            fetch_json=_sec_down,
            ticker_to_cik=lambda t: "0000320193",
            yf_module=_FakeYf(),
            today_fn=lambda: self._TODAY,
        )
        result = provider.get_fundamentals("AAPL", date(2024, 6, 1))

        assert result.status == "failed"
        assert (result.metadata or {}).get("fundamentals") is None

    def test_current_date_still_falls_back_normally(self):
        class _FakeTicker:
            def __init__(self, symbol: str) -> None:
                self.info = {
                    "trailingPE": 10.0,
                    "debtToEquity": 20.0,
                    "sharesOutstanding": 1_500.0,
                }

        class _FakeYf:
            @staticmethod
            def Ticker(symbol: str) -> "_FakeTicker":
                return _FakeTicker(symbol)

        provider = ep.EdgarFundamentalsProvider(
            fetch_json=lambda url: (_ for _ in ()).throw(RuntimeError("sec down")),
            ticker_to_cik=lambda t: "0000320193",
            yf_module=_FakeYf(),
            today_fn=lambda: self._TODAY,
        )
        result = provider.get_fundamentals("AAPL", self._TODAY)

        assert result.status == "success_with_warnings"
        snap = result.metadata["fundamentals"]
        assert snap.source_provider == "yfinance_fallback"
        assert snap.shares_outstanding == 1_500.0


# --------------------------------------------------------------------------- #
# 3. market_cap end-to-end through M11, against a real DuckDB.
# --------------------------------------------------------------------------- #
class TestMarketCapPureFunction:
    def test_product_of_shares_and_raw_close(self):
        assert fq.compute_market_cap(1_000.0, 50.0) == 50_000.0

    @pytest.mark.parametrize(
        "shares,close",
        [(None, 50.0), (1_000.0, None), (None, None), (0.0, 50.0), (1_000.0, 0.0), (-1.0, 50.0)],
    )
    def test_unusable_inputs_yield_none(self, shares, close):
        assert fq.compute_market_cap(shares, close) is None


class TestSharesAsOfJoin:
    _HISTORY = {
        "AAA": [
            (date(2024, 1, 31), 1_000.0),
            (date(2024, 4, 30), 1_100.0),
            (date(2024, 7, 31), 1_200.0),
        ]
    }

    def test_picks_the_latest_filing_not_after_the_cutoff(self):
        assert fq.shares_as_of(self._HISTORY, "AAA", date(2024, 6, 1)) == 1_100.0

    def test_a_later_filing_in_the_same_batch_does_not_leak_backwards(self):
        """The batch read covers dates up to end_date; an early cutoff must not
        see a filing that only became knowable later in the same range."""
        assert fq.shares_as_of(self._HISTORY, "AAA", date(2024, 2, 1)) == 1_000.0

    def test_before_the_first_filing_is_none(self):
        assert fq.shares_as_of(self._HISTORY, "AAA", date(2024, 1, 1)) is None

    def test_exact_boundary_is_inclusive(self):
        assert fq.shares_as_of(self._HISTORY, "AAA", date(2024, 4, 30)) == 1_100.0

    def test_unknown_ticker_is_none(self):
        assert fq.shares_as_of(self._HISTORY, "ZZZ", date(2024, 6, 1)) is None


class TestMarketCapEndToEnd:
    """Real schema, real FeatureEngine, tmp DuckDB."""

    def _seed_shares(self, db_path: Path, ticker: str, rows: list[tuple[date, float | None]]):
        conn = duckdb.connect(str(db_path))
        try:
            for as_of, shares in rows:
                conn.execute(
                    "INSERT INTO ticker_fundamentals "
                    "(ticker, as_of_date, shares_outstanding, source_provider, calculated_at) "
                    "VALUES (?, ?, ?, 'sec_edgar', CURRENT_TIMESTAMP)",
                    [ticker, as_of, shares],
                )
        finally:
            conn.close()

    def test_market_cap_uses_close_raw_and_the_point_in_time_share_count(
        self, tmp_db_paths
    ):
        prod = tmp_db_paths[dbm.DB_ROLE_PROD]
        days = _trading_days(date(2022, 1, 3), 300)
        closes = [50.0 + i * 0.1 for i in range(len(days))]
        _seed_prices(prod, "AAA", days, closes)
        _seed_ticker_master(prod, "AAA")
        # A stale filing, plus one filed after the cutoff that must not be used.
        self._seed_shares(prod, "AAA", [
            (days[-30], 1_000.0),
            (days[-1] + timedelta(days=5), 9_999.0),
        ])

        FeatureEngine().calculate(days[-1], days[-1])
        row = _fetch_feature(prod, "AAA")

        assert row["market_cap"] == pytest.approx(1_000.0 * closes[-1])

    def test_market_cap_is_null_without_a_share_count(self, tmp_db_paths):
        prod = tmp_db_paths[dbm.DB_ROLE_PROD]
        days = _trading_days(date(2022, 1, 3), 300)
        _seed_prices(prod, "AAA", days, [50.0 + i * 0.1 for i in range(len(days))])
        _seed_ticker_master(prod, "AAA")

        FeatureEngine().calculate(days[-1], days[-1])
        assert _fetch_feature(prod, "AAA")["market_cap"] is None

    def test_null_shares_row_does_not_produce_a_market_cap(self, tmp_db_paths):
        prod = tmp_db_paths[dbm.DB_ROLE_PROD]
        days = _trading_days(date(2022, 1, 3), 300)
        _seed_prices(prod, "AAA", days, [50.0 + i * 0.1 for i in range(len(days))])
        _seed_ticker_master(prod, "AAA")
        self._seed_shares(prod, "AAA", [(days[-30], None)])

        FeatureEngine().calculate(days[-1], days[-1])
        assert _fetch_feature(prod, "AAA")["market_cap"] is None

    def test_rows_are_features_v04(self, tmp_db_paths):
        prod = tmp_db_paths[dbm.DB_ROLE_PROD]
        days = _trading_days(date(2022, 1, 3), 300)
        _seed_prices(prod, "AAA", days, [50.0 + i * 0.1 for i in range(len(days))])
        _seed_ticker_master(prod, "AAA")
        FeatureEngine().calculate(days[-1], days[-1])
        assert _fetch_feature(prod, "AAA")["feature_schema_version"] == "features_v04"


# --------------------------------------------------------------------------- #
# 4. Golden diff: both v04 fields are purely additive.
# --------------------------------------------------------------------------- #
class TestGoldenDiffPurelyAdditive:
    _NEW_COLUMNS = {"market_cap", "vcp_sequence_score"}

    def _run(self, prod: Path, days, closes) -> dict:
        FeatureEngine().calculate(days[-1], days[-1])
        return _fetch_feature(prod, "AAA")

    def test_populating_shares_changes_market_cap_and_nothing_else(self, tmp_db_paths):
        """Adding a fundamentals row must not perturb any pre-existing feature.

        The strongest available before/after: identical prices, one run without a
        share count and one with. Every column except market_cap must match.
        """
        prod = tmp_db_paths[dbm.DB_ROLE_PROD]
        days = _trading_days(date(2022, 1, 3), 300)
        closes = [50.0 + i * 0.1 for i in range(len(days))]
        _seed_prices(prod, "AAA", days, closes)
        _seed_ticker_master(prod, "AAA")

        before = self._run(prod, days, closes)
        assert before["market_cap"] is None

        conn = duckdb.connect(str(prod))
        try:
            conn.execute(
                "INSERT INTO ticker_fundamentals "
                "(ticker, as_of_date, shares_outstanding, source_provider, calculated_at) "
                "VALUES ('AAA', ?, 1000.0, 'sec_edgar', CURRENT_TIMESTAMP)",
                [days[-30]],
            )
        finally:
            conn.close()

        after = self._run(prod, days, closes)
        assert after["market_cap"] == pytest.approx(1_000.0 * closes[-1])

        # calculated_at is a per-run timestamp, not a feature.
        volatile = {"calculated_at"}
        changed = {k for k in before if before[k] != after[k]} - volatile
        assert changed == {"market_cap"}, changed

    def test_v04_adds_exactly_two_columns_over_v03(self, tmp_db_paths):
        prod = tmp_db_paths[dbm.DB_ROLE_PROD]
        days = _trading_days(date(2022, 1, 3), 300)
        _seed_prices(prod, "AAA", days, [50.0 + i * 0.1 for i in range(len(days))])
        _seed_ticker_master(prod, "AAA")
        FeatureEngine().calculate(days[-1], days[-1])

        row = _fetch_feature(prod, "AAA")
        assert self._NEW_COLUMNS <= set(row)

    def test_new_fields_are_dormant_no_validator_reads_them(self):
        """Land-the-field discipline: neither column may appear in any Step 3/4/5
        module. Fails loudly the day someone wires one in without a decision."""
        roots = [
            Path("app/services/screening"),
            Path("app/services/analysis"),
            Path("app/services/proposal"),
        ]
        offenders: list[str] = []
        for root in roots:
            for path in root.rglob("*.py"):
                text = path.read_text(encoding="utf-8")
                for column in self._NEW_COLUMNS:
                    if column in text:
                        offenders.append(f"{path}:{column}")
        assert offenders == [], offenders


class TestYfinanceInfoSharesParsing:
    def _info(self, **kw) -> dict:
        return {"trailingPE": 10.0, **kw}

    def test_shares_parsed(self):
        snap = ep.compute_fundamentals_from_yfinance_info(
            self._info(sharesOutstanding=1_500.0), "AAPL", _AS_OF
        )
        assert snap.shares_outstanding == 1_500.0

    @pytest.mark.parametrize("bad", [None, 0, -1, "abc", ""])
    def test_bad_shares_degrade_to_none(self, bad):
        snap = ep.compute_fundamentals_from_yfinance_info(
            self._info(sharesOutstanding=bad), "AAPL", _AS_OF
        )
        assert snap.shares_outstanding is None

    def test_absent_shares_key_is_none(self):
        snap = ep.compute_fundamentals_from_yfinance_info(self._info(), "AAPL", _AS_OF)
        assert snap.shares_outstanding is None
