# Part B, Step 1 — RS Filter / 150MA Investigation & Reconciliation

**Date:** 2026-07-20
**Status:** Investigation complete. **Step 2 (implementation) NOT started** —
stopping here per the coder note's instruction, pending review.

---

## The discrepancy, resolved

The coder note flagged a conflict between "Phase 1.5 declared these
approximated/blocked on no field existing" and "project records say
`features_v02` already added `relative_strength_vs_spy`." Both records are
correct, and the apparent conflict dissolves once the two criteria are
looked at separately — they're in materially different states:

| Criterion | Field exists? | Currently wired? | 150MA needed? |
|---|---|---|---|
| Breakout "RS filter active" | Yes (`relative_strength_vs_spy`, v02) | **No** — `validate_breakout` never reads it | No |
| Trend_continuation "RS vs SPY >0 required (hard, not soft)" | Yes (`relative_strength_vs_spy`, v02) | **Partially** — read as a *soft scoring* input only, no hard gate | — |
| Trend_continuation "price>50MA>150MA>200MA" | **No** — no 150-day MA field anywhere | N/A | Yes, from scratch |

Phase 1.5's design note (`scratchpad/phase1_5_preset_config_design_note.md`,
also mirrored verbatim in `app/services/config/default_configs.py:225-231`)
already documented this exact split precisely — I'm confirming it against the
live code, not discovering it fresh:

