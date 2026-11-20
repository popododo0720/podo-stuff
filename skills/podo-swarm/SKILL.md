---
name: podo-swarm
description: >-
  Run phased parallel DAG swarm orchestration on top of native Antigravity
  /swarm without modifying built-in swarm internals. Automatically decomposes work
  into maximum-parallel DAG phases, runs zero-token deterministic checks per task,
  batches context-isolated interrogator verification once per DAG phase boundary,
  supports cross-pool Arena bake-offs, and keeps an offline Gantt DAG dashboard
  and observability telemetry synced.
---

# Podo Swarm — Phased DAG Orchestrator

Orchestrate multi-agent work on top of native Antigravity `swarm` tools (`swarm`, `post_message`, `read_board`, `swarm_wait`) while optimizing **both engineering rigor and token/time efficiency**.

Do **not** modify any built-in Antigravity files. Always stay inside native `swarm` + `podo-guard.py`.

---

## Step 0: Load Active Role Models Dynamically

Read the active role-to-model mapping from `podo-config.json` before spawning any worker:

```bash
python3 ~/.gemini/config/plugins/podo-stuff/scripts/podo-guard.py config --show
```

Resolve every worker model strictly from the returned `roles` object (`roles.scout`, `roles.builder`, `roles.arena_a`, `roles.arena_b`, `roles.interrogator`, `roles.monitor`). Never hardcode model names or versions in briefs or swarm orchestration steps.

Record the starting commit reference before launching a Phase:

```bash
BASE_SHA="$(git rev-parse HEAD)"
```

Pass `BASE_SHA` explicitly into every `builder` and `interrogator` brief so committed branch changes in shared workspaces are always checked against the Phase base.

---

## Step 1: Auto-DAG Decomposition into Maximum-Parallel Phases

1. **Scout First Only If Context Is Missing**:
   - If the target codebase architecture, call graph, or constraints are unfamiliar, spawn 1–2 `scout` workers (`model: <roles.scout>`, `workspace: "inherit"`) in `phase-0` to map relevant files, the target project's own test/CI/lint commands, and ownership boundaries.
2. **Respect Target Project Test & Check Isolation**:
   - Target repositories define their own test suites, linters, and optional `.podo/checks.json` / `.podo/verify.sh` rules.
   - Plugin development tests live exclusively in the plugin repository's `tests/` directory and are never copied into or imposed onto user repositories.
3. **Partition Into Disjoint DAG Phases (`phase-1`, `phase-2`, ...)**:
   - Maximize parallelism within every Phase by splitting independent work along **strictly disjoint file ownership**. Two `builder` workers in the same Phase must **never** touch the same file.
   - Dependent follow-up work goes into the next Phase (`depends_on`).
   - For high-risk or multi-phase architectural swarms, if `egm-design`, `egm-zoom-out`, and `egm-implementation-readiness` subagents (from the Elephant-Goldfish Model plugin) are available, optionally run a fresh-eyes Goldfish review on the drafted DAG plan before spawning builders. Keep `interrogator` strictly for Tier-2 code, diff, and test verification.
4. **Initialize Live DAG Artifacts (`podo-dag.state.json` / `podo-dag.md` / `podo-dag.html`)**:
   - Use your conversation's artifact directory (`<appDataDir>/brain/<conversation-id>/`):
     ```bash
     python3 ~/.gemini/config/plugins/podo-stuff/scripts/podo-guard.py dag init \
       --state-file "<artifact_dir>/podo-dag.state.json" \
       --md-out "<artifact_dir>/podo-dag.md" \
       --html-out "<artifact_dir>/podo-dag.html" \
       --title "<Swarm Goal>" \
       --phases-json '<phases_json>'
     ```
   - `podo-dag.state.json` automatically tracks `started_at`, `finished_at`, `duration_s`, and `interrogator_calls` on every phase and node as transitions occur (active states set `started_at`; `verified`/`rejected` set `finished_at` and `duration_s`).
   - Present `[podo-dag.html](file://<artifact_dir>/podo-dag.html)` (100% offline HTML dashboard featuring the **Phase & Worker Timeline (Gantt View)** and the **Observability & Token Estimate** panel) to the user.

