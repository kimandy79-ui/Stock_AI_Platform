# 05_AI_WORKFLOW_PROMPT_TEMPLATES.md

Status: prompt-template reference for the Swing Trading Stock Analyzer project.

Purpose:
This file stores reusable prompt formats for:
1. launching a new coding module in Claude;
2. requesting implementation review in ChatGPT.

This file is NOT a product source of truth.
It does not override:
1. module-specific specs;
2. `01_MASTER_SOURCE_OF_TRUTH_MERGED.md`;
3. `02_PROJECT_IMPLEMENTATION_CONTEXT.md`;
4. accepted stable codebase.

Use this file only as a workflow template reference.

---

# 1. When to use this file

Use this file when creating:

- a short Claude coding prompt for a new module;
- a review prompt for ChatGPT after Claude returns code;
- a repeatable workflow prompt that avoids copying global project instructions.

Do not use this file to define product logic, schema, formulas, interfaces, or trading behavior.

---

# 2. Basic prompt format for a new Claude coding module

Use this template when starting a new coding module in Claude.

```text
Use Project Instructions and Project Files.

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
- Do not modify frozen Modules 01–YY unless required by a failing test or integration blocker.
- Do not change unrelated files.

Module-specific rules:
- `<rule 1, only if needed>`
- `<rule 2, only if needed>`
- `<rule 3, only if needed>`

Module-specific source-of-truth output:
- Create or update `MXX_<MODULE_NAME>_SPEC.md` if this module defines behavior, contracts, schemas, interfaces, data models, public APIs, or rules that future modules will depend on.
- If this module is a small utility module with no future contract, create only a short implementation note instead of a full spec.
- The module-specific spec must be derived from Project Files, the current task, and the accepted implementation. It must not invent new architecture or override higher-priority source-of-truth documents.

Expected output:
Follow the Project Instructions output format.
```

---

# 3. Example Claude coding prompt

Example for Module 06.

```text
Use Project Instructions and Project Files.

Attached:
1. `stock_ai_platform_module05_stable.zip`

Task:
Implement Module 06 — Universe Snapshot Engine.

Current codebase state:
Modules 01–05 are accepted and frozen.
Use `stock_ai_platform_module05_stable.zip` as the implementation base.

Scope:
- Implement only Module 06.
- Do not implement Module 07 or later.
- Do not modify frozen Modules 01–05 unless required by a failing test or integration blocker.
- Do not change unrelated files.

Module-specific rules:
- Maintain monthly ticker universe snapshots.
- Do not implement benchmark loading; that belongs to Module 07.
- Do not implement daily price ingestion; that belongs to Module 08.
- Do not call provider APIs outside the provider layer.
- Use the existing provider interface if market symbols are needed.

Module-specific source-of-truth output:
- Create `M06_UNIVERSE_SNAPSHOT_SPEC.md` if the module defines behavior or contracts future modules will depend on.
- The spec must be derived from Project Files, this task, and the accepted implementation.
- Do not invent new architecture.

Expected output:
Follow the Project Instructions output format.
```

---

# 4. Format for a ChatGPT review prompt

Use this template when Claude returns implementation files and the code needs review by ChatGPT.

```text
Review the attached implementation for Module XX — `<module name>`.

Context:
- Modules 01–YY were previously accepted and should remain frozen.
- Claude was instructed to implement only Module XX.
- Project source of truth:
  1. `01_MASTER_SOURCE_OF_TRUTH_MERGED.md`
  2. `02_PROJECT_IMPLEMENTATION_CONTEXT.md`
  3. module-specific spec, if attached

Review goals:
1. Check whether Claude stayed within scope.
2. Check whether frozen modules were modified unnecessarily.
3. Check correctness against the source-of-truth documents.
4. Check architecture boundaries.
5. Check test quality and whether important edge cases are covered.
6. Check whether the module-specific source-of-truth file is accurate, concise, and consistent with the implementation.
7. Identify bugs, hallucinations, overengineering, or missing requirements.
8. Recommend one of:
   - ACCEPT
   - ACCEPT WITH MINOR FIXES
   - REJECT

Do not rewrite the whole module unless necessary.
Provide concrete fixes only where needed.
```

---

# 5. Example ChatGPT review prompt

```text
Review the attached implementation for Module 06 — Universe Snapshot Engine.

Context:
- Modules 01–05 were previously accepted and should remain frozen.
- Claude was instructed to implement only Module 06.
- Project source of truth:
  1. `01_MASTER_SOURCE_OF_TRUTH_MERGED.md`
  2. `02_PROJECT_IMPLEMENTATION_CONTEXT.md`
  3. `M06_UNIVERSE_SNAPSHOT_SPEC.md`, if attached

Review goals:
1. Check whether Claude stayed within scope.
2. Check whether frozen Modules 01–05 were modified unnecessarily.
3. Check correctness against the source-of-truth documents.
4. Check architecture boundaries.
5. Check test quality and whether important edge cases are covered.
6. Check whether `M06_UNIVERSE_SNAPSHOT_SPEC.md` is accurate, concise, and consistent with the implementation.
7. Identify bugs, hallucinations, overengineering, or missing requirements.
8. Recommend one of:
   - ACCEPT
   - ACCEPT WITH MINOR FIXES
   - REJECT

Do not rewrite the whole module unless necessary.
Provide concrete fixes only where needed.
```

---

# 6. How Claude should use this file

When the user asks Claude to prepare a new module prompt, Claude should:

1. use the coding prompt template from this file;
2. fill in the module number, module name, current stable codebase, frozen module range, and module-specific rules;
3. keep the prompt short;
4. avoid repeating full Project Instructions;
5. avoid copying large sections from source-of-truth documents;
6. include only the module-specific rules needed for the immediate task.

When the user asks Claude to prepare a ChatGPT review prompt, Claude should:

1. use the review prompt template from this file;
2. fill in the module number, module name, frozen module range, and relevant source files;
3. include review goals;
4. ask for ACCEPT / ACCEPT WITH MINOR FIXES / REJECT;
5. avoid asking ChatGPT to rewrite the whole module unless necessary.

---

# 7. What not to include in module prompts

Avoid repeating these in every module prompt because they already live in Project Instructions:

- full source-of-truth priority explanation;
- general coding standards;
- global testing discipline;
- global frozen-module rule;
- general output format;
- DB/provider architecture boundaries;
- instruction to ignore old manifests and old archives.

Include only:
- current module task;
- attached files;
- frozen module range;
- strict scope for the module;
- module-specific rules;
- expected output reference.
