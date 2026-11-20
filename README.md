# podo-stuff

My personal agent harness for Google Antigravity.

Auto-routes prompts to the right workflow, orchestrates parallel swarms, and blocks sloppy code before it lands.

## Install

```bash
agy plugin install https://github.com/popododo0720/podo-stuff
```

Or from a local checkout:

```bash
git clone https://github.com/popododo0720/podo-stuff
cd podo-stuff && ./install.sh
```

## What's Inside

- **Autopilot Router (`PreInvocation`)** — routes each turn via `gemini-decision-1` into `direct`, `swarm`, `plan`, `grill`, `research`, or `clarify`, with per-turn dedup, complexity/ambiguity gating, session mode stickiness, optional `egm` Goldfish plan audits, and a 400K context-rot / 3x error-loop guard.
- **Phased Swarm (`/podo-swarm`)** — breaks multi-file tasks into disjoint parallel DAG phases on top of native Antigravity `/swarm`, with optional dual-model Arena bake-offs, one context-isolated `interrogator` pass per phase, and 2x `[reject]` dynamic model escalation / adaptive replanning.
- **Code Gate (`Stop`)** — catches inline narration comments, Python AST dead code, and opt-in hash-trusted `.podo/checks.json` rules scoped strictly to the files edited in that turn.
- **Offline Observability & Rule Evolution** — local JSONL telemetry, offline Gantt DAG dashboard (`podo-dag.html`), role-based token estimates, and `evolve` rule suggestions with no external services.

## Usage

In Antigravity chat:

```text
/podo-setup               # inspect live quotas and configure role models / gates
/podo-swarm <task>        # run phased parallel DAG swarm explicitly
/podo-swarm arena: <task> # pit arena_a vs arena_b on the same task
```

Or just chat normally — the Autopilot Router activates `swarm`, `plan`, `grill`, `research`, or `clarify` automatically when needed and stays silent on normal turns.

## CLI & Configuration

Bundled defaults live in [`podo-config.json`](podo-config.json); `config` updates persist outside the install directory in `$PLUGIN_DATA/podo-config.json` (or `~/.config/podo-stuff/podo-config.json`) so reinstalls never wipe your overrides.

```bash
python3 scripts/podo-guard.py config --show
python3 scripts/podo-guard.py config --set builder=<model_id>
python3 scripts/podo-guard.py config --mode enforce|observe|off
python3 scripts/podo-guard.py config --autopilot on|off
python3 scripts/podo-guard.py codify --repo . --id <rule-id> --regex "<pattern>" --fix "<message>"
python3 scripts/podo-guard.py trust --repo .
python3 scripts/podo-guard.py evolve --repo .
```

Session-level overrides:

```bash
PODO_AUTOPILOT=off   # disable PreInvocation routing for this session
PODO_GUARD_MODE=off  # disable Stop hook (useful on untrusted third-party repos)
```

## Development

```bash
python3 -m unittest discover -s tests -v   # unit tests
python3 scripts/eval-autopilot.py          # live golden router eval
```

## License

MIT — see [LICENSE](LICENSE).