---

## Step 2: Two-Tier Verification Cadence & Context-Isolated Review

Never spend `interrogator` turns on individual micro-tasks. Split verification into **two tiers**:

### Tier 1 — Per-Task Deterministic Script Gate (0 LLM Tokens)
Every `builder` (`model: <roles.builder>`, `workspace: "share"`) MUST follow these rules in its brief:
- Own **only** its assigned files; never edit files outside its slice.
- Follow `no-comments` and `[DEAD-CODE]` rules (zero inline narration comments inside function bodies, zero unreachable code, zero unused imports or unused private helpers).
- Before posting readiness, run the deterministic checker with `--base <BASE_SHA>` plus the target repository's own tests/typecheck/build for its slice. When `.podo/` telemetry is enabled (or `--telemetry-file` / `PODO_TELEMETRY_FILE` is set), tag the invocation with `--phase`, `--worker`, and `--role` (or export `PODO_PHASE`, `PODO_WORKER`, `PODO_ROLE`):
  ```bash
  python3 ~/.gemini/config/plugins/podo-stuff/scripts/podo-guard.py check \
    --repo . --base <BASE_SHA> --phase phase-N --worker <worker-name> --role builder --run-verify
  ```
  (Plain `check` never auto-creates `.podo/` in clean repos unless `.podo/` already exists or `--telemetry-file` is passed.)
- If `podo-guard.py check` or project tests exit non-zero, the `builder` **fixes all violations itself in the same turn** without waking `@interrogator`.
- Commit changes in its shared workspace only after `exit 0` and passing tests, then post **ONLY** the compact structured status line with **zero prose rationalizations**:
  ```text
  @interrogator [phase-N-ready] task=<task-id> branch=<branch> base=<BASE_SHA> cmd="<verify-cmd>" exit=0
  ```

### Tier 2 — Per-DAG-Phase Context-Isolated `Interrogator` (1 Run per Phase)
Add **one** `interrogator` (`model: <roles.interrogator>`, `workspace: "inherit"`) for the swarm with these exact gating and isolation rules in its brief:
- **Strict Phase Gate**: Whenever woken, call `read_board{since: <phase_start_seq>}` and check whether **ALL** builders in active Phase `N` have posted `[phase-N-ready]`. If **any** task in Phase `N` is still missing, do **NOT** run checks and do **NOT** call `post_message` — end your turn immediately to park in `waiting` at 0 token cost.
- **Context-Isolated Review (Contract + Diff + Test Proof Only)**: Once ALL builders in Phase `N` have posted `[phase-N-ready]`:
  - Ignore any builder chatter or prose explanations.
  - Judge **ONLY** against:
    1. The explicit **task contract** (requirements and disjoint file boundaries),
    2. The actual **git diff** (`git diff <BASE_SHA>...HEAD` + working tree / untracked files) and deterministic check run:
       ```bash
       python3 ~/.gemini/config/plugins/podo-stuff/scripts/podo-guard.py check \
         --repo <workspace_or_repo> --base <BASE_SHA> --phase phase-N --worker interrogator --role interrogator --run-verify
       ```
    3. Direct **command/test execution proof** run against the target project's own checks.
  - Inspect all Phase `N` branches/diffs together for integration conflicts, edge cases, concurrency/state invariants, and missing assertions.
  - If a defect or recurring mistake is found:
    - Do **not** edit builder files yourself.
    - Require a permanent structural fix — Type/API boundary tightening, a `.podo/checks.json` rule via `podo-guard.py codify --id ... --fix ...`, target project CI/linter rules, or a `specs/*.tla` TLA+ specification / property test for concurrency/state machines.
    - Post `@<builder> [reject] <defect, reproduction, and required structural guardrail>` so only the responsible builder fixes and re-submits.
  - When Phase `N` passes completely:
    - Post `@coordinator [phase-N-verified] <concise proof summary>` and end your turn.

---

## Step 3: Arena Playbook (`roles.arena_a` vs `roles.arena_b`)

