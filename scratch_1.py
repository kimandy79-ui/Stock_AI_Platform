python - <<'PY'
from app.database import duckdb_manager as dbm

for role in ["prod", "debug", "simulation"]:
    print("\nROLE:", role)
    con = dbm.connect(role, read_only=True)
    try:
        tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
        print("tables:", len(tables))

        for table in ["strategy_configs", "runtime_configs", "config_activation_log", "sector_alias_map"]:
            print(table, con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

        print("active strategies:", con.execute(
            "SELECT strategy_name, config_id, active_flag FROM strategy_configs ORDER BY strategy_name"
        ).fetchall())

        print("runtime types:", con.execute(
            "SELECT config_type, config_id, active_flag FROM runtime_configs ORDER BY config_type"
        ).fetchall())

        print("sector aliases sample:", con.execute(
            "SELECT source, raw_sector, canonical_sector FROM sector_alias_map ORDER BY raw_sector LIMIT 5"
        ).fetchall())
    finally:
        con.close()
PY