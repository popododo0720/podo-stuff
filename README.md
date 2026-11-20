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

- **Autopilot Router (`PreInvocation`)** — routes each turn via `gemini-decision-1` into `direct`, `swarm`, `plan`, `grill`, or `clarify`, with per-turn dedup, complexity/ambiguity gating, session mode stickiness, and optional `egm` Goldfish plan audits.
- **Phased Swarm (`/podo-swarm`)** — breaks multi-file tasks into disjoint parallel DAG phases on top of native Antigravity `/swarm`, with optional dual-model Arena bake-offs and one context-isolated `interrogator` pass per phase.
- **Code Gate (`Stop`)** — catches inline narration comments, Python AST dead code, and repo-level `.podo/checks.json` rules scoped strictly to the files edited in that turn.
- **Offline Observability** — local JSONL telemetry, offline Gantt DAG dashboard (`podo-dag.html`), and role-based token estimates with zero external services.

## Usage

In Antigravity chat:

```text
/podo-setup               # inspect live quotas and configure role models / gates
/podo-swarm <task>        # run phased parallel DAG swarm explicitly
/podo-swarm arena: <task> # pit arena_a vs arena_b on the same task
```

Or just chat normally — the Autopilot Router activates `swarm`, `plan`, `grill`, or `clarify` automatically when needed and stays silent on normal turns.

## CLI & Configuration

All defaults live in [`podo-config.json`](podo-config.json).

```bash
python3 scripts/podo-guard.py config --show
python3 scripts/podo-guard.py config --set builder=<model_id>
python3 scripts/podo-guard.py config --mode enforce|observe|off
python3 scripts/podo-guard.py config --autopilot on|off
python3 scripts/podo-guard.py codify --repo . --id <rule-id> --regex "<pattern>" --fix "<message>"
```

Session-level overrides:

```bash
PODO_AUTOPILOT=off   # disable PreInvocation routing for this session
PODO_GUARD_MODE=off  # disable Stop hook (useful on untrusted third-party repos)
```

## Development

```bash
python3 -m unittest discover -s tests -v   # unit tests
python3 scripts/eval-autopilot.py          # live golden router eval (24 cases)
```

## License

MIT — see [LICENSE](LICENSE).
