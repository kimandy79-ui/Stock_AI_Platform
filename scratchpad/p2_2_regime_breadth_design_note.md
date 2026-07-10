# P2.2 — Regime breadth input: Design Note (2026-07-09)

**UPDATE 2026-07-09 (round 2): IMPLEMENTED per AD-22.25, held uncommitted for
sign-off.** The design below was approved; the architect granted a narrow M12
frozen-module exemption (AD-22.25) scoped strictly to adding the additive,
initially-inert `market_breadth_pct` field. Implementation summary appended at
the end (`## Implementation`). The design analysis below is unchanged from the
design-only version.

## ⚠️ Scope flag (prominent, as requested)

**M12 (market_regime_engine) is NOT in the AD-22.24 frozen-module exemption
scope.** `02b_ARCHITECTURE_DECISIONS.md:197-200` explicitly lists the "regime
engine" among modules that "remain frozen with unchanged public contracts" —
the exemption covers M11/M13/M14/M15/M16/M17/M19/M20/M21/M22 + schema/config,
but *not* M12. So any implementation of breadth **requires its own explicit
frozen-module exemption decision from the architect before a line changes**,
separate from this design note's approval. This is the single biggest gating
item and is why P2.2 is design-only.

## Current regime model (what exists)

`market_regime_engine.py._build_predicates` classifies one market-wide
`market_regime` per calendar date, priority-ordered (first match wins):
`extreme_risk` → `high_risk` → `bear` → `bull` → `neutral` (guaranteed
fallback).
- `extreme_risk`/`high_risk`: VIX gates, `vix_close >= constants.VIX_EXTREME_RISK_THRESHOLD`
  / `VIX_HIGH_RISK_THRESHOLD` (30 / 25 — hardcoded constants; note the
  `risk_label_config.market_regime.{high_risk_vix,extreme_risk_vix}` copies are
  shadow/dead config, flagged separately by the P2.5 read-coverage ledger).
- `bear`: SPY close < SPY ema200 AND QQQ close < QQQ ema200.
- `bull`: SPY close > SPY ema200.
Inputs: SPY / QQQ / ^VIX price history only. Output: a single VARCHAR
`market_regime` written onto every `daily_features` row for that date. It is
pass-through at Step 3/4 (never a hard gate) and a hard gate only at Step 5
(`buy_rules.block_market_regimes` / `block_if_regime_null`).

## (a) Computation approach + where it lands

**Breadth = % of the active universe trading above its own 200-day EMA** on
the signal date. Fully computable from data already materialized: every
`daily_features` row already carries `ema200` and `close_adj` (the same fields
step3 routing reads). No new provider, no new price fetch — a single
same-day aggregate over the day's `daily_features`:

```
breadth_pct = 100 * count(close_adj > ema200) / count(ema200 IS NOT NULL)
```

over the active, feature-ready universe for that `feature_date`.

**Where it's computed — this is the real design fork, because of the M12
freeze and an ordering constraint:**
- M12 runs *before* the per-ticker feature rows are all available in the
  current pipeline order? No — check ordering: M11 (features, writes ema200)
  runs before M12 (regime). So by the time M12 runs, `daily_features.ema200`
  for the universe *is* available to read. So M12 *could* compute breadth with
  one extra aggregate read. **But** M12 currently reads only SPY/QQQ/^VIX rows,
  not the whole universe — adding a universe-wide breadth read broadens its
  input contract (another reason it needs the freeze exemption).
- Alternative that avoids touching M12 at all: compute breadth as a **feature
  in M11** (which *is* exemption-scoped) — but breadth is a market-wide scalar,
  not a per-ticker feature, so storing it on every `daily_features` row is
  redundant/awkward, and M11 computes per-ticker rows in a single pass that
  doesn't naturally produce a cross-sectional aggregate mid-stream (same
  cross-sectional-pass issue as the rs_percentile_126d P1.1 work — it needed a
  post-pass). Feasible but a design smell.
- Recommendation: breadth belongs conceptually in M12 (it's a market-regime
  input, not a ticker feature). Accept that this needs the M12 exemption. Store
  it either as a second column on the regime write, or in a small
  market-regime side table — see (b).

## (b) Single combined label vs. second independent field

Two options, architect's call:

1. **Fold breadth into the existing single `market_regime` label.** E.g., a
   strong-VIX day stays `extreme_risk`; on non-VIX days, low breadth
   (< threshold) downgrades `bull` → `neutral` or `neutral` → `bear`. Pro: no
   schema change, all existing Step-5 gates keep working unchanged. Con:
   collapses two orthogonal signals (volatility regime vs. participation
   breadth) into one enum, losing information and making the label's meaning
   overloaded/harder to diagnose.
2. **Add a second independent field** (`market_breadth_pct` DOUBLE and/or a
   `breadth_regime` enum) alongside `market_regime`. Pro: orthogonal signals
   stay separable, diagnosable, and independently tunable; matches how VIX and
   trend are *already* somewhat separable. Con: schema addition (new column on
   the regime write path / daily_features, or a new side table), and Step 5
   would need to learn to read it (a real M15 change) if breadth is ever to
   gate.

Recommendation: **option 2 (second field), stored but initially inert** — same
discipline as fundamentals (weight 0.0) and rs_percentile_126d (scoring-only,
no gate): land the *measurement* first, decide its *use* later once there's
data. This keeps the first change purely additive (compute + store, no
behavior change to disposition) which is the smallest, safest first step and
sidesteps overloading the existing enum.

