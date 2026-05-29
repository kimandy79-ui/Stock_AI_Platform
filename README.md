Project structure
stock_ai_platform/
  app/
    config/
      settings.py      # pathlib paths + immutable strategy presets
      constants.py     # domain vocabulary + FEATURE_SCHEMA_VERSION
      env.py           # python-dotenv loading + typed getters
    database/
      duckdb_manager.py  # centralized DuckDB connection manager (Module 02)
    utils/
      service_result.py  # shared ServiceResult dataclass contract
      logging_config.py   # run_id-aware logging (timestamp | level | module | run_id | message)
  data/
    duckdb/            # prod/debug/simulation DuckDB files (created on first connect)
    logs/
    exports/
    backups/
  tests/
    test_project_skeleton.py
    test_duckdb_manager.py
  docs/                # source-of-truth specification documents
  pyproject.toml
  requirements.txt
  .env.example
  README.md
