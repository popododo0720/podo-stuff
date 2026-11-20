---
name: podo-setup
description: >-
  Inspect live Antigravity model quotas and configure modular role-based model
  routing (scout, builder, arena_a, arena_b, interrogator, monitor), Stop hook
  mode, 2x reject escalation, Autopilot Router guards, checks.json hash trust,
  rule evolution, and telemetry settings for podo-stuff. Use when the user
  invokes /podo-setup or asks to inspect, override, reset, or tune settings for
  /podo-swarm.
---

# Podo Setup — Live Quota, Role Routing, Gate Mode & Telemetry Helper

Manage effective `podo-stuff` configuration dynamically using live Antigravity model quotas. Bundled defaults live in `podo-config.json` (`default_roles`, `gates`, `telemetry`, `autopilot`), while user overrides persist outside the plugin install directory at `$PLUGIN_DATA/podo-config.json` (or `~/.config/podo-stuff/podo-config.json` when `PLUGIN_DATA` is unset) so reinstalls never wipe custom settings. Always read active `roles`, `default_roles`, `gates`, `telemetry`, and `autopilot` via `config --show` rather than assuming fixed model names.

## Roles

| Role | Purpose |
| :--- | :--- |
| `scout` | Fast codebase exploration and dependency mapping |
| `builder` | Parallel implementation workers (`workspace: "share"`) |
| `arena_a` | Primary contender in Arena bake-offs |
| `arena_b` | Independent cross-pool contender in Arena bake-offs |
| `interrogator` | Per-DAG-Phase context-isolated batch verifier and structural enforcer |
| `monitor` | Low-cost swarm stall watcher |

## Gates, Escalation, Trust & Rule Evolution

- `gates.stop_hook_mode`: `enforce` (blocks Stop on violations), `observe` (logs warnings without blocking), or `off`.
- `gates.max_rejects_before_escalation` (`2`) & `gates.escalation_role` (`"interrogator"`): After 2 consecutive `[reject]` cycles on the same task, `/podo-swarm` escalates the stuck worker in-place via `swarm{action: "model", name: "<builder>", model: "<roles[gates.escalation_role]>"}` or adaptively replans (Arena mode / DAG node split).
- **Opt-In `.podo/checks.json` Trust (`trust`)**: `required_command` shell hooks in `.podo/checks.json` are skipped unless that file's SHA-256 hash is approved in `$PLUGIN_DATA/trusted-checks.json` (or `~/.config/podo-stuff/trusted-checks.json`) via `podo-guard.py trust --repo <repo>` (or `PODO_TRUST_CHECKS=1`). Rules created with `podo-guard.py codify` are trusted automatically.
- **Closed-Loop Rule Evolution (`evolve`)**: `podo-guard.py evolve --repo <repo>` analyzes historical gate-failure telemetry (`--min-count`, `--stale-days`, `--json`) to suggest recurring-defect rules to `codify` and flag stale checks without creating `.podo/`.

## Telemetry & Anomaly Configuration

`podo-config.json` stores observability defaults under `"telemetry"`:

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
- `autopilot.confidence_threshold`: Minimum confidence (`0.65`) required before injecting `swarm`, `plan`, `grill`, or `research` mode (`direct` passes through silently; `research` activates deep parallel web/codebase investigation without requiring `complexity_min_for_swarm`).
- `autopilot.arena_threshold`: Minimum `needs_arena` probability (`0.70`) to recommend dual-model Arena competition during auto-swarm.
- `autopilot.context_warn_tokens`: Session context threshold (`400000` / 400K tokens) that triggers a Context Rot warning recommending `/compact`.
- `autopilot.error_loop_threshold`: Trailing consecutive `ERROR` tool-step threshold (`3`) that triggers an error-loop warning to stop thrashing and switch strategy.
- `autopilot.show_banner`: Instruct the assistant to display a concise one-line status banner at the top of its reply when auto-routing activates.
- Credentials resolve automatically from `GEMINI_API_KEY`, `GOOGLE_API_KEY`, or `~/.config/gemini-api/config.yaml` (fails open silently if unset).

## Procedure

1. **Fetch Live Quotas**:
   - Call `swarm{action: "models", all: true}` to inspect currently available model IDs, `quota_remaining`, and `quota_resets`.
2. **Resolve `podo-guard.py` Portably & Read Active Config**:
   - Resolve `PODO_GUARD` without hardcoding a single install path and inspect effective settings (bundled defaults merged with `$PLUGIN_DATA/podo-config.json` or `~/.config/podo-stuff/podo-config.json`):
     ```bash
     PODO_GUARD=""
     for candidate in \
       "${PLUGIN_ROOT}/scripts/podo-guard.py" \
       ~/.gemini/config/plugins/podo-stuff/scripts/podo-guard.py \
       ~/.gemini/antigravity-cli/plugins/podo-stuff/scripts/podo-guard.py \
       ./scripts/podo-guard.py; do
       if [ -f "$candidate" ]; then PODO_GUARD="$candidate"; break; fi
     done
     python3 "$PODO_GUARD" config --show
     ```
   - Read `roles`, `default_roles`, `gates` (`stop_hook_mode`, `dead_code_check`, `max_rejects_before_escalation`, `escalation_role`), `autopilot` (`enabled`, `context_warn_tokens`, `error_loop_threshold`), and `telemetry` (`role_avg_tokens`, `anomaly_thresholds`) dynamically from the JSON output.
3. **Present Routing, Gate & Telemetry Summary**:
   - Show the user a concise markdown table with each role (`scout`, `builder`, `arena_a`, `arena_b`, `interrogator`, `monitor`), its active model ID from `roles`, its default model ID from `default_roles`, its `telemetry.role_avg_tokens` baseline, and its live `quota_remaining` / reset window.
   - Display `gates.stop_hook_mode` (`enforce`, `observe`, or `off`), `gates.dead_code_check`, `gates.max_rejects_before_escalation` / `gates.escalation_role`, `autopilot.context_warn_tokens`, `autopilot.error_loop_threshold`, `telemetry.enabled`, and `telemetry.anomaly_thresholds`.
4. **Apply Changes (When Requested)**:
   - Override any role ad hoc (writes to persistent user config in `$PLUGIN_DATA/podo-config.json` or `~/.config/podo-stuff/podo-config.json`; verify `<model_id>` exists in `swarm{action: "models", all: true}` first):
     ```bash
     python3 "$PODO_GUARD" config --set <role>=<model_id>
     ```
   - Reset all active `roles` back to `default_roles`:
     ```bash
     python3 "$PODO_GUARD" config --reset
     ```
   - Toggle the `Stop` hook mode (`enforce`, `observe`, or `off`):
     ```bash
     python3 "$PODO_GUARD" config --mode enforce|observe|off
     ```
   - Toggle the `PreInvocation` Autopilot Router (`on` or `off`):
     ```bash
     python3 "$PODO_GUARD" config --autopilot on|off
     ```
   - Approve the current SHA-256 hash of a repository's `.podo/checks.json` so its `required_command` checks can run:
     ```bash
     python3 "$PODO_GUARD" trust --repo .
     ```
   - Analyze recurring gate failures and stale rules for closed-loop rule evolution:
     ```bash
     python3 "$PODO_GUARD" evolve --repo .
     ```
   - Tune `gates`, `autopilot`, or `telemetry` thresholds in the persistent user config (`$PLUGIN_DATA/podo-config.json` or `~/.config/podo-stuff/podo-config.json`, or pass `--config <path>` to target a specific config file).
   - Re-run `python3 "$PODO_GUARD" config --show` and confirm the updated configuration cleanly.
