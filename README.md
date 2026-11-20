# podo-stuff

Deterministic code guardrails, Python AST dead-code checks, and phased task-dependency DAG orchestration built as a standalone add-on plugin for Google Antigravity.

## Features

- **Modular Role-Based Routing (`/podo-setup`)**:
  - `podo-config.json` separates immutable `default_roles` from active `roles` (`scout`, `builder`, `arena_a`, `arena_b`, `interrogator`, `monitor`) with no duplicated preset blocks.
  - All skills and DAG renders read role assignments dynamically from `podo-config.json`.
  - Override any role ad hoc (`config --set <role>=<model_id>`), reset all roles back to `default_roles` (`config --reset`), or toggle the Stop hook mode (`config --mode enforce|observe|off`).
- **Two-Tier Verification Cadence (`/podo-swarm`)**:
  1. **Tier 1 — Per-Task Deterministic Gate (0 LLM tokens)**: `scripts/podo-guard.py check --repo . --base <ref>` runs after every `builder` task. Enforces `no-comments`, Python AST dead-code detection (`[DEAD-CODE]`), `.podo/checks.json` structural rules, and optional `.podo/verify.sh` checks.
  2. **Tier 2 — Per-DAG-Phase Context-Isolated Interrogator**: The `interrogator` role runs **once per DAG Phase boundary** after all parallel builders in that Phase pass Tier 1, judging strictly from task contract, git diff, and direct command proof.
- **Committed-Branch Diff Checking (`--base <ref>`)**:
  - `podo-guard.py check --repo . --base <ref>` resolves `git merge-base <ref> HEAD` (falling back to `<ref>`) and inspects committed, staged, unstaged, and untracked changes together so committed shared-workspace branches are never skipped.
  - Supports `--files <csv>` to narrow inspection to specific relative paths and `--mode enforce|observe`.
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
- **100% Offline Task DAG Dashboard (`podo-dag.html` & `podo-dag.md`)**:
  - Self-contained inline CSS using Antigravity semantic theme tokens (zero external CDN scripts), updated at Phase boundaries via batched `dag update --set-nodes`.

## Security Model (`.podo/checks.json` & `.podo/verify.sh`)

`required_command` entries in `<repo>/.podo/checks.json` (executed during `check` and the `Stop` hook) and `<repo>/.podo/verify.sh` (executed with `--run-verify`) run shell commands inside the target repository with your local user permissions.

- Treat `.podo/checks.json` and `.podo/verify.sh` like executable build scripts (`Makefile`, `package.json` scripts, git hooks).
- Audit `.podo/checks.json` and `.podo/verify.sh` before running `podo-guard.py` or enabling `enforce`/`observe` mode on third-party repositories.
- When exploring untrusted repositories, disable the Stop hook with `PODO_GUARD_MODE=off` or `python3 ~/.gemini/config/plugins/podo-stuff/scripts/podo-guard.py config --mode off`.

## Plugin Development Test Isolation

Plugin unit tests live in `tests/` solely to validate `podo-stuff` itself during plugin development (`python3 -m unittest discover -s tests -v`).

- `install.sh` explicitly excludes `tests/` and `.podo/` when installing into `~/.gemini/config/plugins/podo-stuff/`.
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

Copies runtime plugin files (including `LICENSE`) into `~/.gemini/config/plugins/podo-stuff/` while excluding `tests/` and `.podo/`.

## CLI Reference (`scripts/podo-guard.py`)

```bash
python3 scripts/podo-guard.py config --show
python3 scripts/podo-guard.py config --set builder=<model_id>
python3 scripts/podo-guard.py config --reset
python3 scripts/podo-guard.py config --mode observe
python3 scripts/podo-guard.py check --repo . --base main --run-verify
python3 scripts/podo-guard.py check --repo . --files src/app.py,src/util.py --mode observe
python3 scripts/podo-guard.py codify --repo . --id no-raw-sql --glob "*.py" --regex "execute\(f['\"]" --fix "Use parameterized queries"
python3 scripts/podo-guard.py codify --repo . --id tla-check --cmd "tlc specs/StateMachine.tla" --fix "Fix TLA+ invariant violation"
python3 scripts/podo-guard.py dag update --state-file podo-dag.state.json --set-nodes "task-1=verified,task-2=verified" --phase phase-1 --gate-status verified
```

## License

MIT — see [LICENSE](LICENSE).
