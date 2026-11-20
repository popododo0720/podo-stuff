---
name: podo-setup
description: >-
  Inspect live Antigravity model quotas and configure modular role-based model
  routing (scout, builder, arena_a, arena_b, interrogator, monitor) and Stop
  hook mode for podo-stuff. Use when the user invokes /podo-setup or asks to
  inspect, override, reset, or rebalance model assignments for /podo-swarm.
---

# Podo Setup — Live Quota, Role Routing & Gate Mode Helper

Manage `podo-config.json` dynamically using live Antigravity model quotas. Always read active `roles`, `default_roles`, and `gates` directly from `podo-config.json` rather than assuming fixed model names.

## Roles

| Role | Purpose |
| :--- | :--- |
| `scout` | Fast codebase exploration and dependency mapping |
| `builder` | Parallel implementation workers (`workspace: "share"`) |
| `arena_a` | Primary contender in Arena bake-offs |
| `arena_b` | Independent cross-pool contender in Arena bake-offs |
| `interrogator` | Per-DAG-Phase context-isolated batch verifier and structural enforcer |
| `monitor` | Low-cost swarm stall watcher |

## Procedure

1. **Fetch Live Quotas**:
   - Call `swarm{action: "models", all: true}` to inspect currently available model IDs, `quota_remaining`, and `quota_resets`.
2. **Read Active Config**:
   - Run `python3 ~/.gemini/config/plugins/podo-stuff/scripts/podo-guard.py config --show` (or `./scripts/podo-guard.py config --show` inside the plugin repository).
   - Read `roles`, `default_roles`, and `gates.stop_hook_mode` dynamically from the JSON output.
3. **Present Routing & Gate Table**:
   - Show the user a concise markdown table with each role (`scout`, `builder`, `arena_a`, `arena_b`, `interrogator`, `monitor`), its active model ID from `roles`, its default model ID from `default_roles`, and its live `quota_remaining` / reset window.
   - Display the active `gates.stop_hook_mode` (`enforce`, `observe`, or `off`) and `gates.dead_code_check` status.
4. **Apply Changes (When Requested)**:
   - Override any role ad hoc (verify the requested `<model_id>` exists in `swarm{action: "models", all: true}` first):
     ```bash
     python3 ~/.gemini/config/plugins/podo-stuff/scripts/podo-guard.py config --set <role>=<model_id>
     ```
   - Reset all active `roles` back to `default_roles`:
     ```bash
     python3 ~/.gemini/config/plugins/podo-stuff/scripts/podo-guard.py config --reset
     ```
   - Toggle the `Stop` hook mode (`enforce` blocks stop on violations, `observe` logs warnings without blocking, `off` disables the Stop hook):
     ```bash
     python3 ~/.gemini/config/plugins/podo-stuff/scripts/podo-guard.py config --mode enforce|observe|off
     ```
   - Re-run `config --show` and confirm the updated mapping cleanly.
