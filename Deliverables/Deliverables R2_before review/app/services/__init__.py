"""Service layer for the Swing Trading Stock Analyzer.

Service subpackages (``downloader``, ``universe``, ``features`` ...) implement
the pipeline steps described in ``ARCHITECTURE.md`` / the merged source of
truth. Each service returns an :class:`app.utils.service_result.ServiceResult`
and performs all database access exclusively through
:mod:`app.database.duckdb_manager`.
"""
