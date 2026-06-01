# 00_PROJECT_FILE_MAP

Status: active navigation file for Claude Project Files.
Purpose: reduce retrieval noise and token usage by directing Claude to the smallest relevant source file.

## Active source files

Use these split files instead of the old monolithic merged files.

| Need | Use file |
|---|---|
| Precedence, guardrails, critical merged decisions, project scope, enum/reference values | `01a_CORE_PRINCIPLES.md` |
| Schema, tables, fields, views, indexes, constraints, `ServiceResult` contract | `01b_SCHEMA_AND_DATA.md` |
| Feature formulas, Step 3/4/5 scoring, outcome calculations, strategy configs | `01c_FORMULAS_AND_CONFIGS.md` |
| Module roadmap, module responsibilities, pipeline order, error handling, trading calendar, simulation flow | `01d_MODULES_AND_PIPELINE.md` |
| UI, dashboard, exports, test plan, golden dataset, debug presets | `01e_UI_AND_TESTING.md` |
| Coding standards, implementation workflow, logging, config rules, testing rules, performance rules | `02_PROJECT_IMPLEMENTATION_CONTEXT.md` |
| Full active architecture decisions by §22.x number | `02b_ARCHITECTURE_DECISIONS.md` |
| Current module contracts and accepted implementation details | `MXX_<MODULE_NAME>_SPEC.md` |

## Retrieval rule

Use the smallest relevant source file. Do not load or summarize unrelated source files.

## Do not use as active coding guidance

When the split files are available, do not use these as implementation guidance:

- old FULL / PATCH / MINI PATCH archives;
- duplicate manifests;
- old prompt drafts;
- `manifest.json`;
- archived monolithic merged source files.

## Safe archive policy

The old monolithic files may be kept locally as backup, but should not remain active in Claude Project Files together with the split files.
