Review the attached implementation for Module 13 — Step 3 Screening.

Use Project Instructions and Project Files.
Use `00_PROJECT_FILE_MAP.md` for targeted retrieval only.
Do not rely on archived monolithic source files or old FULL/PATCH/MINI PATCH files if split Project Files are available.

Attached:
1. `stock_ai_platform_module13_stable.zip`
2. `M13_STEP3_SCREENING_SPEC.md`
3. pytest output — NOT AVAILABLE (sandbox had no polars/duckdb/pytest; run `pytest tests/test_step3_screening.py` in the project environment and attach output before final verdict)

Context:
- Modules 01–12 were previously accepted and must remain frozen.
- Claude was instructed to implement only Module 13.
- Module 14 (Step 4 Setup Analysis) and later are out of scope.
- The only write target for this module is `step3_candidates`.

---

Review goals:

1. **Scope compliance and frozen-module protection.**
   Confirm no Modules 01–12 files were modified beyond the additive README note.
   Confirm Module 14+ logic is absent.

2. **Correctness against Project Files and `M13_STEP3_SCREENING_SPEC.md`.**
   Focus on:
   - Hard filter logic: exactly 6 filters; all fail labels match spec; all failures collected (not short-circuit); NULL/missing input fails the relevant filter.
   - Scoring sub-formulas: EMA alignment (0/50/100 input), EMA50-distance band/taper, RSI14 bands, ROC20 bands, sector RS bands (NULL → 50), consolidation_score passthrough, breakout_proximity bands (including the unspecified (0.5, 1.5] gap), pullback bands, RVOL sub-score.
   - Weights: confirm they are read from `scoring_weights.*` at config top level, NOT from `screening.scoring_weights`.
   - Final score: weighted sum clamped 0–100; `screening_score = NULL` for failed-filter rows.
   - Market regime map (bull→100, neutral→60, bear→20, high_risk→0, extreme_risk→0, unknown/NULL→50).
   - Missing `sector_relative_strength` → neutral 50, no warning log.

3. **Architecture boundaries.**
   - `step3_screening.py` must not import `duckdb` directly, use `ATTACH`, issue DDL (`CREATE/ALTER/DROP TABLE`), call `print()`, or import any provider module.
   - All DB access must go through the approved DuckDB manager layer.
   - Only `step3_candidates` is written.

4. **Transaction and write correctness.**
   - Single batch INSERT in one transaction; ROLLBACK on failure leaves zero partial rows.
   - `rows_processed == metadata["candidates_written"]` on every code path.
   - `run_id`: minted (uuid4) when `None`, preserved when supplied.
   - `db_role` guard: `simulation` and any unlisted role must be rejected **before** any DB access; only `prod` and `debug` are allowed.
   - Append-only: no DELETE or UPDATE against `step3_candidates`.

5. **Metadata keys.**
   Confirm exactly 11 keys present on every `ServiceResult`: `db_role`, `signal_date`, `strategy_config_id`, `run_id`, `tickers_evaluated`, `passed_hard_filters`, `failed_hard_filters`, `candidates_written`, `screening_score_min`, `screening_score_max`, `screening_score_mean`.
   Score stats must be computed over **passed rows only**; must be `None` when no rows pass.

6. **Test quality.**
   Key coverage to verify:
   - Each of the 6 hard filter labels individually (including NULL/missing-row cases).
   - All-failures collected (not short-circuit).
   - Passed vs. failed row semantics (both written, correct `passed_hard_filters` flag).
   - All 5 named regime mappings + unknown/NULL → 50.
   - Score reproducibility (same input → same score).
   - `simulation` db_role rejected before DB access.
   - Empty input → success, zero counts, no INSERT.
   - One-transaction rollback leaves no partial rows.
   - Only `step3_candidates` written (write-ownership test).
   - DB isolation (no real prod/debug/simulation DB files; tmp_path or monkeypatch).

7. **`M13_STEP3_SCREENING_SPEC.md` accuracy.**
   Check that documented spec gaps and their resolutions (G-WEIGHTS-PATH, G-VOL-ADV, G-EMA50-TAPER, G-BP-MIDBAND, A-CLOSE-EMA200, A-NULL-COMPONENT) are accurately described and consistent with the actual implementation.

8. **Verdict.**
   Recommend one of: **ACCEPT** / **ACCEPT WITH MINOR FIXES** / **REJECT**.

---

Do not rewrite the whole module unless necessary.
Provide only concrete findings and fixes.
Flag the open spec gap G-VOL-ADV (avg_dollar_volume_20d sub-score mapping absent from Project Files) as a known acknowledged gap — this is not a bug.
