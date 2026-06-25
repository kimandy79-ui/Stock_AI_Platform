"""Temporary diagnostic script for FDX 2026-06-23."""
import json
import duckdb

conn = duckdb.connect("data/duckdb/prod.duckdb", read_only=True)

print("=== step4_analysis FDX 2026-06-23 ===")
r = conn.execute(
    "SELECT setup_type, setup_passed, setup_score, market_regime, earnings_days, "
    "entry_price_raw, support_level, resistance_level, next_resistance_level, "
    "atr_pct, rvol, setup_fail_reason FROM step4_analysis "
    "WHERE ticker='FDX' AND signal_date='2026-06-23' ORDER BY created_at DESC LIMIT 5"
).fetchall()
for row in r:
    print(row)

print("\n=== step4_analysis FDX explanation_json ===")
r2 = conn.execute(
    "SELECT setup_type, explanation_json FROM step4_analysis "
    "WHERE ticker='FDX' AND signal_date='2026-06-23' ORDER BY created_at DESC LIMIT 3"
).fetchall()
for st, expl in r2:
    print(f"setup_type={st}")
    if expl:
        parsed = json.loads(expl) if isinstance(expl, str) else expl
        for k, v in (parsed or {}).items():
            print(f"  {k}: {v}")

print("\n=== step5_proposals FDX 2026-06-23 ===")
r3 = conn.execute(
    "SELECT setup_type, disposition, stop_price_raw, target_price_raw, "
    "estimated_rr, rejection_reason, mechanical_explanation "
    "FROM step5_proposals WHERE ticker='FDX' AND signal_date='2026-06-23'"
).fetchall()
for row in r3:
    print(row)
if not r3:
    print("(no rows)")

print("\n=== daily_features FDX 2026-06-23 ===")
r4 = conn.execute(
    "SELECT feature_date, feature_schema_version, market_regime, "
    "ema20, ema50, ema200, support_level, resistance_level, next_resistance_level, "
    "swing_high, swing_low, atr14, atr_pct, rvol20, pullback_depth_pct, "
    "days_to_earnings_bd, relative_strength_vs_spy, sector_relative_strength "
    "FROM daily_features WHERE ticker='FDX' AND feature_date='2026-06-23' "
    "ORDER BY feature_schema_version DESC LIMIT 3"
).fetchall()
cols = [d[0] for d in conn.execute(
    "SELECT feature_date, feature_schema_version, market_regime, "
    "ema20, ema50, ema200, support_level, resistance_level, next_resistance_level, "
    "swing_high, swing_low, atr14, atr_pct, rvol20, pullback_depth_pct, "
    "days_to_earnings_bd, relative_strength_vs_spy, sector_relative_strength "
    "FROM daily_features LIMIT 0"
).description]
for row in r4:
    for c, v in zip(cols, row):
        print(f"  {c}: {v}")

print("\n=== market_regime in daily_features for 2026-06-23 (any ticker) ===")
r5 = conn.execute(
    "SELECT ticker, feature_date, market_regime FROM daily_features "
    "WHERE feature_date='2026-06-23' LIMIT 5"
).fetchall()
for row in r5:
    print(row)

print("\n=== SPY/QQQ/VIX in daily_prices for 2026-06-23 ===")
r6 = conn.execute(
    "SELECT ticker, date, close_raw, close_adj, data_quality_status "
    "FROM daily_prices WHERE ticker IN ('SPY','QQQ','^VIX') AND date='2026-06-23'"
).fetchall()
for row in r6:
    print(row)

print("\n=== earnings_calendar for FDX ===")
r7 = conn.execute(
    "SELECT ticker, earnings_date, session, source, confidence "
    "FROM earnings_calendar WHERE ticker='FDX' ORDER BY earnings_date DESC LIMIT 5"
).fetchall()
for row in r7:
    print(row)
if not r7:
    print("(no rows)")

print("\n=== FDX ticker_master ===")
r8 = conn.execute(
    "SELECT ticker, company_name, sector, industry, symbol_type, active_flag "
    "FROM ticker_master WHERE ticker='FDX'"
).fetchall()
for row in r8:
    print(row)

print("\n=== sector_etf_map ===")
r9 = conn.execute(
    "SELECT sector, etf_ticker FROM sector_etf_map ORDER BY sector"
).fetchall()
for row in r9:
    print(row)

print("\n=== daily_features_current view - FDX ===")
try:
    r10 = conn.execute(
        "SELECT ticker, feature_date, feature_schema_version, market_regime, "
        "swing_high, swing_low, support_level FROM daily_features_current "
        "WHERE ticker='FDX' LIMIT 3"
    ).fetchall()
    for row in r10:
        print(row)
except Exception as e:
    print(f"ERROR: {e}")

conn.close()