## (c) Penalty-only vs. regime-conditional routing — flag as SEPARATE decision

- **Penalty-only (current pattern):** breadth influences Step-5 scoring/gating
  the same way `market_regime` does today (a `buy_rules`-style block or a
  soft score penalty). Small, in-pattern, reversible.
- **Regime-conditional routing** (e.g., suppress `breakout` routing in
  low-breadth/bear regimes at Step 3): **a materially bigger architectural
  change.** Step 3 routing is currently regime-*independent* by design
  (`market_regime` is explicitly pass-through, never a gate, at Step 3/4 —
  AD note + the P2.5 matrix). Making routing regime-conditional would:
  couple Step 3 to the regime signal (new input dependency), change which
  setups even reach Step 4 (not just their disposition), and break the current
  clean invariant that routing depends only on per-ticker structure. **This
  should be its own scoped decision, NOT bundled with adding the breadth
  measurement.** Recommend: explicitly defer; land measurement (a)+(b) first,
  penalty-only use second if wanted, routing-conditional only as a distinct
  later architecture item.

## Recommended sequencing

1. **Architect decision #1:** grant (or decline) an M12 frozen-module
   exemption for this work. Nothing proceeds without it.
2. If granted: implement breadth as an additive second field, computed in M12
   from the already-available universe `ema200`/`close_adj`, stored, **inert**
   (no disposition change). Prove zero behavior change to `market_regime` and
   to Step-5 output.
3. **Architect decision #2 (later, separate):** whether/how breadth gates —
   penalty-only first; regime-conditional routing only as its own item.

## Not designed here (out of scope, flagged)
- The exact breadth threshold(s) — that's threshold tuning, gated on diagnostics
  per CLAUDE.md's no-pre-diagnostic-tuning principle (see
  [[p1-adaptive-threshold-deferred]]).
- Cleaning up the shadow `risk_label_config.market_regime.*_vix` dead keys
  (surfaced by P2.5) — related but a separate cleanup decision.

---

## Implementation (2026-07-09, under AD-22.25 — held uncommitted for sign-off)

**AD-22.25 written first** (`02b_ARCHITECTURE_DECISIONS.md`) documenting the
narrow M12 carve-out before any code, per instruction.

**What changed (additive + inert only):**
- `schema_manager.py`: `market_breadth_pct DOUBLE` column added to
  `daily_features` (nullable, after `market_regime`). Existing prod/debug DBs
  need reinit to get it (reinit-not-migrate, same as every schema bump here).
- `market_regime_engine.py`: `_write` now, per classified date, computes
  breadth from the already-written feature rows and sets it in the UPDATE.
  Breadth = `100 * count(distance_to_ema200_pct > 0) / count(distance_to_ema200_pct)`
  over `feature_ready = TRUE` rows with non-null `distance_to_ema200_pct`
  (that field = `close_adj/ema200 - 1`, so `> 0` == above EMA200 — single
  table, no join, reuses M11 output). NULL when no qualifying rows.
  `_build_predicates`, the priority order, and the `market_regime` enum are
  **untouched**.
- Module docstring updated to note the carve-out.

**Chosen design points** (matching the approved note): additive **second
field** (not folded into the enum), **inert** (no disposition/routing path
reads it — verified: nothing outside this write references `market_breadth_pct`).
Population = feature-ready universe. (Minor known caveat: benchmark rows
SPY/QQQ/VIX, if they have feature rows that day, are included in the
denominator — negligible and documentable; can be excluded via a symbol_type
join later if wanted. Does not affect the zero-behavior-change proof.)

**Zero-behavior-change PROVEN** (AD-22.25 proof obligation, same standard as
P2.1): a before/after golden harness classified 3 fixed scenarios
(bull / high_risk / neutral) on fresh tmp DBs, dumping `market_regime` per
(ticker, date). Ran against pre-change HEAD (via `git stash` of
`schema_manager.py` + `market_regime_engine.py`) vs post-change working tree →
**empty diff, identical SHA256 (6fd9b41e…)**. `market_regime` is byte-identical.

**Tests:** `tests/test_market_breadth_field.py` (3 tests — correct breadth
value, feature-ready/non-null population filtering, NULL-when-no-qualifying-rows,
each also asserting `market_regime` unchanged on its fixture). Full existing
`test_market_regime_engine.py` suite passes unchanged (the exhaustive
behavioral spec for `market_regime`), plus schema/M11/step5/orchestrator/phase7
regression green.

**Deliberately NOT done (out of the AD-22.25 carve-out):** no penalty-gating,
no regime-conditional routing, no change to any consumption of the regime
signal, no enum change. Those remain separate future decisions.

**Recommended follow-up (not done, flag for architect):** an M12 spec addendum
(`M12_MARKET_REGIME_ENGINE_SPEC.md`) documenting the new field — deferred
because AD-22.25 is the authoritative record and touching the frozen M12 spec
is the architect's call.

**Status: held uncommitted for sign-off**, same pattern as the rest of this
batch. Files: `schema_manager.py`, `market_regime_engine.py`,
`specs/02b_ARCHITECTURE_DECISIONS.md` (AD-22.25),
`tests/test_market_breadth_field.py`.
