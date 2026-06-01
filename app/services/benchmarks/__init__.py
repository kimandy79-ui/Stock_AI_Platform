"""Benchmark / sector-ETF service package (Module 07).

Exposes :class:`~app.services.benchmarks.benchmark_etf_loader.BenchmarkEtfLoader`,
which loads benchmark, index, and sector-ETF price history into ``daily_prices``,
upserts those symbols into ``ticker_master``, and seeds ``sector_etf_map`` before
the feature engine runs. See ``M07_BENCHMARK_ETF_LOADER_SPEC.md``.
"""

from __future__ import annotations

from app.services.benchmarks.benchmark_etf_loader import BenchmarkEtfLoader

__all__ = ["BenchmarkEtfLoader"]
