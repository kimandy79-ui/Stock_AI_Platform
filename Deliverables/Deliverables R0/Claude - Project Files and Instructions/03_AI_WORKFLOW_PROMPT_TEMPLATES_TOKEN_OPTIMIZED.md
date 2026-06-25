# 03_AI_WORKFLOW_PROMPT_TEMPLATES.md

Status: token-optimized prompt-template reference for the Swing Trading Stock Analyzer project.

Purpose:
This file stores reusable, short prompt formats for:
1. launching a new coding module in Claude;
2. requesting implementation review in ChatGPT;
3. asking Claude to fix implementation after review;
4. keeping Project Instructions, Project Files, module specs, and task prompts aligned without repeating the same rules.

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
- module-specific constraints not already obvious from Project Files or module specs;
- targeted source retrieval hints only when helpful;
- expected output reference.

Claude/ChatGPT must use Project Instructions and Project Files for the rest.

The task prompt should be a launcher, not a duplicate source-of-truth document.

---

# 1.1 Prompt size budget

Default module prompts should be short.

Target length:

- Claude coding prompt: 400–900 words.
- ChatGPT review prompt: 300–700 words.
- Fix-after-review prompt: 200–500 words.

Exceed these limits only when:

- the module-specific spec is missing, weak, or known to be incomplete;
- a critical module-specific contract is not documented anywhere else;
- the user explicitly asks for a self-contained prompt.

Do not paste schema definitions, formulas, long SQL mappings, full test plans, full source-of-truth priority lists, or long architecture explanations into the prompt.

Instead, point to the relevant Project File or module spec.

---

# 1.2 What belongs where

| Content type | Put it in |
|---|---|
| Global behavior, silent work, output style, frozen-module discipline | Project Instructions |
| Schema, formulas, configs, module roadmap, architecture decisions | Split Project Files |
| Current module contract and future dependency rules | Module-specific spec |
| Stable codebase zip, frozen range, task name, narrow scope, special constraints | Current task prompt |
| Detailed durable behavior discovered during coding | Module-specific spec, not repeated in future prompts |
| Review result and fix list | Fix-after-review prompt |

If a module-specific spec exists, the coding prompt should reference it instead of repeating it.

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

Do not ask the model to load, summarize, or quote unrelated Project Files.

---

# 3. Standard Claude coding prompt

Use this template when starting a new coding module in Claude.

```text
Use Project Instructions and Project Files.
Use `00_PROJECT_FILE_MAP.md` for targeted retrieval only.
Do not repeat or summarize global rules.

Attached:
1. `<current_stable_codebase>.zip`
2. `<module_specific_spec>.md`, if applicable

Task:
Implement Module XX — `<module name>`.

Current codebase:
Modules 01–YY are accepted and frozen.
Use `<current_stable_codebase>.zip` as the implementation base.

Scope:
Implement only Module XX. Do not implement later modules or modify unrelated/frozen modules unless required by a failing test or real integration blocker.

Module-specific constraints:
- `<only constraints unique to this module>`
- `<do not restate rules already covered by Project Instructions, Project Files, or module spec>`
- `<prefer 3–7 bullets>`

Source retrieval hints, only if helpful:
- `<schema/tables → 01b_SCHEMA_AND_DATA.md>`
- `<formulas/scoring → 01c_FORMULAS_AND_CONFIGS.md>`
- `<module/pipeline responsibility → 01d_MODULES_AND_PIPELINE.md>`
- `<architecture decision → 02b_ARCHITECTURE_DECISIONS.md>`

Output:
Follow Project Instructions. Work silently. Return only actionable results.
```

Rules for using this template:

- Keep normal coding prompts within 400–900 words.
- If a module-specific spec exists, reference it instead of repeating it.
- Do not paste long schema, SQL, formula, provider, DB, testing, or output rules into the prompt.
- Put durable module behavior into `MXX_<MODULE_NAME>_SPEC.md`, not into reusable prompts.
- Include only constraints that are unique to the current module.

---

# 4. Ultra-short Claude coding prompt

Use this when the module-specific spec is strong and current Project Files are complete.

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
- `<only constraints not already covered by the module spec>`

Output:
Follow Project Instructions. Work silently. Return only actionable results.
```

---

# 5. Example Claude coding prompt — Module 06

```text
Use Project Instructions and Project Files.
Use `00_PROJECT_FILE_MAP.md` for targeted retrieval only.
Do not repeat or summarize global rules.

Attached:
1. `stock_ai_platform_module05_stable.zip`
2. `M06_UNIVERSE_SNAPSHOT_SPEC.md`, if available

Task:
Implement Module 06 — Universe Snapshot Engine.

Current codebase:
Modules 01–05 are accepted and frozen.
Use `stock_ai_platform_module05_stable.zip` as the implementation base.

Scope:
Implement only Module 06. Do not implement Module 07 or later. Do not modify unrelated/frozen modules unless required by a failing test or real integration blocker.

Module-specific constraints:
- Maintain ticker universe state and monthly snapshots.
- Do not implement benchmark/sector ETF loading; that belongs to Module 07.
- Do not implement daily price ingestion; that belongs to Module 08.
- Use existing database/provider abstractions.

