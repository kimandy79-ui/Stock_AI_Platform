# 03_AI_WORKFLOW_PROMPT_TEMPLATES.md

Status: token-optimized prompt-template reference for the Swing Trading Stock Analyzer project.

Purpose:
This file stores reusable, short prompt formats for:
1. launching a new coding module in Claude;
2. requesting implementation review in ChatGPT;
3. keeping Project Instructions, Project Files, and task prompts aligned without repeating the same rules.

This file is NOT a product source of truth.
It does not override:
1. Project Instructions;
2. module-specific specs;
3. split Project Files;
4. accepted stable codebase.

Use this file only as a workflow template reference.

---

# 1. Core principle

Prompts should be short.

Do not copy global rules into every prompt. Those rules already live in Project Instructions and Project Files.

Each task prompt should include only:

- what module/task to perform;
- which stable codebase zip to use;
- which modules are frozen;
- module-specific scope limits;
- module-specific rules that are not obvious from Project Files;
- expected output reference.

Claude/ChatGPT must use Project Instructions and Project Files for the rest.

---

# 2. Project File routing reference

Use Project Instructions first.

For source retrieval, use `00_PROJECT_FILE_MAP.md` and the smallest relevant source file.

Quick routing:

- Precedence, guardrails, enums, critical merged decisions → `01a_CORE_PRINCIPLES.md`
- Schema, tables, fields, views, indexes, constraints, `ServiceResult` → `01b_SCHEMA_AND_DATA.md`
- Formulas, scoring, features, strategy configs, outcome calculations → `01c_FORMULAS_AND_CONFIGS.md`
- Module roadmap, responsibilities, pipeline, simulation, trading calendar → `01d_MODULES_AND_PIPELINE.md`
- UI, dashboard, exports, test plan, golden dataset, debug presets → `01e_UI_AND_TESTING.md`
- Coding standards, workflow, logging, testing, config, performance rules → `02_PROJECT_IMPLEMENTATION_CONTEXT.md`
- Numbered architecture decisions → `02b_ARCHITECTURE_DECISIONS.md`
- Current module contract → `MXX_<MODULE_NAME>_SPEC.md`, if provided

Do not ask the model to load or summarize unrelated Project Files.

---

# 3. Basic Claude coding prompt

Use this template when starting a new coding module in Claude.

```text
Use Project Instructions and Project Files.
Use `00_PROJECT_FILE_MAP.md` to retrieve only the smallest relevant source files.
Do not repeat or summarize global rules unless they affect the current task.

Attached:
1. `<current_stable_codebase>.zip`
2. `<module_specific_spec>.md`, if applicable

Task:
Implement Module XX — `<module name>`.

Current codebase state:
Modules 01–YY are accepted and frozen.
Use `<current_stable_codebase>.zip` as the implementation base.

Scope:
- Implement only Module XX.
- Do not implement Module XX+1 or later.
- Do not modify frozen Modules 01–YY unless required by a failing test or real integration blocker.
- Do not change unrelated files.

Module-specific rules:
- `<rule 1, only if needed>`
- `<rule 2, only if needed>`
- `<rule 3, only if needed>`

Source retrieval hints, if needed:
- `<example: schema/tables → 01b_SCHEMA_AND_DATA.md>`
- `<example: formulas/scoring → 01c_FORMULAS_AND_CONFIGS.md>`
- `<example: architecture decision 22.X → 02b_ARCHITECTURE_DECISIONS.md>`

Output:
Follow the Project Instructions output format.
Work silently and provide only actionable results.
```

---

# 4. Minimal Claude coding prompt

Use this shorter version when the module-specific spec is strong and Project Files are already loaded.

```text
Use Project Instructions and Project Files.
Use `00_PROJECT_FILE_MAP.md` for targeted retrieval only.

Attached:
1. `<current_stable_codebase>.zip`
2. `<module_specific_spec>.md`

Task:
Implement Module XX — `<module name>`.

Frozen modules:
Modules 01–YY are accepted and frozen.

Scope:
Implement only Module XX. Do not implement later modules or modify unrelated/frozen modules unless required by a failing test or real integration blocker.

Module-specific constraints:
- `<only constraints not already obvious from the module spec>`

Output:
Follow Project Instructions. Work silently. Return only actionable results.
```

---

# 5. Example Claude coding prompt — Module 06

```text
Use Project Instructions and Project Files.
Use `00_PROJECT_FILE_MAP.md` to retrieve only the smallest relevant source files.
Do not repeat or summarize global rules unless they affect the current task.

Attached:
1. `stock_ai_platform_module05_stable.zip`
2. `M06_UNIVERSE_SNAPSHOT_SPEC.md`, if available

Task:
Implement Module 06 — Universe Snapshot Engine.

Current codebase state:
Modules 01–05 are accepted and frozen.
Use `stock_ai_platform_module05_stable.zip` as the implementation base.

Scope:
- Implement only Module 06.
- Do not implement Module 07 or later.
- Do not modify frozen Modules 01–05 unless required by a failing test or real integration blocker.
- Do not change unrelated files.

Module-specific rules:
- Maintain ticker universe state and monthly snapshots.
- Do not implement benchmark/sector ETF loading; that belongs to Module 07.
- Do not implement daily price ingestion; that belongs to Module 08.
- Do not call provider APIs outside the provider layer.
- Use existing database and provider abstractions.

Source retrieval hints:
- Universe snapshot schema → `01b_SCHEMA_AND_DATA.md`
- Module 06 boundaries and pipeline position → `01d_MODULES_AND_PIPELINE.md`
- Coding/testing/logging rules → `02_PROJECT_IMPLEMENTATION_CONTEXT.md`
- Related architecture decision on monthly snapshots → `02b_ARCHITECTURE_DECISIONS.md`

Output:
Follow Project Instructions. Work silently and provide only actionable results.
```

