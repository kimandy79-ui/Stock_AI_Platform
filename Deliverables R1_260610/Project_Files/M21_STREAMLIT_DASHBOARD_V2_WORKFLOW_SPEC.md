# M21 — Streamlit Dashboard V2 Workflow Spec

M21 Dashboard V2 is an update to the accepted M21 Dashboard V1.

M21 Dashboard V1 remains accepted stable and is not incomplete. V2 extends the user workflow while preserving existing V1 behavior and tests unless this spec explicitly changes them.

---

## 1. Purpose

M21 Dashboard V2 turns the dashboard from a read-only viewer into a fuller local user workflow surface.

It must still remain a UI/control layer. It must not become a domain engine.

---

## 2. Non-negotiable boundaries

- No provider calls from Streamlit UI or dashboard data-access.
- No heavy market-data, screening, scoring, outcome, simulation, export, or AI logic inside dashboard code.
- No direct DB writes from Streamlit UI or dashboard `data_access`.
- Write-like actions are allowed only through approved service APIs.
- Approved service APIs:
  - M17 Simulation Engine
  - M18 Export Package Engine
  - M19 AI Review Engine
  - M20 Pipeline Orchestrator, only if explicitly required
  - M22 Debug Mode Controller
- Preserve prod/debug/simulation DB isolation.
- Preserve all accepted M21 V1 behavior and tests.

---

## 3. Existing M21 V1 behavior to preserve

The following V1 panels and behavior must continue to work:

- Daily Proposals tab
- Outcome Tracking tab
- Pipeline Health tab
- AI Review metadata tab
- `prod` / `debug` DB role selection
- Signal date selector
- Strategy config selector
- `Show diversified shortlist` checkbox
- Raw vs diversified ordering and filtering
- Amber highlighting where raw/diversified membership differs
- Read-only dashboard data-access
- No provider calls
- No direct writes

Existing `tests/test_dashboard.py` must continue to pass unless a test change is explicitly justified by V2 behavior.

---

## 4. V2 target workflow

### 4.1 Home / Overview

Add a home/overview page or tab showing:

- latest pipeline run status
- latest run date
- selected DB role
- available signal dates
- current proposal counts
- unresolved outcome count if available
- repair queue count if available
- latest AI review count/status if available

Quick actions may navigate the UI state, but must not perform hidden writes.

### 4.2 Daily Proposals with Step 4 drill-down

Extend Daily Proposals:

- user selects a proposal/ticker row or selects ticker from a dropdown
- dashboard shows Step 4 analysis details for the selected proposal/ticker
- display setup type, setup score, entry proxy, stop, target, estimated RR, and mechanical explanation where available
- use stored Step 4 / Step 5 data only
- no recomputation of setup classification, scores, stops, targets, or RR

### 4.3 Export & AI action UI

Add a user-triggered Export & AI panel.

Allowed behavior:

- create ticker review package through M18
- create simulation review package through M18 where applicable
- display generated ZIP path/status
- allow manual AI send through M19 where applicable
- display AI review status and human action status

Forbidden behavior:

- no automatic AI calls on page load
- no direct writes to `ai_reviews` or `sim_ai_reviews`
- no prompt construction outside accepted M18/M19 contracts unless explicitly added through a spec update

### 4.4 Debug Mode UI

Add a Debug Mode panel that calls M22 Debug Mode Controller.

Controls:

- preset dropdown
- run date selector
- sample count input
- optional watchlist text area
- optional strategy selection
- force rerun checkbox, default true
- run button
- result/status display

Rules:

- always target `debug.duckdb`
- never target production or simulation DB
- do not duplicate M22 logic in dashboard

### 4.5 Signal Explorer

Add read-only exploration over already-computed signals/proposals.

Filters may include:

- date range
- ticker
- strategy config
- setup type
- raw/diversified membership
- score range
- sector/industry if available

Data should be read from existing stored outputs such as Step 4, Step 5, ticker metadata, and selected proposal views.

### 4.6 Strategy Performance

Add read-only strategy performance summaries from stored outcomes and/or simulation outputs.

Metrics may include:

- expectancy
- win rate
- average win
- average loss
- profit factor
- max drawdown where available
- resolved outcome percentage

No metric should be invented if the source data is unavailable.

### 4.7 Simulation Lab

Add a Simulation Lab UI that calls M17 Simulation Engine.

Controls may include:

- simulation name
- mode
- date range
- strategy configs
- run button
- result summary
- comparison table

Rules:

- simulation writes are owned only by M17
- dashboard must not write simulation tables directly
- simulation may read production historical data only through approved service behavior

### 4.8 Config Manager

Add optional read-only Config Manager first.

Initial scope:

- view configs
- compare configs
- show active/default configs

Editing, cloning, activation, or persistence of configs requires explicit source-of-truth rules before implementation.

---

## 5. Suggested implementation shape

Dashboard code may be refactored for maintainability.

Suggested structure:

```text
app/dashboard/
  streamlit_app.py
  data_access.py
  actions.py
  components.py
```

Guidelines:

- `data_access.py`: read-only DB queries and pure helpers
- `actions.py`: thin wrappers around approved service APIs
- `components.py`: reusable Streamlit render helpers
- `streamlit_app.py`: UI composition only

This structure is optional, but the boundaries are required.

---

## 6. Testing requirements

Add or update tests for:

- preserved M21 V1 behavior
- read-only data-access
- no direct DB writes from dashboard data-access
- Step 4 drill-down reads stored rows only
- Export & AI action wrappers call M18/M19 fakes
- Debug Mode action wrapper calls M22 fake and forces debug role
- Simulation Lab action wrapper calls M17 fake
- no provider imports/calls from dashboard modules
- DB role validation and isolation
- empty data paths

Targeted tests:

```bash
pytest -q tests/test_dashboard.py
```

Full regression:

```bash
pytest -q
```

---

## 7. Acceptance criteria

M21 Dashboard V2 is accepted only if:

- existing M21 V1 tests still pass
- new V2 tests pass
- no frozen domain module public contracts are changed without explicit approval
- no direct Streamlit/data-access DB writes exist
- action UI delegates to approved service APIs
- prod/debug/simulation DB isolation is preserved
- dashboard remains local, single-user, and non-autonomous