Source retrieval hints:
- Universe snapshot schema → `01b_SCHEMA_AND_DATA.md`
- Module 06 boundaries and pipeline position → `01d_MODULES_AND_PIPELINE.md`
- Coding/testing/logging rules → `02_PROJECT_IMPLEMENTATION_CONTEXT.md`
- Monthly snapshot decision → `02b_ARCHITECTURE_DECISIONS.md`

Output:
Follow Project Instructions. Work silently. Return only actionable results.
```

---

# 6. ChatGPT implementation review prompt

Use this template when Claude returns implementation files and the code needs review by ChatGPT.

```text
Review the attached implementation for Module XX — `<module name>`.

Use Project Instructions and Project Files.
Use `00_PROJECT_FILE_MAP.md` for targeted retrieval only.
Do not rely on archived monolithic source files or old FULL/PATCH/MINI PATCH files if split Project Files are available.

Attached:
1. `<claude_output_or_updated_codebase>.zip`
2. `<module_specific_spec>.md`, if applicable
3. `<test_output_or_notes>.txt`, if applicable

Context:
- Modules 01–YY were previously accepted and should remain frozen.
- Claude was instructed to implement only Module XX.
- Focus on correctness, scope control, integration safety, and tests.

Review goals:
1. Check scope compliance and frozen-module protection.
2. Check correctness against relevant Project Files and module spec.
3. Check architecture boundaries relevant to this module.
4. Check test quality and important edge cases.
5. Check whether the module-specific spec or implementation note is accurate, concise, and consistent with the implementation.
6. Identify bugs, hallucinations, overengineering, duplicated logic, or missing requirements.
7. Recommend one of: ACCEPT / ACCEPT WITH MINOR FIXES / REJECT.

Do not rewrite the whole module unless necessary.
Provide only concrete findings and fixes.
```

Rules for using this template:

- Keep review prompts within 300–700 words.
- Do not paste global source-of-truth lists.
- Do not paste full code snippets unless the review must focus on a specific suspected bug.
- Include only the module-specific risks that matter for the review.

---

# 7. Example ChatGPT review prompt — Module 06

```text
Review the attached implementation for Module 06 — Universe Snapshot Engine.

Use Project Instructions and Project Files.
Use `00_PROJECT_FILE_MAP.md` for targeted retrieval only.
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
1. Check Module 06 scope compliance and frozen-module protection.
2. Check correctness against relevant Project Files and `M06_UNIVERSE_SNAPSHOT_SPEC.md`.
3. Check DuckDB/database-layer and provider-layer boundaries.
4. Check test quality, especially idempotency, monthly snapshot behavior, empty input, inactive/delisted tickers, and DB isolation.
5. Check whether `M06_UNIVERSE_SNAPSHOT_SPEC.md` is accurate, concise, and consistent with the implementation.
6. Recommend one of: ACCEPT / ACCEPT WITH MINOR FIXES / REJECT.

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

Rules for using this template:

- Keep fix prompts within 200–500 words.
- Paste the review comments or attach them as a file.
- Do not restate the whole original coding prompt.
- Do not add new requirements unless the review identified a real gap.

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
- long excerpts from schema, formulas, architecture decisions, or module specs;
- full SQL upsert statements when schema/source files already define the tables;
- full column mappings unless unique, critical, and absent from Project Files/specs;
- long “read this file, then this file” lists;
- complete test plans when a concise checklist is enough;
- repeated allowed/forbidden global file lists.

Include only:

- current task;
- attached files;
- frozen module range;
- strict module scope;
- module-specific constraints;
- targeted source retrieval hints;
- expected output reference.

---

# 9.1 Prompt anti-patterns

Avoid prompts that become self-contained mini-specs.

Bad pattern:

```text
Paste all table columns, DTO fields, SQL statements, full test plan, and full Project File priority list into the module prompt.
```

Good pattern:

```text
Use `01b_SCHEMA_AND_DATA.md` for table definitions and `M04_PROVIDER_INTERFACE_SPEC.md` for provider DTOs.
Module-specific constraint: do not bypass the provider interface.
```

Bad pattern:

```text
Read file A section 1, file B section 2, file C section 3, then summarize everything before coding.
```

Good pattern:

```text
Use `00_PROJECT_FILE_MAP.md` for targeted retrieval only. Do not summarize sources.
```

Bad pattern:

```text
Re-list every forbidden future module and every global architecture boundary.
```

Good pattern:

```text
Implement only Module XX. Do not implement later modules or modify unrelated/frozen modules unless required by a failing test or real integration blocker.
```

---

# 10. Alignment rule

Project Instructions define behavior.
Project Files define source of truth.
Module-specific specs define durable module contracts.
This file defines reusable prompt shapes only.

If this file conflicts with Project Instructions, Project Files, or a module-specific spec, ignore this file and follow the higher-priority source.

---

# 11. Final checklist before sending a prompt

Before sending a Claude coding prompt, check:

- Is the prompt under the normal size budget?
- Does it avoid copying Project Instructions?
- Does it avoid copying schema/formulas/source-file contents?
- Does it reference the module spec instead of repeating it?
- Does it include only task-specific constraints?
- Does it clearly state the frozen module range?
- Does it clearly state what zip to use as the implementation base?
- Does it tell Claude to use targeted retrieval only?

If yes, the prompt is ready.
