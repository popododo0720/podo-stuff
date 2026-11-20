# Podo Stuff Structural Rules

1. **Deterministic Comment Rule (`no-comments`)**:
   - Do **not** write inline narration comments inside function or method bodies explaining what the code is doing step-by-step.
   - Do **not** leave commented-out dead code or untracked `TODO` / `FIXME` placeholders.
   - Allowed only: module/file headers, API/function docstrings (`"""..."""`, `/** ... */`, `///`), linter/type directives (`# type:`, `// @ts-`, `# noqa`), and explicit rationale tags (`WHY:`, `INVARIANT:`, `SAFETY:`, `NOTE(popododo):`) for non-obvious constraints.
   - `scripts/podo-guard.py` enforces this deterministically at `Stop` and before every task handoff.

2. **Structural Enforcement (No Soft Prose Rules for Bugs)**:
   - When a defect or review rejection occurs, never rely on markdown memory notes to prevent recurrence.
   - Encode the fix permanently into the codebase structure:
     1. **Types & API Boundaries** (make invalid states unrepresentable).
     2. **Formal Verification (`specs/*.tla`)** / property tests for concurrency, locks, retries, or state machines.
     3. **Codified Structural Checks**: register deterministic regex/command gates in `.podo/checks.json` via `python3 ~/.gemini/config/plugins/podo-stuff/scripts/podo-guard.py codify ...` or project linters/CI.

3. **Two-Tier Verification Cadence**:
   - **Tier 1 (Per Dev Task — 0 LLM tokens)**: Run `podo-guard.py check` + fast local checks after every single `Orchestrator (Neon)` dev task.
   - **Tier 2 (Per DAG Phase Boundary — `Claude Opus 5.5 (Medium)`)**: Run the LLM `interrogator` **only once per DAG Phase (Wave)** after all parallel tasks in that Phase pass Tier 1. Never invoke an LLM verifier per individual micro-task.
