---
name: podo-setup
description: >-
  Inspect live Antigravity model quotas and configure role-based model routing
  presets (scout, builder, arena_a, arena_b, interrogator, monitor) for the
  podo-stuff plugin. Use when the user invokes /podo-setup or asks to inspect,
  rebalance, or change model assignments for /podo-swarm.
---

# Podo Setup — Live Quota & Role Routing Helper

Manage `podo-config.json` role-to-model routing based on real-time Antigravity quota pools (`Neon/Hydrogen`, `Claude`, `Argon`).

## Default Role Mapping (`podo-default`)

| Role | Default Model ID | Display Name | Purpose |
| :--- | :--- | :--- | :--- |
| `scout` | `gemini-4-hydrogen-medium` | Gemini 4 Hydrogen (Medium) | Fast `/how` & `/why` codebase investigation |
| `builder` | `gemini-pro-orchestrator-neon` | Orchestrator (Neon) | Main parallel dev workers (`workspace: "share"`) |
| `arena_a` | `gemini-pro-orchestrator-neon` | Orchestrator (Neon) | Arena Contender A |
| `arena_b` | `claude-opus-5-5-medium` | Claude Opus 5.5 (Medium) | Arena Contender B (cross-pool) |
| `interrogator` | `claude-opus-5-5-medium` | Claude Opus 5.5 (Medium) | Per-DAG-Phase batch verifier & structural enforcer |
| `monitor` | `gemini-4-hydrogen-low` | Gemini 4 Hydrogen (Low) | Low-cost swarm stall watcher |

## Procedure

1. **Fetch Live Quotas**:
   - Call `swarm{action: "models", all: true}` to inspect currently available models and `quota_remaining` / `quota_resets`.
2. **Read Active Config**:
   - Run `python3 ~/.gemini/config/plugins/podo-stuff/scripts/podo-guard.py config --show` (or `./scripts/podo-guard.py` if run from inside the plugin repository).
3. **Present Table**:
   - Show the user a concise markdown table mapping each role (`scout`, `builder`, `arena_a`, `arena_b`, `interrogator`, `monitor`) to its configured model and live `quota_remaining`.
   - Note that the default preset is `podo-default` and any individual role can be overridden ad hoc when desired.
4. **Apply Changes (When Requested)**:
   - Individual role override (ad hoc):
     ```bash
     python3 ~/.gemini/config/plugins/podo-stuff/scripts/podo-guard.py config --set <role>=<model_id>
     ```
   - Reset to default preset (`podo-default`):
     ```bash
     python3 ~/.gemini/config/plugins/podo-stuff/scripts/podo-guard.py config --preset podo-default
     ```
   - Verify any user-specified model ID exists in the `swarm{action: "models", all: true}` output before saving.
   - Re-run `config --show` and confirm the updated mapping cleanly.
