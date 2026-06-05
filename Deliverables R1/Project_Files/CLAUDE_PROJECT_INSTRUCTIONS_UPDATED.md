# Claude Project Instructions — Current Alignment Addendum

Use `SOURCE_OF_TRUTH_INDEX.md` first.

The active next work is **Module 21 Dashboard V2 Update**.

M21 Dashboard V1 remains accepted stable and is not incomplete. Keep `M21_STREAMLIT_DASHBOARD_SPEC.md` as the accepted V1 contract. Use `M21_STREAMLIT_DASHBOARD_V2_WORKFLOW_SPEC.md` as the active next-work spec.

Do not treat the old codebase `docs/` folder as authoritative. In the cleaned baseline it is intentionally removed to avoid source-of-truth drift.

Dashboard V2 constraints:

- preserve existing M21 V1 behavior and tests unless explicitly changed by the V2 spec
- no direct DB writes from Streamlit UI or dashboard data-access
- no provider calls from dashboard
- no heavy domain logic in dashboard
- action UI must call approved service APIs only
- approved action services: M17, M18, M19, M20 only if explicitly required, and M22
- maintain prod/debug/simulation DB isolation
