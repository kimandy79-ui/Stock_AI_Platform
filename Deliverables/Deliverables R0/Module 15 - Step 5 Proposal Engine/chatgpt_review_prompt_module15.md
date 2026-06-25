# ChatGPT Code Review Prompt — Module 15: Step 5 Proposal Engine

## Instructions for ChatGPT

You are a senior Python engineer conducting a focused code review of **Module 15
— Step 5 Proposal Engine** from the Swing Trading Stock Analyzer project. The
full source, spec, and key context are embedded below.

Your review should be **concrete and actionable**. For every finding, state:
1. The file and line / section
2. What the problem is (or what is done well)
3. The recommended fix or improvement

Structure your response using the categories listed under **Review scope**. Be
direct. Do not paraphrase the code back. Skip sections where you have nothing
material to add.

---

## Project context (brief)

- Python 3.11+, DuckDB for persistence.
- Pipeline: Step 3 Screening → Step 4 Analysis → **Step 5 Proposals** (this
  module) → Step 6+
- All DB access goes through a single `duckdb_manager` module; no direct
  `duckdb` import, no `ATTACH`, no DDL in service modules.
- Every service returns a `ServiceResult(status, run_id, rows_processed,
  warnings, errors, metadata)`.
- Module 15 **only inserts** into `step5_proposals` — no updates, deletes, DDL,
  or other table writes.
- Reruns are append-only: a new `run_id` produces a new set of rows.

---

## Review scope

Review the code across these dimensions. Be specific; skip any dimension where
the code is clean.

### 1. Correctness of scoring and ranking logic
- `proposal_score_raw` formula: weights sum to 1.0; formula matches spec.
- RR tier boundaries (3.0 / 2.2 / 1.8): closed/open interval correctness.
- Raw-rank sort key: DESC score, DESC rr (NULL = −∞), ASC ticker.
- Score clamping to [0, 100] — raw and final.

### 2. Hard-cap diversification
- Processing strictly in `raw_rank` order.
- Correct rejection condition: `prior_count >= cap` (bucket *already full*).
- Rejected rows: still inserted, `diversified_rank = NULL`, `final = raw`, no
  penalty, `rejection_reason` set.
- Both-caps-full priority: `sector_cap` over `industry_cap`.
- Accepted rows: sequential `diversified_rank`, counts increment correctly.

### 3. Soft-penalty diversification
- `prior_count` is the count of *earlier raw-rank* candidates in the same bucket
  (not the count *including* the current candidate).
- Multiplier formula: `penalty ** max(0, prior_count)`. First occurrence gets
  exponent 0 (no penalty). Correct?
- Re-ranking by `proposal_score_final` DESC, then `ticker` ASC.
- No hard rejections; `rejection_reason = None` for all rows.

### 4. NULL / missing input handling
- NULL `setup_score` or `screening_score` → skip (not analyzable), count in
  `analyses_read`.
- NULL `timing_score` → 50.0.
- NULL `estimated_rr` → `rr_score = 0`, sorts lowest.
- NULL sector/industry → sentinel strings.
- Are all sentinel / default substitutions happening before scoring (not after)?

### 5. Config validation
- All required keys validated *before* any DB access.
- Bool vs int type guards (Python `bool` is a subclass of `int`; `isinstance(x,
  bool)` check must come first).
- `top_n > 0`, `max_*_count >= 1`, `0 < penalty <= 1.0` — boundary correctness.
- Hard-cap vs soft-penalty conditional: are the right keys checked for each mode?

### 6. Transaction model and write safety
- Read connection opened read-only, closed before any mutation.
- Write: single `BEGIN TRANSACTION` / per-row `execute()` / `COMMIT`.
- `ROLLBACK` on any exception; does the ROLLBACK run even if `COMMIT` raises?
- Empty-rows fast path (no transaction opened).
- Does `_write` return the row count in all paths?

### 7. `ServiceResult` contract
- `rows_processed == metadata["proposals_written"]` on every return path
  (success, empty, guard failure, config failure, read failure, write failure).
- `analyses_read` preserved on write failure.
- Exactly 9 metadata keys on every path (no extras, no missing).

### 8. `_build_rows` structure and clarity
- Are the 5 phases (filter, score, raw-rank, diversify, selected-semantics) clean
  and separable?