> 1. **Breakout "RS filter active"**: `validate_breakout` never reads any
>    relative-strength feature — only `validate_trend_continuation` does. ...
>    Not implemented; would require a Step 4 code change.
> 2. **Trend continuation "RS vs SPY >0 required not soft" and
>    "price>50MA>150MA>200MA"**: `relative_strength_vs_spy`/
>    `sector_relative_strength` are scoring-only inputs in
>    `validate_trend_continuation` (no hard RS gate exists in code), and there
>    is no 150-day EMA/SMA feature anywhere in the `daily_features` schema
>    (only ema20/ema50/ema200). Approximated by raising the
>    `relative_strength` scoring weight (0.20 → 0.30) and requiring a higher
>    `min_ema_alignment` (80 vs v1's 50) — a soft emphasis, not a true hard
>    requirement.

CLAUDE.md's "Dormant feature fields" list (`rs_percentile_126d,
market_breadth_pct, market_cap, vcp_sequence_score`) does **not** include
`relative_strength_vs_spy` — correctly, since it's not dormant: it's actively
read (just only by one of the two validators, and only as a soft score).

## 1. Does `relative_strength_vs_spy` exist and get computed today?

**Yes**, confirmed at every layer:

- **Schema** (`app/database/schema_manager.py:197`): `relative_strength_vs_spy DOUBLE`
  column exists in `daily_features`, has existed since the v02 migration.
- **Feature engine** (`app/services/features/feature_engine.py`): in
  `OPTIONAL_FEATURE_COLUMNS` and `_FEATURE_PARAM_COLUMNS`; computed at
  `feature_engine.py:1268-1273`:
  ```python
  rs_vs_spy = ticker_roc - spy_roc   # both ticker's and SPY's roc20
  ```
  where `roc20 = close_adj / close_adj.shift(20) - 1.0`
  (`feature_engine.py:1663`) — a **20-trading-day (~1 month) rate-of-change
  spread against SPY**, on adjusted close. NULL whenever either side's
  `roc20` is unavailable (insufficient history, or no SPY price row for that
  cutoff date).
- **Consumer**: `app/services/screening/m14_setup_validators.py:1075` reads
  it inside `validate_trend_continuation` **only** — grepped the whole file;
  it does not appear anywhere in `validate_breakout` (line range 380-667),
  `validate_pullback`, or `validate_consolidation_base`.

**Real sample values** (queried `data/duckdb/prod.duckdb`, read-only, 2026-07-13
signal_date, `features_v04`):

```
ticker  roc20      relative_strength_vs_spy   rs_percentile_126d
AAPL    0.07333    0.05525                    75.27
ABBV    0.10335    0.08527                    63.72
AMD     0.09405    0.07597                    98.22
AVGO   -0.00394   -0.02202                    67.77
AMZN    0.02402    0.00593                    44.72
```

Coverage is high, not sparse: `114546 / 114816` `features_v04` rows
(**~99.8%**) have a non-NULL `relative_strength_vs_spy`; `112751/114816`
(~98.2%) have `rs_percentile_126d`. This is a live, well-populated,
production field, not a thinly-tested corner case — "usable" in the sense
the coder note conditions Step 2 on.

**Formula caveat, worth flagging before anyone hard-gates on it:**
`relative_strength_vs_spy`'s window is `roc20` — 20 trading days (~1 month).
Classic literature RS measures it's meant to approximate (O'Neil RS Rating,
Minervini's trend-template RS check) use 6-12 month, often quarter-weighted,
lookbacks. This exact tension is *why* P1.1 (2026-07-08) added a second,
independent field, `rs_percentile_126d` (126-trading-day / ~6-month window),
rather than just percentile-ranking the existing spread — see
`scratchpad/p1_batch_rs_earnings_breakout_scoring_design_note.md:44-51`:

> Both `relative_strength_vs_spy` and `sector_relative_strength` are built on
> **`roc20`** — a ~1-month lookback. O'Neil RS Rating / Minervini trend
> template both use 6-12 month (often quarter-weighted) lookbacks. ... A
> percentile rank of a 20-day spread is a different (and noisier) signal than
> a percentile rank of a 6-12 month spread.

So there are actually **two** existing, populated RS-shaped fields to choose
from for these gates, with a real tradeoff:
- `relative_strength_vs_spy` — vs.-SPY time-series spread, ~1-month window,
  matches the *shape* of the literature criteria ("RS vs SPY > 0") most
  literally, but on a much shorter horizon than the literature intends.
- `rs_percentile_126d` — same-day cross-sectional percentile against the
  active universe, ~6-month window (closer to the literature's horizon), but
  a *different kind* of measure (percentile rank, not a vs.-SPY spread) and
  is explicitly marked dormant in CLAUDE.md ("wiring one in is an explicit
  decision, gated on diagnostics").

## 2. Does a 150-day moving average exist anywhere?

**No — fully missing, not partial.** Grepped `feature_engine.py` for
`ema150`/`sma150`/`150`: only `ema20`, `ema50`, `ema200` exist
(`_MIN_BARS_EMA20/50/200` constants, `REQUIRED_FEATURE_COLUMNS`,
`_FEATURE_PARAM_COLUMNS`). Confirmed against the DDL
(`schema_manager.py`) too — no 150-day column of any kind. Nothing computable
from already-stored columns either (`ema20`/`ema50`/`ema200` don't combine
into a 150-day figure). This would be a from-scratch addition.

## 3. Reconciliation

`relative_strength_vs_spy` is **not** "the same thing, just not wired in yet"
for both criteria uniformly — it splits:

- **Breakout RS filter**: the field exists and is well-populated, but is
  **entirely unread** by `validate_breakout`. Wiring it in is a real but
  small validator change (read the field, add a threshold check) — no new
  data needed.
- **Trend_continuation RS gate**: the field exists, is well-populated, and
  **is already read** — but only as a soft scoring weight
  (`components["relative_strength"]`), never a hard pass/fail gate. Turning
  it into a hard gate is a *behavior* change (can newly reject/downgrade
  setups that currently only lose scoring points), not a wiring gap.
- **150MA**: fully missing on both counts (no field, no gate) — this part is
  exactly as large as originally scoped: a new feature column plus a new
  schema version.

## 4. Additional findings that affect Step 2's scope — flagging before any implementation

**(a) 150MA would need a `features_v05` bump, following the established
v01→v02→v03→v04 pattern exactly.** Every prior structural addition
(`ema20_slope`/... in v02, `rs_percentile_126d` in v03, `market_cap`/
`vcp_sequence_score` in v04) bumped `FEATURE_SCHEMA_VERSION`
(`app/config/constants.py:28`, currently `"features_v04"`) rather than
silently landing in the current version — the stated reason each time is
that existing rows computed without the new field shouldn't retroactively
claim to have it under the same version tag. A 150-day MA is the same kind
of structural addition. Neither `feature_engine.py` (M11) nor
`m14_setup_validators.py` (M14) is in CLAUDE.md's frozen-module list, so no
frozen-module exemption is needed — confirms the note's assumption.

**(b) CLAUDE.md explicitly blocks the breakout half of Step 2 as scoped —
this is the most important finding of this investigation.** CLAUDE.md's
"Current state & next steps" (a document whose instructions are declared to
"OVERRIDE any default behavior") lists, verbatim, under "Pending (in order)":
```
2. Scoring additions (post-diagnostics only):
   - relative_strength_score for BREAKOUT (high priority)
   ...
Do not add scoring components or tune thresholds before diagnostics data is available.
```
Item 1 (funnel diagnostics) is not complete — per memory, only a single
2026-07-10 datapoint has been captured, explicitly flagged "don't tune off
one date." Whether the note's "breakout RS filter" is implemented as a hard
gate or a soft score, it is the same feature CLAUDE.md names and defers.
Implementing it now would directly contradict a standing, explicit,
higher-priority-than-coder-note instruction. **I did not implement any part
of the breakout half of Step 2 for this reason and it should not proceed
without the architect explicitly overriding this CLAUDE.md guidance.**

**(c) The trend_continuation RS-hard-gate is not literally named in that
CLAUDE.md pending list, so it isn't blocked by the same clause outright** —
but converting an existing soft score into a hard pass/fail gate is a bigger
behavioral change than adding a new score, and a config-defined RS threshold
for it is arguably itself the kind of "config threshold" CLAUDE.md separately
says is "deferred until diagnostics provide empirical signal." Not an
automatic blocker like (b), but flagging it as the same category of risk for
the architect to weigh in on explicitly, rather than treating it as cleared
by omission.

**(d) 150MA as a bare field addition (no gate wired yet) would be lower-risk
in isolation** — same shape as landing `rs_percentile_126d`/`market_cap`/
`vcp_sequence_score` dormant and gating the wiring decision separately. But
the note's Step 2 pairs "add 150MA" directly with "wire it into a hard
trend_continuation gate" as one unit of work, so I'm not treating the field
addition as pre-cleared on its own either — reporting it as a option the
architect could choose to split (land the field now, decide the gate later)
rather than assuming that split.

## What I did NOT do

Per the coder note's explicit instruction, no implementation was attempted —
no changes to `feature_engine.py`, `m14_setup_validators.py`, `constants.py`,
or any setup_config. This report is investigation-only.

## Recommendation for Step 2 scoping (for the architect to decide, not decided here)

- **Breakout RS filter: hold.** Directly conflicts with CLAUDE.md's explicit
  diagnostics-gate on this exact feature. Needs either diagnostics to
  actually complete first, or an explicit architect override of that
  guidance — not something to proceed on by default.
- **Trend_continuation RS hard-gate: architect call.** Field exists, is
  populated, and is literally what the criterion asks for if the ~1-month
  window is acceptable — but consider `rs_percentile_126d` (already dormant,
  already 126-day, already closer to the literature's horizon) as the
  alternative if the window mismatch matters more than reusing the exact
  field name the original criterion referenced. Also weigh (c) above.
- **150MA: architect call on scope** — full pairing (field + gate) as
  originally scoped, or field-only now with the gate decision deferred
  (mirroring how `rs_percentile_126d`/`market_cap`/`vcp_sequence_score` were
  each landed dormant first).
