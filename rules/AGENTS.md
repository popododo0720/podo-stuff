# Podo Stuff Structural Rules

1. **Deterministic Comment Rule (`no-comments`)**:
   - Do **not** write inline narration comments inside function or method bodies explaining what the code does step-by-step.
   - Do **not** leave commented-out dead code or untracked `TODO` / `FIXME` placeholders.
   - Allowed only: module/file headers, API/function docstrings (`"""..."""`, top-level `/** ... */`, `///`), linter/type directives (`# type:`, `// @ts-`, `# noqa`), and explicit rationale tags (`WHY:`, `INVARIANT:`, `SAFETY:`, `NOTE(popododo):`) for non-obvious constraints.
   - `scripts/podo-guard.py` enforces this deterministically at `Stop` (scoped to edits in the current turn) and before every task handoff.

2. **AST Dead-Code Gate (`[DEAD-CODE]`)**:
   - Do **not** leave unreachable statements after `return`, `raise`, `break`, or `continue`.
   - Do **not** introduce unused imports on added lines (outside `__init__.py`, `__all__`, `TYPE_CHECKING` blocks, or explicit `# noqa` directives).
   - Do **not** leave unused private helper functions (`def _name`, excluding `__*__` dunders).
   - `scripts/podo-guard.py` checks added/modified Python files via `ast` whenever `gates.dead_code_check` is enabled.

3. **Structural Enforcement (No Soft Prose Rules for Bugs)**:
   - When a defect or review rejection occurs, never rely on markdown memory notes to prevent recurrence.
   - Encode the fix permanently into the codebase structure:
     1. **Types & API Boundaries** (make invalid states unrepresentable).
     2. **Formal Verification (`specs/*.tla`)** / property tests for concurrency, locks, retries, or state machines.
     3. **Codified Structural Checks**: register deterministic regex or required-command gates in the target repository's `.podo/checks.json` via `python3 ~/.gemini/config/plugins/podo-stuff/scripts/podo-guard.py codify ...`, or add project linters/CI checks.

4. **Two-Tier Verification Cadence**:
   - **Tier 1 (Per `builder` Task — 0 LLM tokens)**: Run `podo-guard.py check --repo . --base <base_ref>` plus fast project-local tests after every `builder` task. Fix all script-gate failures before posting readiness.
   - **Tier 2 (Per DAG Phase Boundary — `interrogator` Role)**: Run the context-isolated `interrogator` **only once per DAG Phase (Wave)** after all parallel `builder` tasks in that Phase pass Tier 1. Never invoke an LLM verifier per individual micro-task.