- Is the inner sort-key closure (`_raw_sort_key`) readable? Any edge-case risk?
- Is `diversity_penalty = raw - final` always non-negative? Can it be negative in
  soft-penalty mode?

### 9. Code quality and project conventions
- Module docstring completeness.
- Type hints — any missing, incorrect, or overly broad `Any` that could be
  narrowed?
- `Final` / `Protocol` usage consistent with the frozen Module 14 style.
- No `print()`, no `duckdb` import, no provider imports — confirm.
- Logging: are all three phases (start, guard failures, done) covered? Anything
  missing?
- `__all__` completeness.

### 10. Test suite (`tests/test_step5_proposal_engine.py`)
- Coverage gaps: is there any specified behaviour not covered by at least one
  test?
- Fixture and seeding helper design — any fragility or boilerplate concerns?
- `_FailingConn` rollback test: does it correctly verify the rollback happened?
- Static-scan tests: are all forbidden patterns covered?
- Any tests that test implementation details rather than contract behaviour?

### 11. Open gaps and assumptions (from the spec)
- **`G-SOFT-PENALTY-PRIOR-COUNT`**: the module uses config key names
  `sector_penalty`/`industry_penalty` while the example strategy-config blocks in
  `01c_FORMULAS_AND_CONFIGS.md` use `sector_penalty_factor`/`industry_penalty_factor`
  and `sector_max_positions`/`industry_max_positions`. Is this handled correctly,
  and how should it be resolved?
- **`G-UNKNOWN-BUCKET`**: NULL sector/industry rows all funnel into a single
  `__UNKNOWN_SECTOR__` bucket. Does this create a correctness problem when hard
  caps are small? Suggest the safest resolution.
- Any other gaps you identify that the spec or code does not address.

---

## Module spec (M15_STEP5_PROPOSAL_ENGINE_SPEC.md)

```
# M15 — Step 5 Proposal Engine — Module Spec

Module-specific source of truth for Module 15. Derived from the frozen split
Project Files and the frozen Module 14 style. Where this spec and a
higher-priority Project File disagree, the Project File wins; this spec only
fills gaps the Project Files leave open (recorded under Assumptions / open gaps).

## Purpose & pipeline position

Module 15 runs after Module 14 (Step 4 Setup Analysis) and before Module 16. It
reads the Step 4 analyses for one `signal_date` / `strategy_config_id`, joins
each to its Step 3 screening score and its ticker's sector / industry, scores and
raw-ranks every analyzable analysis, applies diversification (hard-cap or
soft-penalty), and appends one `step5_proposals` row per analyzable analysis in a
single transaction.

## Public API

    class Step5ProposalEngine:
        def __init__(self, db_manager=None) -> None: ...
        def propose(
            self,
            signal_date: date,
            strategy_config: dict,
            strategy_config_id: str,
            db_role: str = "prod",
            run_id: str | None = None,
        ) -> ServiceResult: ...

## ServiceResult metadata (exact keys, every path)

    db_role
    signal_date              # ISO string
    strategy_config_id
    run_id
    analyses_read            # all step4_analysis rows read (analyzable or not)
    proposals_written        # == rows_processed
    raw_top_n_count          # rows with in_raw_top_n
    diversified_top_n_count  # rows with in_diversified_top_n
    hard_cap_rejections      # rows with a non-NULL rejection_reason

On guard/config failure all counts are 0. On write failure: failed, rollback,
proposals_written=0, rows_processed=0, analyses_read preserved.

## Scoring

    rr_score = 100 if estimated_rr >= 3.0
               80  if 2.2 <= estimated_rr < 3.0
               60  if 1.8 <= estimated_rr < 2.2
               0   otherwise / NULL

    proposal_score_raw = 0.40*setup_score + 0.25*screening_score
                       + 0.20*rr_score    + 0.15*timing_score

Scores clamped to [0, 100].

## Raw ranking

Sort by proposal_score_raw DESC, estimated_rr DESC (NULL lowest), ticker ASC.
Assign 1-based raw_rank. in_raw_top_n = raw_rank <= top_n.

## Diversified ranking

Hard-cap: process in raw_rank order; reject if sector/industry bucket already
full; rejected rows still inserted (diversified_rank=NULL, final=raw, no penalty,
rejection_reason set); both-full -> sector_cap wins; accepted rows get sequential
diversified_rank.

Soft-penalty: process in raw_rank order; apply penalty**prior_count multipliers;
re-rank by final DESC, ticker ASC; rejection_reason=None for all.

Shared:
    in_diversified_top_n = diversified_rank is not NULL and diversified_rank <= top_n
    selected_flag        = in_diversified_top_n
    selected_top_n       = in_raw_top_n OR in_diversified_top_n

## Assumptions / open gaps

G-SOFT-PENALTY-PRIOR-COUNT: config key names sector_penalty/industry_penalty
(prompt) vs sector_penalty_factor/industry_penalty_factor (01c example blocks).
G-UNKNOWN-BUCKET: NULL sector/industry bucket under sentinels; participates in
caps/penalties like any named bucket.
```

