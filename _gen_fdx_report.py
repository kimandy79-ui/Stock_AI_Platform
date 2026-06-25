"""Generate FDX report for manual inspection."""
import datetime
import sys
sys.path.insert(0, ".")

from app.dashboard.ticker_report import build_ticker_report

row = {
    "ticker": "FDX",
    "company_name": "FedEx Corporation",
    "sector": "Consumer Discretionary",
    "industry": "Consumer Discretionary",
    "setup_type": "pullback",
}
signal_date = datetime.date(2026, 6, 23)

# Guess the setup_config_id from DB
import duckdb
conn = duckdb.connect("data/duckdb/prod.duckdb", read_only=True)
r = conn.execute(
    "SELECT config_id FROM setup_configs WHERE setup_type='pullback' AND active_flag=TRUE LIMIT 1"
).fetchone()
setup_config_id = r[0] if r else "pullback_v1"
print(f"Using setup_config_id: {setup_config_id}")

r_scid = conn.execute(
    "SELECT setup_config_id FROM step5_proposals WHERE ticker='FDX' AND signal_date='2026-06-23' LIMIT 1"
).fetchone()
if r_scid:
    print(f"step5 setup_config_id in DB: {r_scid[0]}")
conn.close()

content, filename = build_ticker_report(row, signal_date, "prod", setup_config_id)
with open(f"_{filename}", "wb") as f:
    f.write(content)
print(f"Written to _{filename}")
print()
print(content.decode("utf-8"))
