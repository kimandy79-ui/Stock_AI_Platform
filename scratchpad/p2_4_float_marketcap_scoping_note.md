# P2.4 — Float / market-cap universe metadata: Scoping Note (2026-07-09)

**Scoping only, kept brief per the P2 batch note ("lower priority than
P2.2/P2.3 ... don't over-invest until Andrey decides it's worth scheduling").**
No code, no design commitment.

## Gap
Universe gating (Step 3) is price + `avg_dollar_volume_20d` only. O'Neil-style
supply/demand — low **float** (fewer shares → sharper moves on demand) — is
unaddressed. Market cap is a related, easier-to-source proxy.

## Data source — one useful finding
The EDGAR provider **already retrieves a shares-outstanding concept**:
`edgar_provider.py:442` pulls `WeightedAverageNumberOfDilutedSharesOutstanding`
(currently only to derive EPS). So *diluted weighted-average shares* is already
in reach from the existing EDGAR path — market cap ≈ shares × price is
derivable with no new provider. Caveats:
- Weighted-average diluted shares ≠ **free float** (float excludes insider/
  restricted/closely-held shares). True float needs a different source —
  EDGAR cover-page `dei:EntityCommonStockSharesOutstanding` (point-in-time,
  closer to shares outstanding but still not free float), or a commercial/
  Yahoo field. **Genuine free-float is the hard part; shares-outstanding /
  market-cap is the cheap 80%.**
- Stooq (the other Phase-4 provider) is price/volume only — not a fundamentals
  source for shares.

**Recommendation:** scope V1 to **market cap / shares outstanding** (sourceable
from existing EDGAR concepts, low effort) and treat **true free float** as a
separate, higher-effort follow-up needing a data source the project doesn't
have yet. Don't conflate the two in scheduling.

## Storage — follow the Phase 4 pattern
Add to the existing **`ticker_fundamentals` companion table** (Phase 4:
`ticker VARCHAR, as_of_date DATE, eps_growth_trend, leverage_ratio,
valuation_band, piotroski_f_score, altman_z_score, insider_trade_flag,
institutional_ownership_delta, source_provider, calculated_at`), not
`ticker_master`. Rationale: shares/float/market-cap update irregularly
(filing-driven), exactly like the existing fundamentals fields — the companion
table already carries `as_of_date` history for this. A `shares_outstanding`
(and optional `market_cap`, `free_float`) column there is the natural, in-
pattern home. `ticker_master` is identity/status (sector, active_flag) — wrong
place for a time-varying quantity.

## Rough effort estimate (order-of-magnitude)
- **Market-cap / shares-outstanding path (recommended V1):** small–medium.
  Add column(s) to `ticker_fundamentals` schema; extend the EDGAR fundamentals
  fetch to persist the shares concept it already reads; expose it to Step 3 as
  an optional universe gate (mirrors the existing `min_avg_dollar_volume_20d`
  gate structure — a `min_market_cap` / `max_shares_for_low_float` config key
  in the `universe` block, defaulting to off/None so it's byte-identical until
  seeded). ~1 focused change + tests.
- **True free-float path:** medium–large and **blocked on data sourcing** —
  no current provider supplies free float; would need a new data source
  decision first. Recommend deferring until/unless V1 market-cap gating proves
  its worth.

## Not in scope here
- The gate thresholds (min market cap, float ceiling) — diagnostics-gated
  tuning per CLAUDE.md, not decided at scoping time.
- Whether it's a hard universe gate vs. a Step-4/5 scoring input — a design
  decision for if/when this is scheduled.

## Bottom line for Andrey
Cheap version (market cap via existing EDGAR shares concept, stored in
`ticker_fundamentals`, optional Step-3 universe gate) is genuinely low-effort
and reuses existing plumbing. True free-float is a separate, data-blocked item.
Recommend scheduling only the cheap version if/when supply/demand filtering
moves up the priority list; keep free-float parked.