---

## Engine source (app/services/proposal/step5_proposal_engine.py — 841 lines)

```python
"""Module 15 — Step 5 Proposal Engine.

Reads the Step 4 analyses for one ``signal_date`` / ``strategy_config_id`` from
``step4_analysis``, joins each analysis to its Step 3 screening score
(``step3_candidates`` on ``candidate_id``) and its ticker's sector / industry
(``ticker_master`` on ``ticker``), computes a raw proposal score, assigns a raw
ranking, applies either *hard-cap* or *soft-penalty* diversification, and appends
one row per *analyzable* Step 4 analysis to ``step5_proposals`` in a single
transaction. It runs after Module 14 (Step 4 Setup Analysis) and before Module 16.
...
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from typing import Any, Final, Protocol

from app.database import duckdb_manager
from app.utils import logging_config
from app.utils import service_result
from app.utils.service_result import ServiceResult

DB_ROLE_PROD: Final[str] = duckdb_manager.DB_ROLE_PROD
DB_ROLE_DEBUG: Final[str] = duckdb_manager.DB_ROLE_DEBUG
ALLOWED_DB_ROLES: Final[tuple[str, ...]] = (DB_ROLE_PROD, DB_ROLE_DEBUG)

UNKNOWN_SECTOR: Final[str] = "__UNKNOWN_SECTOR__"
UNKNOWN_INDUSTRY: Final[str] = "__UNKNOWN_INDUSTRY__"
DEFAULT_TIMING_SCORE: Final[float] = 50.0
REJECT_SECTOR_CAP: Final[str] = "sector_cap"
REJECT_INDUSTRY_CAP: Final[str] = "industry_cap"

_W_SETUP: Final[float] = 0.40
_W_SCREENING: Final[float] = 0.25
_W_RR: Final[float] = 0.20
_W_TIMING: Final[float] = 0.15

METADATA_KEYS: Final[tuple[str, ...]] = (
    "db_role", "signal_date", "strategy_config_id", "run_id",
    "analyses_read", "proposals_written", "raw_top_n_count",
    "diversified_top_n_count", "hard_cap_rejections",
)


class _DbManagerLike(Protocol):
    def connect(self, db_role: str, read_only: bool = ...) -> Any: ...


class _ConfigError(ValueError):
    pass


_SELECT_ANALYSES: Final[str] = (
    "SELECT "
    "  a.analysis_id AS analysis_id, "
    "  a.candidate_id AS candidate_id, "
    "  a.ticker AS ticker, "
    "  a.setup_score AS setup_score, "
    "  a.timing_score AS timing_score, "
    "  a.estimated_rr AS estimated_rr, "
    "  c.screening_score AS screening_score, "
    "  t.sector AS sector, "
    "  t.industry AS industry "
    "FROM step4_analysis a "
    "LEFT JOIN step3_candidates c ON c.candidate_id = a.candidate_id "
    "LEFT JOIN ticker_master t ON t.ticker = a.ticker "
    "WHERE a.signal_date = ? "
    "  AND a.strategy_config_id = ? "
    "ORDER BY a.ticker, a.analysis_id"
)

_INSERT_PROPOSAL: Final[str] = (
    "INSERT INTO step5_proposals "
    "(proposal_id, run_id, strategy_config_id, ticker, signal_date, "
    " proposal_score_raw, diversity_penalty, proposal_score_final, "
    " rank_position, raw_rank, diversified_rank, in_raw_top_n, "
    " in_diversified_top_n, diversification_applied, selected_top_n, "
    " selected_flag, ai_reviewed, executed_flag, rejection_reason, "
    " mechanical_explanation, sector_count_at_selection, "
    " industry_count_at_selection, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, FALSE, FALSE, ?, "
    " ?, ?, ?, CAST(now() AS TIMESTAMP))"
)


def _json_dumps(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True)


def _f(value: Any) -> float | None:
    if value is None:
        return None
    fv = float(value)
    if fv != fv:  # NaN
        return None
    return fv


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _rr_score(estimated_rr: float | None) -> float:
    if estimated_rr is None:
        return 0.0
    if estimated_rr >= 3.0:
        return 100.0
    if 2.2 <= estimated_rr < 3.0:
        return 80.0
    if 1.8 <= estimated_rr < 2.2:
        return 60.0
    return 0.0


def _proposal_score_raw(
    setup_score: float,
    screening_score: float,
    rr_score: float,
    timing_score: float,
) -> float:
    raw = (
        _W_SETUP * setup_score
        + _W_SCREENING * screening_score
        + _W_RR * rr_score
        + _W_TIMING * timing_score
    )
    return _clamp(raw)


class Step5ProposalEngine:
    """Score + rank + diversify Step 4 analyses for one signal date / config."""

    def __init__(self, db_manager: _DbManagerLike | None = None) -> None:
        self._db: _DbManagerLike = (
            db_manager if db_manager is not None else duckdb_manager
        )

    def propose(
        self,
        signal_date: date,
        strategy_config: dict,
        strategy_config_id: str,
        db_role: str = "prod",
        run_id: str | None = None,
    ) -> ServiceResult:
        run_id = run_id if run_id is not None else str(uuid.uuid4())
        log = logging_config.get_logger(__name__, run_id)
        signal_iso = signal_date.isoformat()

        log.info(
            "propose start db_role=%s signal_date=%s strategy_config_id=%s",
            db_role, signal_iso, strategy_config_id,
        )

        if db_role not in ALLOWED_DB_ROLES:
            message = (
                f"Unsupported db_role {db_role!r}. "
                f"Module 15 writes only to {list(ALLOWED_DB_ROLES)}."
            )
            log.error("propose failed: %s", message)
            return self._failed(run_id, db_role, signal_iso, strategy_config_id, message)

        try:
            cfg = self._parse_config(strategy_config)
        except _ConfigError as exc:
            log.error("propose failed: %s", exc)
            return self._failed(run_id, db_role, signal_iso, strategy_config_id, str(exc))

        try:
            analyses = self._read(db_role, signal_date, strategy_config_id)
        except Exception as exc:
            message = f"read failed: {type(exc).__name__}: {exc}"
            log.error("propose failed: %s", message)
            return self._failed(run_id, db_role, signal_iso, strategy_config_id, message)

        analyses_read = len(analyses)

        if analyses_read == 0:
            log.info("propose done: no step4 analyses for %s", signal_iso)
            return self._success(run_id, db_role, signal_iso, strategy_config_id,
                                 analyses_read=0, rows=[])

        rows = self._build_rows(analyses, cfg, run_id, strategy_config_id, signal_date)

        try:
            self._write(db_role, rows)
        except Exception as exc:
            log.error("propose failed during write (rolled back): %s: %s",
                      type(exc).__name__, exc)
            return self._failed(run_id, db_role, signal_iso, strategy_config_id,
                                f"{type(exc).__name__}: {exc}",
                                analyses_read=analyses_read)

        log.info("propose done status=success analyses_read=%d proposals_written=%d",
                 analyses_read, len(rows))
        return self._success(run_id, db_role, signal_iso, strategy_config_id,
                             analyses_read=analyses_read, rows=rows)

    @staticmethod
    def _parse_config(strategy_config: dict) -> dict[str, Any]:
        if not isinstance(strategy_config, dict):
            raise _ConfigError("strategy_config must be a dict")

        block = strategy_config.get("diversification")
        if not isinstance(block, dict):
            raise _ConfigError("missing config section diversification")

        if "hard_cap_enabled" not in block:
            raise _ConfigError("missing config key diversification.hard_cap_enabled")
        hard_cap_enabled = block["hard_cap_enabled"]
        if not isinstance(hard_cap_enabled, bool):
            raise _ConfigError("config key diversification.hard_cap_enabled must be bool")

        if "top_n" not in block:
            raise _ConfigError("missing config key diversification.top_n")
        top_n = block["top_n"]
        if isinstance(top_n, bool) or not isinstance(top_n, int):
            raise _ConfigError("config key diversification.top_n must be int")
        if not top_n > 0:
            raise _ConfigError("config key diversification.top_n must be > 0")

        cfg: dict[str, Any] = {"hard_cap_enabled": hard_cap_enabled, "top_n": top_n}

        if hard_cap_enabled:
            for key in ("max_sector_count", "max_industry_count"):
                if key not in block:
                    raise _ConfigError(f"missing config key diversification.{key}")
                value = block[key]
                if isinstance(value, bool) or not isinstance(value, int):
                    raise _ConfigError(f"config key diversification.{key} must be int")
                if value < 1:
                    raise _ConfigError(f"config key diversification.{key} must be >= 1")
                cfg[key] = value
        else:
            for key in ("sector_penalty", "industry_penalty"):
                if key not in block:
                    raise _ConfigError(f"missing config key diversification.{key}")
                value = block[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise _ConfigError(f"config key diversification.{key} must be numeric")
                fvalue = float(value)
                if not (0.0 < fvalue <= 1.0):
                    raise _ConfigError(f"config key diversification.{key} must be in (0, 1]")
                cfg[key] = fvalue

        return cfg

    def _read(self, db_role: str, signal_date: date,
              strategy_config_id: str) -> list[dict[str, Any]]:
        cols = ("analysis_id", "candidate_id", "ticker", "setup_score",
                "timing_score", "estimated_rr", "screening_score", "sector", "industry")
        connection = self._db.connect(db_role, read_only=True)
        try:
            raw_rows = connection.execute(
                _SELECT_ANALYSES, [signal_date, strategy_config_id]
            ).fetchall()
        finally:
            connection.close()
        return [dict(zip(cols, raw)) for raw in raw_rows]

    def _build_rows(
        self,
        analyses: list[dict[str, Any]],
        cfg: dict[str, Any],
        run_id: str,
        strategy_config_id: str,
        signal_date: date,
    ) -> list[dict[str, Any]]:
        top_n = cfg["top_n"]
        scored: list[dict[str, Any]] = []

        for a in analyses:
            setup_score = _f(a["setup_score"])
            screening_score = _f(a["screening_score"])
            if setup_score is None or screening_score is None:
                continue

            estimated_rr = _f(a["estimated_rr"])
            timing_raw = _f(a["timing_score"])
            timing_score = DEFAULT_TIMING_SCORE if timing_raw is None else timing_raw
            rr_score = _rr_score(estimated_rr)
            raw_score = _proposal_score_raw(setup_score, screening_score, rr_score, timing_score)

            sector = a["sector"] if a["sector"] is not None else UNKNOWN_SECTOR
            industry = a["industry"] if a["industry"] is not None else UNKNOWN_INDUSTRY

            scored.append({
                "analysis_id": a["analysis_id"],
                "candidate_id": a["candidate_id"],
                "ticker": a["ticker"],
                "setup_score": setup_score,
                "screening_score": screening_score,
                "timing_score": timing_score,
                "estimated_rr": estimated_rr,
                "rr_score": rr_score,
                "proposal_score_raw": raw_score,
                "sector": sector,
                "industry": industry,
            })

        if not scored:
            return []

        def _raw_sort_key(item: dict[str, Any]) -> tuple[float, float, str]:
            rr = item["estimated_rr"]
            rr_for_sort = rr if rr is not None else float("-inf")
            return (-item["proposal_score_raw"], -rr_for_sort, item["ticker"])

        scored.sort(key=_raw_sort_key)
        for idx, item in enumerate(scored, start=1):
            item["raw_rank"] = idx
            item["in_raw_top_n"] = idx <= top_n

        if cfg["hard_cap_enabled"]:
            self._apply_hard_cap(scored, cfg)
        else:
            self._apply_soft_penalty(scored, cfg)

        rows: list[dict[str, Any]] = []
        for item in scored:
            div_rank = item["diversified_rank"]
            in_div_top_n = div_rank is not None and div_rank <= top_n
            selected_flag = in_div_top_n
            selected_top_n = item["in_raw_top_n"] or in_div_top_n
            raw_score = item["proposal_score_raw"]
            final_score = item["proposal_score_final"]
            diversity_penalty = raw_score - final_score

            explanation = {
                "analysis_id": item["analysis_id"],
                "candidate_id": item["candidate_id"],
                "ticker": item["ticker"],
                "sector": item["sector"],
                "industry": item["industry"],
                "setup_score": item["setup_score"],
                "screening_score": item["screening_score"],
                "timing_score": item["timing_score"],
                "estimated_rr": item["estimated_rr"],
                "rr_score": item["rr_score"],
                "proposal_score_raw": raw_score,
                "proposal_score_final": final_score,
                "raw_rank": item["raw_rank"],
                "diversified_rank": div_rank,
                "diversification_mode": (
                    "hard_cap" if cfg["hard_cap_enabled"] else "soft_penalty"
                ),
                "rejection_reason": item["rejection_reason"],
                "sector_count_at_selection": item["sector_count_at_selection"],
                "industry_count_at_selection": item["industry_count_at_selection"],
            }

            rows.append({
                "proposal_id": str(uuid.uuid4()),
                "run_id": run_id,
                "strategy_config_id": strategy_config_id,
                "ticker": item["ticker"],
                "signal_date": signal_date,
                "proposal_score_raw": raw_score,
                "diversity_penalty": diversity_penalty,
                "proposal_score_final": final_score,
                "rank_position": item["raw_rank"],
                "raw_rank": item["raw_rank"],
                "diversified_rank": div_rank,
                "in_raw_top_n": item["in_raw_top_n"],
                "in_diversified_top_n": in_div_top_n,
                "diversification_applied": True,
                "selected_top_n": selected_top_n,
                "selected_flag": selected_flag,
                "rejection_reason": item["rejection_reason"],
                "mechanical_explanation": _json_dumps(explanation),
                "sector_count_at_selection": item["sector_count_at_selection"],
                "industry_count_at_selection": item["industry_count_at_selection"],
            })
        return rows

    @staticmethod
    def _apply_hard_cap(scored: list[dict[str, Any]], cfg: dict[str, Any]) -> None:
        max_sector = cfg["max_sector_count"]
        max_industry = cfg["max_industry_count"]
        accepted_sector: dict[str, int] = {}
        accepted_industry: dict[str, int] = {}
        next_div_rank = 1

        for item in scored:
            sector = item["sector"]
            industry = item["industry"]
            prior_sector = accepted_sector.get(sector, 0)
            prior_industry = accepted_industry.get(industry, 0)
            sector_full = prior_sector >= max_sector
            industry_full = prior_industry >= max_industry

            item["proposal_score_final"] = item["proposal_score_raw"]

            if sector_full or industry_full:
                item["diversified_rank"] = None
                item["rejection_reason"] = (
                    REJECT_SECTOR_CAP if sector_full else REJECT_INDUSTRY_CAP
                )
                item["sector_count_at_selection"] = prior_sector
                item["industry_count_at_selection"] = prior_industry
            else:
                item["diversified_rank"] = next_div_rank
                next_div_rank += 1
                item["rejection_reason"] = None
                accepted_sector[sector] = prior_sector + 1
                accepted_industry[industry] = prior_industry + 1
                item["sector_count_at_selection"] = accepted_sector[sector]
                item["industry_count_at_selection"] = accepted_industry[industry]

    @staticmethod
    def _apply_soft_penalty(scored: list[dict[str, Any]], cfg: dict[str, Any]) -> None:
        sector_penalty = cfg["sector_penalty"]
        industry_penalty = cfg["industry_penalty"]
        seen_sector: dict[str, int] = {}
        seen_industry: dict[str, int] = {}

        for item in scored:
            sector = item["sector"]
            industry = item["industry"]
            prior_sector = seen_sector.get(sector, 0)
            prior_industry = seen_industry.get(industry, 0)

            sector_multiplier = sector_penalty ** max(0, prior_sector)
            industry_multiplier = industry_penalty ** max(0, prior_industry)
            final_score = _clamp(
                item["proposal_score_raw"] * sector_multiplier * industry_multiplier
            )

            item["proposal_score_final"] = final_score
            item["rejection_reason"] = None
            item["sector_count_at_selection"] = prior_sector + 1
            item["industry_count_at_selection"] = prior_industry + 1

            seen_sector[sector] = prior_sector + 1
            seen_industry[industry] = prior_industry + 1

        order = sorted(
            range(len(scored)),
            key=lambda i: (-scored[i]["proposal_score_final"], scored[i]["ticker"]),
        )
        for div_rank, pos in enumerate(order, start=1):
            scored[pos]["diversified_rank"] = div_rank

    def _write(self, db_role: str, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0

        connection = self._db.connect(db_role)
        try:
            connection.execute("BEGIN TRANSACTION")
            try:
                for row in rows:
                    connection.execute(
                        _INSERT_PROPOSAL,
                        [
                            row["proposal_id"], row["run_id"],
                            row["strategy_config_id"], row["ticker"],
                            row["signal_date"], row["proposal_score_raw"],
                            row["diversity_penalty"], row["proposal_score_final"],
                            row["rank_position"], row["raw_rank"],
                            row["diversified_rank"], row["in_raw_top_n"],
                            row["in_diversified_top_n"], row["diversification_applied"],
                            row["selected_top_n"], row["selected_flag"],
                            row["rejection_reason"], row["mechanical_explanation"],
                            row["sector_count_at_selection"],
                            row["industry_count_at_selection"],
                        ],
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        finally:
            connection.close()
        return len(rows)

    def _success(self, run_id, db_role, signal_iso, strategy_config_id, *,
                 analyses_read, rows) -> ServiceResult:
        proposals_written = len(rows)
        raw_top_n_count = sum(1 for r in rows if r["in_raw_top_n"])
        diversified_top_n_count = sum(1 for r in rows if r["in_diversified_top_n"])
        hard_cap_rejections = sum(1 for r in rows if r["rejection_reason"] is not None)
        return ServiceResult(
            status=service_result.STATUS_SUCCESS,
            run_id=run_id,
            rows_processed=proposals_written,
            metadata=self._metadata(
                db_role=db_role, signal_date=signal_iso,
                strategy_config_id=strategy_config_id, run_id=run_id,
                analyses_read=analyses_read, proposals_written=proposals_written,
                raw_top_n_count=raw_top_n_count,
                diversified_top_n_count=diversified_top_n_count,
                hard_cap_rejections=hard_cap_rejections,
            ),
        )

    def _failed(self, run_id, db_role, signal_iso, strategy_config_id, message,
                *, analyses_read=0) -> ServiceResult:
        return ServiceResult(
            status=service_result.STATUS_FAILED,
            run_id=run_id,
            rows_processed=0,
            errors=[message],
            metadata=self._metadata(
                db_role=db_role, signal_date=signal_iso,
                strategy_config_id=strategy_config_id, run_id=run_id,
                analyses_read=analyses_read, proposals_written=0,
                raw_top_n_count=0, diversified_top_n_count=0, hard_cap_rejections=0,
            ),
        )

    @staticmethod
    def _metadata(*, db_role, signal_date, strategy_config_id, run_id,
                  analyses_read, proposals_written, raw_top_n_count,
                  diversified_top_n_count, hard_cap_rejections) -> dict[str, Any]:
        return {
            "db_role": db_role, "signal_date": signal_date,
            "strategy_config_id": strategy_config_id, "run_id": run_id,
            "analyses_read": analyses_read, "proposals_written": proposals_written,
            "raw_top_n_count": raw_top_n_count,
            "diversified_top_n_count": diversified_top_n_count,
            "hard_cap_rejections": hard_cap_rejections,
        }
```

---

## Notes for the reviewer

- The full 841-line original engine is faithfully reproduced above (docstrings
  condensed slightly for token efficiency; all logic is verbatim).
- The test file (`tests/test_step5_proposal_engine.py`, ~900 lines) covers the
  full contract listed in the spec's *Tests* section — review from the spec
  description rather than the file if token budget is a concern, or request it
  separately.
- Focus your deepest attention on items 2 (hard-cap), 3 (soft-penalty), 6
  (transaction safety), and 11 (open gaps) — these are the areas most likely to
  carry subtle correctness bugs that unit tests won't catch.
