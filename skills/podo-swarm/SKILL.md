---
name: podo-swarm
description: >-
  Run popododo's pstack + OmO DAG swarm orchestration on top of native Antigravity
  /swarm without modifying built-in swarm internals. Automatically decomposes work
  into maximum-parallel DAG phases, runs zero-token deterministic checks per task,
  batches Claude Opus 5.5 (Medium) interrogation strictly once per DAG phase boundary,
  supports Neon vs Opus Arena bake-offs, and keeps a live DAG artifact synced.
---

# Podo Swarm — `pstack` + OmO DAG Orchestrator

Orchestrate multi-agent work on top of native Antigravity `swarm` tools (`swarm`, `post_message`, `read_board`, `swarm_wait`) while optimizing **both engineering rigor and token/time efficiency**.

Do **not** modify any built-in Antigravity files. Always stay inside native `swarm` + `podo-guard.py`.

---

## Step 0: Load Active Role Models

Read the configured role models before spawning any worker:

```bash
python3 ~/.gemini/config/plugins/podo-stuff/scripts/podo-guard.py config --show
```

Expected defaults (unless changed via `/podo-setup`):
- `scout`: `gemini-4-hydrogen-medium`
- `builder`: `gemini-pro-orchestrator-neon`
- `arena_a`: `gemini-pro-orchestrator-neon`
- `arena_b`: `claude-opus-5-5-medium`
- `interrogator`: `claude-opus-5-5-medium`
- `monitor`: `gemini-4-hydrogen-low`

---

## Step 1: Auto-DAG Decomposition into Maximum-Parallel Phases

1. **Scout First Only If Context Is Missing**:
   - If the codebase/call-chain (`/how`, `/why`) is unfamiliar, spawn 1–2 `scout` workers (`model: <roles.scout>`, `workspace: "inherit"`) in `phase-0` to map relevant files, existing test/CI/lint commands, and ownership boundaries.
2. **Partition Into Disjoint DAG Phases (`phase-1`, `phase-2`, ...)**:
   - Maximize parallelism within every Phase by splitting independent work along **strictly disjoint file ownership**. Two builders in the same Phase must **never** touch the same file.
   - Dependent follow-up work goes into the next Phase (`depends_on`).
3. **Initialize Live DAG Artifacts (`podo-dag.json` / `podo-dag.md` / `podo-dag.html`)**:
   - Use your conversation's artifact directory (`<appDataDir>/brain/<conversation-id>/`) for `podo-dag.state.json`, `podo-dag.md`, and `podo-dag.html`:
     ```bash
     python3 ~/.gemini/config/plugins/podo-stuff/scripts/podo-guard.py dag init \
       --state-file "<artifact_dir>/podo-dag.state.json" \
       --md-out "<artifact_dir>/podo-dag.md" \
       --html-out "<artifact_dir>/podo-dag.html" \
       --title "<Swarm Goal>" \
       --phases-json '<phases_json>'
     ```
   - Register `<artifact_dir>/podo-dag.html` as a user-facing artifact using `write_file` (or keep it synced via `dag update` and present the artifact link) with `ArtifactMetadata: {UserFacing: true, RequestFeedback: false}` so the user can view the live DAG dashboard in the right panel.

---

## Step 2: Two-Tier Verification Cadence (Core Efficiency Rule)

Never waste Opus/LLM verification turns on every micro-task. Split verification into **two tiers**:

### Tier 1 — Per-Task Deterministic Script Gate (0 LLM Tokens)
Every `builder` (`model: <roles.builder>`, `workspace: "share"`) MUST obey these rules in its brief:
- Own **only** its assigned files; never edit files outside its slice.
- Follow `no-comments`: zero inline narration comments inside function bodies (`WHY:`, `INVARIANT:`, `SAFETY:`, docstrings, and directives only).
- Before posting readiness, run the deterministic checker + project checks in its workspace:
  ```bash
  python3 ~/.gemini/config/plugins/podo-stuff/scripts/podo-guard.py check --repo . --run-verify
  ```
  Plus the relevant unit tests / typecheck / build for its slice.
- If `podo-guard.py check` exits non-zero, the builder **fixes all violations itself in the same turn** without waking `@interrogator`.
- Only after `exit 0` and local tests pass: commit its changes in its shared workspace and post:
  `@interrogator [phase-N-ready] <task-id> summary + command outputs`

