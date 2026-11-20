# podo-stuff

Deterministic code guardrails, Python AST dead-code checks, phased task-dependency DAG orchestration, and native observability built as a standalone add-on plugin for Google Antigravity.

## Features

- **Modular Role-Based Routing (`/podo-setup`)**:
  - `podo-config.json` separates immutable `default_roles` from active `roles` (`scout`, `builder`, `arena_a`, `arena_b`, `interrogator`, `monitor`) with no duplicated preset blocks.
  - All skills and DAG renders read role assignments dynamically from `podo-config.json`.
  - Override any role ad hoc (`config --set <role>=<model_id>`), reset all roles back to `default_roles` (`config --reset`), or toggle the Stop hook mode (`config --mode enforce|observe|off`).
- **Two-Tier Verification Cadence (`/podo-swarm`)**:
  1. **Tier 1 — Per-Task Deterministic Gate (0 LLM tokens)**: `scripts/podo-guard.py check --repo . --base <ref>` runs after every `builder` task. Enforces `no-comments`, Python AST dead-code detection (`[DEAD-CODE]`), `.podo/checks.json` structural rules, and optional `.podo/verify.sh` checks.
  2. **Tier 2 — Per-DAG-Phase Context-Isolated Interrogator**: The `interrogator` role runs **once per DAG Phase boundary** after all parallel builders in that Phase pass Tier 1, judging strictly from task contract, git diff, and direct command proof.
- **Committed-Branch Diff Checking (`--base <ref>`)**:
  - `podo-guard.py check --repo . --base <ref>` validates `<ref>`, resolves `git merge-base <ref> HEAD`, and inspects committed, staged, unstaged, and untracked changes together so committed shared-workspace branches are never skipped.
  - Supports `--files <csv>`, `--mode enforce|observe`, and `--phase` / `--worker` / `--role` telemetry tags.
- **Deterministic Python AST Dead-Code Gate (`[DEAD-CODE]`)**:
  - Enabled by `gates.dead_code_check: true`. Scans added/modified Python files via `ast` to flag:
    - Unreachable statements immediately following `return`, `raise`, `break`, or `continue`.
    - Unused imports on added lines (exempts `__init__.py`, names exported in `__all__`, `TYPE_CHECKING` blocks, and `# noqa` lines).
    - Unused private helper functions (`def _name`, excluding `__*__` dunders).
- **Turn-Scoped `Stop` Hook & `observe` Mode**:
  - Reads Antigravity's `transcriptPath` payload to inspect actions since the last `USER_INPUT` step:
    - Read-only turns (no `edit_file`, `write_file`, or `run_command`) exit immediately with `{}`.
    - Edit-only turns (no `run_command`) scope checks strictly to the file paths edited during that turn.
    - Non-builder roles (`PODO_ROLE` in `scout`, `interrogator`, `monitor`, `coordinator`) exit immediately with `{}`.
  - Enforces a bounded deadline budget across `required_command` checks so hook execution stays under the 15s `hooks.json` timeout.
  - Mode precedence: `PODO_GUARD_MODE` environment variable overrides `gates.stop_hook_mode` in `podo-config.json`:
    - `enforce`: blocks stop with `{"decision": "continue", "reason": ...}` on violations.
    - `observe`: logs violations to `stderr` and `<repo>/.podo/last-observe.log` (when `.podo/` exists) and returns `{}` without blocking.
    - `off`: skips Stop hook checks immediately (`{}`).
- **Persistent Structural Checks (`podo-guard.py codify`)**:
  - Converts recurring failures into permanent deterministic rules in `<repo>/.podo/checks.json` (`forbidden_regex` or `required_command` such as linters, typecheckers, or `specs/*.tla` TLC model checks).

## 5-Pillar Antigravity-Native Observability

`podo-stuff` instruments gate runs, DAG phase transitions, and native Antigravity transcripts without external telemetry services or CDN dependencies:

1. **Structured JSONL Telemetry Emitter**:
   - `check`, `--hook-stop`, `dag init`, `dag update`, and `codify` emit structured JSONL records (`ts`, `iso_ts`, `event`, `conversation_id`, `phase`, `worker`, `role`, `model`, `gate`, `result`, `violation_count`, `files`, `duration_ms`).
   - Writes to `--telemetry-file` / `PODO_TELEMETRY_FILE`, `<state_file_dir>/podo-telemetry.jsonl` (for `dag`), or `<repo>/.podo/telemetry.jsonl` **only when `<repo>/.podo/` already exists** (plain `check` never auto-creates `.podo/` in clean repositories).
   - Tag check runs via `--phase`, `--worker`, `--role` (falling back to `PODO_PHASE`, `PODO_WORKER`, `PODO_ROLE`).
2. **DAG Phase/Node Timestamps & Offline Gantt Timeline View**:
   - `podo-dag.state.json` records `started_at`, `finished_at`, `duration_s`, and `interrogator_calls` per phase and node (auto-populated on status transitions, with optional `--started-at` and `--finished-at` on `dag update`).
   - `podo-dag.html` renders a 100% offline **Phase & Worker Timeline (Gantt View)** with relative timing bars and an **Observability & Token Estimate** panel using inline CSS and Antigravity semantic theme tokens (zero external scripts).
