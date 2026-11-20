# podo-stuff

`pstack` structural discipline + `OmO` task-dependency DAG orchestration built as a zero-core-modification add-on plugin for Google Antigravity (`popododo`).

## Features

- **Role-Based Model Routing (`/podo-setup`)**:
  - `scout`: `gemini-4-hydrogen-medium` (Gemini 4 Hydrogen Medium)
  - `builder`: `gemini-pro-orchestrator-neon` (Orchestrator Neon)
  - `arena_a` vs `arena_b`: `gemini-pro-orchestrator-neon` vs `claude-opus-5-5-medium`
  - `interrogator`: `claude-opus-5-5-medium` (Claude Opus 5.5 Medium)
  - `monitor`: `gemini-4-hydrogen-low` (Gemini 4 Hydrogen Low)
- **Two-Tier Verification Cadence (`/podo-swarm`)**:
  1. **Tier 1 — Per-Task Deterministic Gate (0 LLM tokens)**: `scripts/podo-guard.py check` + `Stop` hook runs after every `Orchestrator (Neon)` task. Blocks inline narration comments inside function bodies (`no-comments`), runs `.podo/checks.json` structural rules, and runs `.podo/verify.sh` / CI / tests.
  2. **Tier 2 — Per-DAG-Phase Batch Interrogator**: `Claude Opus 5.5 (Medium)` runs **once per DAG Phase (Wave) boundary** after all parallel builders in that Phase pass Tier 1.
- **Encode Lessons in Structure (`podo-guard.py codify`)**:
  - Converts recurring failures into permanent deterministic rules in `.podo/checks.json` (regex checks or required commands such as linters, typecheckers, or `specs/*.tla` TLC model checks).
- **Live Task DAG Dashboard (`podo-dag.html` & `podo-dag.md`)**:
  - Tracks Phase-by-Phase parallel execution, worker/model assignments, per-task script gates, and per-phase Opus Interrogator status.

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

Installs the plugin globally into `~/.gemini/config/plugins/podo-stuff/`.

## CLI Reference (`scripts/podo-guard.py`)

```bash
python3 scripts/podo-guard.py config --show
python3 scripts/podo-guard.py config --preset quota-saver-argon
python3 scripts/podo-guard.py check --repo . --run-verify
python3 scripts/podo-guard.py codify --repo . --id no-raw-sql --glob "*.py" --regex "execute\(f['\"]" --fix "Use parameterized queries"
python3 scripts/podo-guard.py codify --repo . --id tla-check --cmd "tlc specs/StateMachine.tla" --fix "Fix TLA+ invariant violation"
```
