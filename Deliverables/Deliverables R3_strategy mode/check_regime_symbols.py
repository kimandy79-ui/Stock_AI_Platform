import duckdb

conn = duckdb.connect(r'data\duckdb\prod.duckdb', read_only=True)

print("=== SPY / QQQ / ^VIX in daily_prices ===")
rows = conn.execute(
    "SELECT ticker, COUNT(1) as rows, MIN(date) as first, MAX(date) as last "
    "FROM daily_prices "
    "WHERE ticker IN ('SPY', 'QQQ', '^VIX') "
    "GROUP BY ticker "
    "ORDER BY ticker"
).fetchall()
if rows:
    for r in rows:
        print(f"  {r[0]}: {r[1]} rows  {r[2]} → {r[3]}")
else:
    print("  NONE FOUND — SPY/QQQ/^VIX not in daily_prices")

print()
print("=== pipeline_runs — market_regime step ===")
runs = conn.execute(
    "SELECT run_date, status, steps_completed "
    "FROM pipeline_runs "
    "ORDER BY run_date DESC "
    "LIMIT 5"
).fetchall()
for r in runs:
    print(f"  {r[0]}  {r[1]}  steps={r[2]}")

conn.close()
