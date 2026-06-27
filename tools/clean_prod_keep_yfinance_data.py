import duckdb
import shutil
from pathlib import Path
from datetime import datetime

db_path = Path("data/duckdb/prod.duckdb")

if not db_path.exists():
    raise FileNotFoundError(f"DB not found: {db_path}")

ts = datetime.now().strftime("%Y%m%d_%H%M%S")
backup_path = db_path.with_name(f"prod_before_cleanup_{ts}.duckdb")
report_path = Path(f"db_cleanup_report_{ts}.csv")

# Always backup first
shutil.copy2(db_path, backup_path)
print(f"Backup created: {backup_path}")

# Tables to preserve because they contain downloaded/provider or reference data
keep_tables = {
    "ticker_master",
    "ticker_universe_snapshot",
    "sector_etf_map",
    "daily_prices",
}

# Known calculated / pipeline / derived tables to clean if present
delete_tables = [
    # Feature / regime / calculated market context
    "daily_features",
    "market_regime",
    "market_regimes",
    "market_regime_history",

    # Data quality / mutation calculated outputs
    "data_validation_results",
    "daily_price_validation",
    "price_validation_results",
    "price_mutation_events",
    "mutation_events",
    "mutation_detection_results",

    # Setup-mode pipeline outputs
    "step3_candidates",
    "step4_analysis",
    "step5_proposals",

    # Older strategy-mode or proposal outputs, if still present
    "screening_results",
    "strategy_candidates",
    "strategy_analysis",
    "proposals",
    "daily_proposals",
    "historical_results",
    "outcome_tracking",
    "trade_outcomes",

    # Run/debug/report logs that can point to stale calculated rows
    "pipeline_runs",
    "pipeline_run_steps",
    "pipeline_health",
    "debug_runs",
    "debug_results",
    "ai_review_exports",
    "report_exports",
]

con = duckdb.connect(str(db_path))

try:
    existing_tables = {
        r[0]
        for r in con.execute("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'main'
              AND table_type = 'BASE TABLE'
            ORDER BY table_name
        """).fetchall()
    }

    report_rows = []

    def count_rows(table: str) -> int:
        return con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]

    print("\nPreserved tables:")
    for t in sorted(keep_tables & existing_tables):
        cnt = count_rows(t)
        print(f"  KEEP   {t:<35} rows={cnt}")
        report_rows.append((t, "KEEP", cnt, cnt, 0))

    print("\nDeleting calculated/pipeline tables:")
    con.execute("BEGIN TRANSACTION")

    for t in delete_tables:
        if t not in existing_tables:
            continue

        before = count_rows(t)
        con.execute(f'DELETE FROM "{t}"')
        after = count_rows(t)
        deleted = before - after

        print(f"  DELETE {t:<35} rows_before={before} rows_after={after} deleted={deleted}")
        report_rows.append((t, "DELETE", before, after, deleted))

    con.execute("COMMIT")

    # Write cleanup report CSV
    with report_path.open("w", encoding="utf-8") as f:
        f.write("table,action,rows_before,rows_after,rows_deleted\n")
        for table, action, before, after, deleted in report_rows:
            f.write(f"{table},{action},{before},{after},{deleted}\n")

    print()
    print("Cleanup completed.")
    print(f"Report written: {report_path}")

finally:
    con.close()