---

# 6. ChatGPT implementation review prompt

Use this template when Claude returns implementation files and the code needs review by ChatGPT.

```text
Review the attached implementation for Module XX — `<module name>`.

Use Project Instructions and Project Files.
Use `00_PROJECT_FILE_MAP.md` to retrieve only the smallest relevant source files.
Do not rely on archived monolithic source files or old FULL/PATCH/MINI PATCH files if split Project Files are available.

Attached:
1. `<claude_output_or_updated_codebase>.zip`
2. `<module_specific_spec>.md`, if applicable
3. `<test_output_or_notes>.txt`, if applicable

Context:
- Modules 01–YY were previously accepted and should remain frozen.
- Claude was instructed to implement only Module XX.
- The review should focus on correctness, scope control, integration safety, and tests.

Review goals:
1. Check whether Claude stayed within Module XX scope.
2. Check whether frozen Modules 01–YY were modified unnecessarily.
3. Check correctness against the relevant Project Files and module-specific spec.
4. Check architecture boundaries.
5. Check test quality and important edge cases.
6. Check whether the module-specific spec or implementation note is accurate, concise, and consistent with the implementation.
7. Identify bugs, hallucinations, overengineering, duplicated logic, or missing requirements.
8. Recommend one of:
   - ACCEPT
   - ACCEPT WITH MINOR FIXES
   - REJECT

Do not rewrite the whole module unless necessary.
Provide only concrete findings and fixes.
```

---

# 7. Example ChatGPT review prompt — Module 06

```text
Review the attached implementation for Module 06 — Universe Snapshot Engine.

Use Project Instructions and Project Files.
Use `00_PROJECT_FILE_MAP.md` to retrieve only the smallest relevant source files.
Do not rely on archived monolithic source files or old FULL/PATCH/MINI PATCH files if split Project Files are available.

Attached:
1. `stock_ai_platform_module06_stable.zip`
2. `M06_UNIVERSE_SNAPSHOT_SPEC.md`, if included in Claude output
3. pytest output, if available

Context:
- Modules 01–05 were previously accepted and should remain frozen.
- Claude was instructed to implement only Module 06.
- Module 07 benchmark/sector ETF loading and Module 08 daily price ingestion are out of scope.

Review goals:
1. Check whether Claude stayed within Module 06 scope.
2. Check whether frozen Modules 01–05 were modified unnecessarily.
3. Check correctness against relevant Project Files and `M06_UNIVERSE_SNAPSHOT_SPEC.md`.
4. Check DuckDB/database-layer boundaries.
5. Check provider-layer boundaries.
6. Check test quality, especially idempotency, monthly snapshot behavior, empty input, inactive/delisted tickers, and DB isolation.
7. Check whether `M06_UNIVERSE_SNAPSHOT_SPEC.md` is accurate, concise, and consistent with the implementation.
8. Recommend one of:
   - ACCEPT
   - ACCEPT WITH MINOR FIXES
   - REJECT

Do not rewrite the whole module unless necessary.
Provide only concrete findings and fixes.
```

---

# 8. Prompt for fixing Claude output after review

Use this when ChatGPT returns review comments and Claude needs to fix the implementation.

```text
Use Project Instructions and Project Files.
Use `00_PROJECT_FILE_MAP.md` for targeted retrieval only.

Attached:
1. `<current_module_codebase>.zip`
2. ChatGPT review comments
3. `<module_specific_spec>.md`, if applicable

Task:
Fix only the issues listed in the review for Module XX — `<module name>`.

Scope:
- Do not add new features.
- Do not refactor unrelated code.
- Do not modify frozen modules unless the review identified a real integration blocker.
- Keep fixes minimal and test-covered.

Output:
Follow Project Instructions. Include updated zip, changed files, test command/results, and short notes.
Work silently and provide only actionable results.
```

---

# 9. What not to include in module prompts

Avoid repeating these in every prompt because they already live in Project Instructions or Project Files:

- full source-of-truth priority explanation;
- full list of split Project Files;
- general coding standards;
- global testing discipline;
- global frozen-module rule beyond the current frozen range;
- general output format;
- DB/provider architecture boundaries, unless the module specifically touches them;
- instruction to ignore old manifests and old archives, unless there is a risk they are attached;
- long excerpts from schema, formulas, architecture decisions, or module specs.

Include only:

- current task;
- attached files;
- frozen module range;
- strict module scope;
- module-specific constraints;
- targeted source retrieval hints;
- expected output reference.

---

# 10. Alignment rule

Project Instructions define behavior.
Project Files define source of truth.
This file defines reusable prompt shapes only.

If this file conflicts with Project Instructions or Project Files, ignore this file and follow the higher-priority source.
