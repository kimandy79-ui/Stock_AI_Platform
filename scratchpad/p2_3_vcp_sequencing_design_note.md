# P2.3 — VCP sequencing check: Design Note (design-only, 2026-07-09)

**No code changes.** Feature-engine design proposal per the P2 batch note.
Candidate for the `consolidation_base` `_strict` preset specifically.

## The gap (what today's scores do and don't capture)

Both current contraction signals are **single-window, point-in-time ratios**
(`feature_engine.py` Stage F):
- `atr_compression_score = 100 * (1 - clip(atr14 / atr_mean60, ≤1))` — today's
  14-bar ATR vs. its 60-bar mean. One number: "is volatility currently below
  its recent average."
- `volume_dry_up_score = 100 * (1 - clip(vol_mean10 / vol_mean60, ≤1))` —
  10-bar vs. 60-bar mean volume. One number: "is volume currently below its
  recent average."

Neither captures **progressive contraction** — Minervini's defining VCP trait
that each successive pullback within the base is *narrower* than the prior one
(e.g. 25% → 12% → 6%) and *drier* on volume. A flat, uniformly-quiet base and
a true tightening coil can produce identical `atr_compression_score` /
`volume_dry_up_score` today, yet have very different breakout reliability. The
existing `_compute_base` finds the base *window* (longest contiguous low-true-
range run in the last 60 bars) and its overall `range_tightness_score`, but
nothing measures the *sequence* of contractions inside it.

## Proposed detection approach (multi-window, not a single point)

Detect the contraction *sequence* by segmenting the base window into its
successive swings and comparing each to the prior:

1. **Locate the base window** — reuse `_compute_base`'s existing detection
   (longest contiguous ≤ `1.5 × median_TR` run in the last 60 bars). This is
   already computed; sequencing operates *within* that `[base_start, base_end]`
   span, so no change to base detection itself.
2. **Segment into contractions** — within the base window, identify successive
   swing highs→lows (pullback legs) using the same pivot machinery already
   present (`_compute_swing_pivots`). Each leg has a depth
   `(swing_high - swing_low) / swing_high` and an average volume.
3. **Score monotonicity** — over the ordered legs, measure whether depth (and
   volume) is progressively decreasing:
   - `contraction_count` = number of distinct legs found.
   - `depth_sequence_ok` = each leg's depth ≤ prior leg's depth (allowing a
     small tolerance), i.e. a non-increasing depth series.
   - `volume_sequence_ok` = each leg's avg volume ≤ prior leg's (drying).
   - A composite `vcp_sequence_score` (0–100) rewarding more legs + stricter
     monotonic tightening (e.g. Minervini's "2T–4T" — 2 to 4 contractions is
     the classic profile).

## New fields vs. derivable — and the honest answer

**Cannot be derived from the existing stored features** — `atr_compression_score`
/ `volume_dry_up_score` / `range_tightness_score` are already-collapsed scalars;
the leg-by-leg sequence is gone by the time they're computed. Sequencing needs
the **raw price/volume history within the base window**, which the feature
engine already loads per ticker (`ticker_prices` full-history slice — the same
input `_compute_base` and `_compute_swing_pivots` consume). So:
- **No new data source / no provider change** — computable from data already
  in scope during the M11 per-ticker pass.
- **Needs new stored field(s)** — the output (`vcp_sequence_score` and/or
  `contraction_count`) must be persisted to `daily_features` to be usable
  downstream, so this is a **`features_v04` candidate** (a schema bump, same
  pattern as rs_percentile_126d → features_v03). Confirm with architect whether
  to batch it with other v04 features or ship standalone.

## Data / lookback requirements (flagged clearly, as requested)

- **Lookback:** bounded by the base window, which lives within the last
  `_BASE_MAX_DURATION` (60) bars; base detection already requires **≥ 60 bars**
  of history, so tickers with < 60 bars get `NULL` (same gating as the existing
  base features — no new minimum beyond what consolidation already needs).
- **Base-duration dependence — the real caveat:** sequencing only works for
  bases long enough to *contain multiple legs*. A short base (e.g.
  `range_duration` 10–15 bars) may hold only one identifiable contraction, so
  `contraction_count` would be 0–1 and `vcp_sequence_score` uninformative /
  NULL. This is inherent, not a bug: VCP is a *longer-base* pattern
  (Bulkowski/Minervini longer-base-higher-reliability profile). Recommend the
  feature return `NULL` (not 0) when fewer than 2 legs are detectable, and that
  it be wired **only into the `consolidation_base_strict` preset** (which
  already targets the tighter, longer-base VCP-style profile per its seed
  comment), not the base `consolidation_base_v1`.
- **Pivot sensitivity:** leg segmentation depends on `_compute_swing_pivots`'s
  confirmation window (currently 2 bars each side). Tight bases have small
  swings that may not clear the pivot confirmation, under-counting legs. May
  need a base-scoped, more sensitive pivot pass — flag as an implementation
  risk to validate against real bases before trusting the count.

## Scope boundaries (what this note does NOT propose)
- Not proposing to make it a **hard gate** — consistent with the platform's
  scoring-first discipline (fundamentals, rs_percentile_126d all landed
  scoring-only first). Initial use: a soft scoring component in the
  `consolidation_base_strict` scoring weights, or an opt-in floor mirroring
  `enforce_compression_floor`.
- Not proposing thresholds (min contraction count, depth tolerance) — that's
  diagnostics-gated tuning per CLAUDE.md.
- Not touching `_compute_base` detection or the existing point-in-time scores —
  purely additive.

## Recommended next step
Architect sign-off on: (a) `features_v04` schema bump for
`vcp_sequence_score` (+ maybe `contraction_count`), (b) scoping the feature to
`consolidation_base_strict` only, (c) confirming scoring-only (no hard gate) for
V1. Then a follow-up implementation note with the exact segmentation algorithm
and its test plan (including validation against a set of real known-VCP and
known-flat-base tickers, since the leg-detection heuristic needs empirical
checking, not just synthetic unit tests).
