---
name: podo-setup
description: >-
  Inspect live Antigravity model quotas and configure modular role-based model
  routing (scout, builder, arena_a, arena_b, interrogator, monitor), Stop hook
  mode, and telemetry token/anomaly settings for podo-stuff. Use when the user
  invokes /podo-setup or asks to inspect, override, reset, or tune settings for
  /podo-swarm.
---

# Podo Setup — Live Quota, Role Routing, Gate Mode & Telemetry Helper

Manage `podo-config.json` dynamically using live Antigravity model quotas. Always read active `roles`, `default_roles`, `gates`, and `telemetry` directly from `podo-config.json` rather than assuming fixed model names.

## Roles

| Role | Purpose |
| :--- | :--- |
| `scout` | Fast codebase exploration and dependency mapping |
| `builder` | Parallel implementation workers (`workspace: "share"`) |
| `arena_a` | Primary contender in Arena bake-offs |
| `arena_b` | Independent cross-pool contender in Arena bake-offs |
| `interrogator` | Per-DAG-Phase context-isolated batch verifier and structural enforcer |
| `monitor` | Low-cost swarm stall watcher |

## Telemetry & Anomaly Configuration

`podo-config.json` also stores observability defaults under `"telemetry"`:

- `telemetry.enabled`: Toggle structured JSONL telemetry emission and snapshot estimation (`true` / `false`).
- `telemetry.role_avg_tokens`: Per-role average token baselines (`scout`, `builder`, `arena_a`, `arena_b`, `interrogator`, `monitor`) used by `telemetry report`, `telemetry snapshot`, and the `podo-dag.html` Observability panel to estimate role spend and Tier-1 saved tokens.
- `telemetry.anomaly_thresholds`:
  - `interrogator_calls_per_phase_warn`: Triggers `HIGH_INTERROGATOR_CHURN` when `interrogator_calls / max(1, phases)` meets or exceeds this threshold.
  - `gate_fail_rate_warn`: Triggers `HIGH_GATE_FAIL_RATE` when the deterministic gate failure rate meets or exceeds this threshold.
  - `interrogator_history_multiplier_warn`: Triggers `INTERROGATOR_SPIKE_VS_HISTORY` when current session `interrogator_calls` meets or exceeds `multiplier * historical_avg` across prior `.podo/snapshots/*.json`.

## Autopilot Router (`PreInvocation` + `gemini-decision-1`)

`podo-config.json` includes `"autopilot"` settings for automatic intent routing via the Gemini Decisions API (`gemini-decision-1`, 0 output tokens):

- `autopilot.enabled`: Toggle the `PreInvocation` router (`true` / `false`, or `PODO_AUTOPILOT=off` per session).
- `autopilot.model`: Decisions model ID (`gemini-decision-1` by default).
- `autopilot.confidence_threshold`: Minimum confidence (`0.65`) required before injecting `swarm`, `plan`, or `grill` mode (`direct` passes through silently).
- `autopilot.arena_threshold`: Minimum `needs_arena` probability (`0.70`) to recommend dual-model Arena competition during auto-swarm.
- `autopilot.show_banner`: Display a concise one-line inline markdown banner (`metadata.user_markdown`) when auto-routing activates.
- Credentials resolve automatically from `GEMINI_API_KEY`, `GOOGLE_API_KEY`, or `~/.config/gemini-api/config.yaml` (fails open silently if unset).

## Procedure

1. **Fetch Live Quotas**:
   - Call `swarm{action: "models", all: true}` to inspect currently available model IDs, `quota_remaining`, and `quota_resets`.
2. **Read Active Config**:
   - Run `python3 ~/.gemini/config/plugins/podo-stuff/scripts/podo-guard.py config --show` (or `./scripts/podo-guard.py config --show` inside the plugin repository).
   - Read `roles`, `default_roles`, `gates.stop_hook_mode`, and `telemetry` (`role_avg_tokens`, `anomaly_thresholds`) dynamically from the JSON output.
3. **Present Routing, Gate & Telemetry Summary**:
   - Show the user a concise markdown table with each role (`scout`, `builder`, `arena_a`, `arena_b`, `interrogator`, `monitor`), its active model ID from `roles`, its default model ID from `default_roles`, its `telemetry.role_avg_tokens` baseline, and its live `quota_remaining` / reset window.
   - Display `gates.stop_hook_mode` (`enforce`, `observe`, or `off`), `gates.dead_code_check`, `telemetry.enabled`, and `telemetry.anomaly_thresholds`.
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
   - Toggle the `PreInvocation` Autopilot Router (`on` or `off`):
     ```bash
     python3 ~/.gemini/config/plugins/podo-stuff/scripts/podo-guard.py config --autopilot on|off
     ```
   - Tune `telemetry.role_avg_tokens` or `telemetry.anomaly_thresholds` directly in `podo-config.json` when calibrating baselines to a project's typical task size or stricter/looser churn alerts.
   - Re-run `config --show` and confirm the updated configuration cleanly.