### Tier 2 — Per-DAG-Phase Batch `Interrogator` (1 Opus Run per Phase)
Add **one** `interrogator` (`model: <roles.interrogator>`, `workspace: "inherit"`) for the swarm with this exact gating rule in its brief:
- Whenever woken, call `read_board{since: 0}` and check whether **ALL** builders in the active Phase `N` have posted `[phase-N-ready]`.
- **If ANY task in Phase `N` is not yet `[phase-N-ready]`**: do **NOT** run checks and do **NOT** call `post_message` — end your turn immediately so you park in `waiting` at 0 token cost.
- **Only when ALL builders in Phase `N` have posted `[phase-N-ready]`** (batch review once per Phase):
  1. Inspect all Phase `N` shared workspace commits/diffs together for integration conflicts, edge cases, concurrency/state invariants, and missing tests.
  2. Run `podo-guard.py check --run-verify` and integration checks across the combined Phase `N` surface.
  3. If a defect or recurring mistake is found:
     - Do **not** edit builder files yourself.
     - Apply `pstack`'s **`encode-lessons-in-structure`**: specify the structural fix required — Type/API boundary tightening, a `.podo/checks.json` rule via `podo-guard.py codify --id ... --fix ...`, CI/linter rule, or a `specs/*.tla` TLA+ specification + TLC model check (or property test) for concurrency/state machines.
     - Post `@<builder> [reject] <defect, reproduction, and required structural guardrail>` so only the responsible builder fixes and re-submits.
  4. When Phase `N` passes completely:
     - Post `@coordinator [phase-N-verified] <summary of checks & structural guardrails>` (and if Phase `N+1` builders already exist and are parked waiting, Coordinator or Phase handoff wakes them; otherwise `@coordinator` merges Phase `N` into the working copy and launches Phase `N+1`).

---

## Step 3: Arena Playbook (`Orchestrator (Neon)` vs `Claude Opus 5.5 (Medium)`)

Trigger **Arena mode** when the user writes `arena:` / asks to compare approaches, or for high-risk core algorithms/concurrency bugs where competing implementations add clear value:

1. Spawn two independent contenders with `workspace: "share"` on the exact same task brief:
   - `contender-neon` (`model: <roles.arena_a>`, default `gemini-pro-orchestrator-neon`)
   - `contender-opus` (`model: <roles.arena_b>`, default `claude-opus-5-5-medium`)
2. Require **both** contenders to pass Tier 1 (`podo-guard.py check --run-verify` + tests) before posting `@interrogator [arena-ready] <test proof & diff summary>`.
3. `interrogator` (`<roles.interrogator>`) stays silent until **both** `[arena-ready]` posts arrive, then runs **one** batch evaluation comparing:
   - Real execution & edge-case resilience (including TLA+ / property tests if stateful)
   - Minimal, cleanest diff with zero narration-comment slop
   - Structural safety (types/linters/checks)
4. `interrogator` posts `@coordinator [arena-winner] <winner or graft plan>`, and Coordinator merges the winning workspace (or grafts the best parts).

---

## Step 4: Native Swarm UI + Live DAG Sync

1. Name the swarm concisely: `swarm{action: "title", title: "podo-swarm: <goal>"}`.
2. Add `monitor` (`model: <roles.monitor>`) if `swarm_monitor` is in your skill list.
3. On every Phase start, `[reject]`, and `[phase-N-verified]`, update the DAG artifacts:
   ```bash
   python3 ~/.gemini/config/plugins/podo-stuff/scripts/podo-guard.py dag update \
     --state-file "<artifact_dir>/podo-dag.state.json" \
     --node "<node_id>" --status "<status>" [--note "<note>"]
   ```
   Supported statuses: `pending`, `building`, `script_checked`, `arena_running`, `phase_interrogating`, `rejected`, `verified`.
4. Merge shared workspaces (`git merge` / `cherry-pick` from verified worker branches) into the main workspace as each Phase completes, then kill finished workers only after their work is safely landed (never kill `monitor` early).
5. Always end the creation turn (and status replies) with the exact `open_board` string returned by `swarm` on its own line, alongside a link to `[podo-dag.html](file://<artifact_dir>/podo-dag.html)`.
6. When the final Phase passes, post `[done] <summary>` (with NO `@mention`) to quiet `monitor`, run final `podo-guard.py check --run-verify` on the merged workspace, and present the summary + final DAG artifact link.