3. **Stop-Hook & CLI Session Snapshots**:
   - `podo-guard.py telemetry snapshot` and the `Stop` hook automatically persist session snapshots to `<repo>/.podo/snapshots/<conversationId>.json` (when `.podo/` exists) or `<artifactDir>/podo-session-snapshot.json` (when `artifactDirectoryPath` is present).
   - Captures `dag_summary` (phases, durations, node counts, `last_interrogator_verdict`), `gate_metrics` (pass/fail/observe counts, failure rate, breakdown by `gate`, `phase`, `worker`), `token_estimates`, and `trace_link`.
4. **Role-Based Token Estimation & Anomaly Detection**:
   - Estimates per-role and total token usage (`calls × telemetry.role_avg_tokens`) and Tier-1 saved tokens using baselines in `podo-config.json`.
   - Raises actionable anomaly warnings against `telemetry.anomaly_thresholds`:
     - `HIGH_INTERROGATOR_CHURN`: `interrogator_calls / max(1, phases) >= interrogator_calls_per_phase_warn`.
     - `HIGH_GATE_FAIL_RATE`: `gate_fail_rate >= gate_fail_rate_warn`.
     - `INTERROGATOR_SPIKE_VS_HISTORY`: current `interrogator_calls >= interrogator_history_multiplier_warn * historical_avg` across prior `<repo>/.podo/snapshots/*.json`.
5. **Antigravity Native Trace Joining (`trace-index.json`)**:
   - Joins `conversationId`, `transcriptPath`, and `artifactDirectoryPath` with `telemetry_file`, `snapshot_file`, and `dag_state_file`.
   - Safely parses `transcriptPath` when present on disk to record `transcript_steps_count`, `user_turns_count`, and `tool_call_counts` in both the session snapshot and `<repo>/.podo/trace-index.json`.

## Security Model (`.podo/checks.json` & `.podo/verify.sh`)

`required_command` entries in `<repo>/.podo/checks.json` (executed during `check` and the `Stop` hook) and `<repo>/.podo/verify.sh` (executed with `--run-verify`) run shell commands inside the target repository with your local user permissions.

- Treat `.podo/checks.json` and `.podo/verify.sh` like executable build scripts (`Makefile`, `package.json` scripts, git hooks).
- Audit `.podo/checks.json` and `.podo/verify.sh` before running `podo-guard.py` or enabling `enforce`/`observe` mode on third-party repositories.
- When exploring untrusted repositories, disable the Stop hook with `PODO_GUARD_MODE=off` or `python3 ~/.gemini/config/plugins/podo-stuff/scripts/podo-guard.py config --mode off`.

## Plugin Development Test Isolation

Plugin unit tests live in `tests/` solely to validate `podo-stuff` itself during plugin development (`python3 -m unittest discover -s tests -v`).

- `install.sh` explicitly excludes `tests/`, `.podo/`, and `scripts/__pycache__` when installing into `~/.gemini/config/plugins/podo-stuff/`.
- Target projects always define and run their own test suites, linters, and optional `.podo/checks.json` rules independently.

## Installation

### Via Antigravity CLI (Recommended)

```bash
agy plugin install https://github.com/popododo0720/podo-stuff
```

### Via Git Clone

```bash
git clone https://github.com/popododo0720/podo-stuff ~/.gemini/config/plugins/podo-stuff
```

### From Local Checkout

```bash
./install.sh
```

Copies runtime plugin files (including `LICENSE`) into `~/.gemini/config/plugins/podo-stuff/` while excluding `tests/`, `.podo/`, and `scripts/__pycache__`.

## CLI Reference (`scripts/podo-guard.py`)

```bash
python3 scripts/podo-guard.py config --show
python3 scripts/podo-guard.py config --set builder=<model_id>
python3 scripts/podo-guard.py config --reset
python3 scripts/podo-guard.py config --mode observe
python3 scripts/podo-guard.py check --repo . --base main --phase phase-1 --worker builder-1 --role builder --run-verify
python3 scripts/podo-guard.py check --repo . --files src/app.py,src/util.py --mode observe
python3 scripts/podo-guard.py codify --repo . --id no-raw-sql --glob "*.py" --regex "execute\(f['\"]" --fix "Use parameterized queries"
python3 scripts/podo-guard.py codify --repo . --id tla-check --cmd "tlc specs/StateMachine.tla" --fix "Fix TLA+ invariant violation"
python3 scripts/podo-guard.py dag update --state-file podo-dag.state.json --set-nodes "task-1=verified,task-2=verified" --phase phase-1 --gate-status verified
python3 scripts/podo-guard.py telemetry report --repo . --state-file podo-dag.state.json
python3 scripts/podo-guard.py telemetry report --repo . --state-file podo-dag.state.json --json
python3 scripts/podo-guard.py telemetry snapshot --repo . --state-file podo-dag.state.json --conversation-id <id> --transcript-path <path/to/transcript.jsonl> --artifact-dir <artifact_dir>
```

## License

MIT — see [LICENSE](LICENSE).