Trigger **Arena mode** when the user writes `arena:`, asks to compare approaches, or tackles high-risk core algorithms/concurrency bugs where competing implementations add clear value:

1. Spawn two independent contenders with `workspace: "share"` on the exact same task brief and `BASE_SHA`:
   - `contender-a` (`model: <roles.arena_a>`)
   - `contender-b` (`model: <roles.arena_b>`)
2. Require **both** contenders to pass Tier 1 (`podo-guard.py check --repo . --base <BASE_SHA> --role arena_a|arena_b --run-verify` + target project tests) before posting the compact contract line:
   ```text
   @interrogator [arena-ready] task=<task-id> branch=<branch> base=<BASE_SHA> cmd="<verify-cmd>" exit=0
   ```
3. `interrogator` (`model: <roles.interrogator>`) stays silent until **both** `[arena-ready]` posts arrive, then performs **one** context-isolated evaluation comparing only task contract, diffs, and direct command/test proof:
   - Real execution & edge-case resilience (including TLA+ / property tests if stateful)
   - Minimal, cleanest diff with zero inline narration comments or dead code
   - Structural safety (types, linters, codified checks)
4. `interrogator` posts `@coordinator [arena-winner] <winner or graft plan>`, and Coordinator merges the winning workspace (or grafts the best parts).

---

## Step 4: Native Swarm UI, Phase-Boundary Batch DAG Sync & Final Observability Report

1. Name the swarm concisely: `swarm{action: "title", title: "podo-swarm: <goal>"}`.
2. Add `monitor` (`model: <roles.monitor>`) if `swarm_monitor` is in your skill list.
3. **Restrict `dag update` to Phase Boundaries Using Batch `--set-nodes`**:
   - Do **not** invoke `dag update` on every individual micro-event. Update the DAG strictly at Phase boundaries (Phase start, `[reject]` cycle, or `[phase-N-verified]`) in a single batched call (which also records timestamps, durations, `interrogator_calls`, and re-renders the offline Gantt timeline + observability panel):
     ```bash
     python3 ~/.gemini/config/plugins/podo-stuff/scripts/podo-guard.py dag update \
       --state-file "<artifact_dir>/podo-dag.state.json" \
       --set-nodes "task-1=verified,task-2=verified" \
       --phase "phase-1" --gate-status "verified" --note "<concise proof>"
     ```
   - Optional `--started-at` and `--finished-at` flags override auto-captured timestamps when needed.
   - Supported statuses: `pending`, `building`, `script_checked`, `arena_running`, `phase_interrogating`, `rejected`, `verified`.
4. Merge shared workspaces (`git merge` / `cherry-pick` from verified worker branches) into the main workspace as each Phase completes, then kill finished workers only after their work is safely landed (never kill `monitor` early).
5. Always end the creation turn (and status replies) with the exact `open_board` string returned by `swarm` on its own line, alongside a link to `[podo-dag.html](file://<artifact_dir>/podo-dag.html)`.
6. When the final Phase passes:
   - Post `[done] <summary>` (with NO `@mention`) to quiet `monitor`.
   - Run final `podo-guard.py check --repo . --base <BASE_SHA> --run-verify` on the merged workspace.
   - Generate the final observability report and session snapshot to review gate pass/fail breakdown, estimated role token usage, Tier-1 saved tokens, anomaly warnings, and `trace-index.json` links:
     ```bash
     python3 ~/.gemini/config/plugins/podo-stuff/scripts/podo-guard.py telemetry snapshot \
       --repo . \
       --state-file "<artifact_dir>/podo-dag.state.json" \
       --conversation-id "<conversation_id>" \
       --transcript-path "<artifact_dir>/.system_generated/logs/transcript.jsonl" \
       --artifact-dir "<artifact_dir>"
     python3 ~/.gemini/config/plugins/podo-stuff/scripts/podo-guard.py telemetry report \
       --repo . \
       --state-file "<artifact_dir>/podo-dag.state.json"
     ```
   - Present the final summary, observability highlights, and `[podo-dag.html](file://<artifact_dir>/podo-dag.html)` link to the user.
