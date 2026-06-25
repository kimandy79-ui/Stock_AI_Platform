"""Database subpackage: centralized DuckDB connection management.

Module 02 (DuckDB Manager) lives here in ``duckdb_manager``. Per
``CODING_STANDARDS.md`` section 8, this is the single approved place to open
DuckDB connections; no other module opens arbitrary database paths directly.

Module 03 (Schema Manager) lives here in ``schema_manager``. It creates the
final merged DuckDB schema for the ``prod`` / ``debug`` / ``simulation`` roles,
going through ``duckdb_manager`` for all connections. It does not run
migrations or ``ALTER TABLE`` and does not implement any provider, screening,
scoring, trading, simulation, AI-review, or dashboard logic.

Submodules are imported explicitly by callers
(``from app.database import duckdb_manager`` /
``from app.database import schema_manager``); this package intentionally does
not eagerly import them to keep import side effects minimal.
"""
