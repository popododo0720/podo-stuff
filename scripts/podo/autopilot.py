#!/usr/bin/env python3
"""Lightweight PreInvocation Autopilot Router using Gemini Decisions API (gemini-decision-1)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_CONFIG_PATH = PLUGIN_ROOT / "podo-config.json"

DEFAULT_AUTOPILOT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "model": "gemini-decision-1",
    "api_base_url": "https://generativelanguage.googleapis.com/v1beta/decisions",
    "confidence_threshold": 0.65,
    "arena_threshold": 0.70,
    "complexity_min_for_swarm": 2.2,
    "clarification_threshold": 0.90,
    "boundary_plan_threshold": 0.75,
    "egm_plan_review": True,
    "show_banner": True,
    "timeout_s": 5,
}

AUTOPILOT_SKIP_ROLES = {"builder", "scout", "interrogator", "monitor", "arena_a", "arena_b"}

AUTOPILOT_DECISION_QUESTIONS: dict[str, Any] = {
    "continues_previous_task": {
        "type": "noul",
        "instructions": "Does Current User Request continue, approve, or refine the task/plan discussed in Recent Conversation (as opposed to starting a brand-new unrelated question or topic)?",
    },
    "mode": {
        "type": "choice",
        "instructions": "First decide whether Current User Request continues/approves Recent Conversation or starts a new unrelated topic. If it is a new topic, ignore Recent Conversation and judge Current User Request alone. If it approves or continues a proposed plan/task (e.g., 'ㅇㅋ ㄱㄱ', 'go ahead'), judge mode based on the full agreed scope in Recent Conversation.",
        "criteria": {
            "direct": "Normal conversation, conceptual question, explanation, quick lookup, status check, or small single-file edit.",
            "swarm": "Multi-file implementation, new feature build, broad refactoring, or autonomous end-to-end coding task (including approving a previously discussed multi-item plan to start building) that benefits from parallel podo-swarm execution.",
            "plan": "Architecture design, system trade-off analysis, or step-by-step planning requested before writing code.",
            "grill": "Adversarial code review, security or bug audit, or critical evaluation challenging flaws in code or design.",
        },
    },
    "needs_arena": {
        "type": "noul",
        "instructions": "Is this task high-risk or architecture-critical enough to warrant dual-model Arena competition?",
    },
    "complexity": {
        "type": "score",
        "instructions": "Estimate the engineering scope and parallelism benefit. If Current User Request is a new unrelated topic, score Current User Request alone. If it approves or continues a plan/task from Recent Conversation, score the full agreed implementation scope.",
        "criteria": [
            "Trivial conversation, question, or lookup with no code changes",
            "Small localized edit in a single file",
            "Moderate bugfix or change across 2-3 tightly coupled files",
            "Multi-module feature or refactor across 4+ files suited for parallel execution",
            "Large architectural overhaul, distributed system, or high-risk concurrency redesign",
        ],
    },
    "needs_clarification": {
        "type": "noul",
        "instructions": "Is the user request completely vague with no concrete target modules, deliverables, or approved plan (as opposed to naming specific components, technologies, or approving a prior plan)?",
    },
}

API_KEY_YAML_RE = re.compile(r"^\s*api_key\s*:\s*['\"]?([^'\"#\s]+)['\"]?\s*$")
SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")
USER_REQUEST_TAG_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)


def utc_now_iso() -> str:
    """Return the current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sanitize_identifier(raw: str) -> str:
    """Sanitize a conversation or session identifier for safe filename usage."""
    cleaned = SAFE_ID_RE.sub("_", raw.strip()).strip("._")
    return cleaned[:96] or "session"


def load_json_dict(path: Path | None) -> dict[str, Any]:
    """Load a JSON object from disk, returning an empty dict on failure."""
    if path is None or not path.is_file():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def is_telemetry_enabled(cfg: dict[str, Any]) -> bool:
    """Return whether JSONL telemetry emission is enabled in configuration."""
    telemetry = cfg.get("telemetry", {})
    if isinstance(telemetry, dict) and "enabled" in telemetry:
        return bool(telemetry["enabled"])
    return True


def get_autopilot_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Return autopilot router settings merged with defaults."""
    merged = dict(DEFAULT_AUTOPILOT_CONFIG)
    raw = cfg.get("autopilot")
    if isinstance(raw, dict):
        if "enabled" in raw:
            merged["enabled"] = bool(raw["enabled"])
        if isinstance(raw.get("model"), str) and raw["model"].strip():
            merged["model"] = raw["model"].strip()
        if isinstance(raw.get("api_base_url"), str) and raw["api_base_url"].strip():
            merged["api_base_url"] = raw["api_base_url"].strip()
        if isinstance(raw.get("confidence_threshold"), (int, float)):
            merged["confidence_threshold"] = max(0.0, min(1.0, float(raw["confidence_threshold"])))
        if isinstance(raw.get("arena_threshold"), (int, float)):
            merged["arena_threshold"] = max(0.0, min(1.0, float(raw["arena_threshold"])))
        if isinstance(raw.get("complexity_min_for_swarm"), (int, float)):
            merged["complexity_min_for_swarm"] = max(0.0, min(4.0, float(raw["complexity_min_for_swarm"])))
        if isinstance(raw.get("clarification_threshold"), (int, float)):
            merged["clarification_threshold"] = max(0.0, min(1.0, float(raw["clarification_threshold"])))
        if isinstance(raw.get("boundary_plan_threshold"), (int, float)):
            merged["boundary_plan_threshold"] = max(0.0, min(1.0, float(raw["boundary_plan_threshold"])))
        if "egm_plan_review" in raw:
            merged["egm_plan_review"] = bool(raw["egm_plan_review"])
        if "show_banner" in raw:
            merged["show_banner"] = bool(raw["show_banner"])
        if isinstance(raw.get("timeout_s"), (int, float)) and raw["timeout_s"] > 0:
            merged["timeout_s"] = float(raw["timeout_s"])
    return merged


def resolve_gemini_api_key(home_dir: Path | None = None) -> str:
    """Resolve Gemini API key from environment variables or headless gemini-api config.yaml."""
    for env_name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        val = os.environ.get(env_name, "").strip()
        if val:
            return val
    base_home = home_dir if home_dir is not None else Path.home()
    yaml_path = base_home / ".config" / "gemini-api" / "config.yaml"
    if yaml_path.is_file():
        try:
            for line in yaml_path.read_text(encoding="utf-8", errors="replace").splitlines():
                match = API_KEY_YAML_RE.match(line)
                if match:
                    key = match.group(1).strip()
                    if key:
                        return key
        except OSError:
            return ""
    return ""


def extract_recent_transcript_context(
    transcript_path: Path | None,
    max_turns: int = 3,
    max_chars: int = 10000,
) -> str:
    """Extract the most recent user and assistant turns from transcript.jsonl."""
    recent_ctx, _ = inspect_transcript_for_autopilot(
        transcript_path,
        max_turns=max_turns,
        max_chars=max_chars,
    )
    return recent_ctx


def normalize_transcript_text(raw: str, max_turn_chars: int = 3000) -> str:
    """Extract clean user/assistant text without metadata wrappers while bounding huge dumps."""
    match = USER_REQUEST_TAG_RE.search(raw)
    cleaned = match.group(1).strip() if match else raw.strip()
    if len(cleaned) <= max_turn_chars:
        return cleaned
    return cleaned[-max_turn_chars:]


def inspect_transcript_for_autopilot(
    transcript_path: Path | None,
    max_turns: int = 3,
    max_chars: int = 10000,
) -> tuple[str, int]:
    """Return (recent_context_text, latest_user_step_index) from transcript.jsonl."""
    if transcript_path is None or not transcript_path.is_file():
        return "", -1
    snippets: list[str] = []
    latest_user_step = -1
    try:
        lines = transcript_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "", -1
    for line in lines[-60:]:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            entry = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        step_type = str(entry.get("type") or "")
        if step_type == "USER_INPUT":
            raw_idx = entry.get("step_index")
            if isinstance(raw_idx, int):
                latest_user_step = raw_idx
        content = entry.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        if step_type == "USER_INPUT":
            text = normalize_transcript_text(content)
            if text:
                snippets.append(f"User: {text}")
        elif step_type == "PLANNER_RESPONSE":
            text = normalize_transcript_text(content)
            if text:
                snippets.append(f"Assistant: {text}")
    if not snippets:
        return "", latest_user_step
    selected = snippets[-(max_turns * 2) :]
    joined = "\n\n".join(selected)
    return joined[-max_chars:], latest_user_step


def resolve_autopilot_state_path(
    conversation_id: str,
    artifact_dir: Path | None = None,
) -> Path | None:
    """Resolve persistent per-conversation state file path using PLUGIN_DATA or artifact_dir."""
    if not conversation_id:
        return None
    plugin_data = os.environ.get("PLUGIN_DATA", "").strip()
    if plugin_data:
        return Path(plugin_data).resolve() / "autopilot-state" / f"{sanitize_identifier(conversation_id)}.json"
    if artifact_dir is not None:
        return artifact_dir / "podo-autopilot-state.json"
    return None


def compute_turn_key(conversation_id: str, user_input: str, latest_user_step: int = -1) -> str:
    """Compute a stable short hash representing a single user turn in a conversation."""
    material = f"{conversation_id}\0{latest_user_step}\0{user_input.strip()}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:16]


def save_autopilot_state(state_path: Path | None, state: dict[str, Any]) -> None:
    """Persist per-conversation autopilot routing state safely."""
    if state_path is None:
        return
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError:
        return


def parse_decisions_response(data: dict[str, Any]) -> dict[str, Any] | None:
    """Extract normalized mode, confidence, probabilities, arena, complexity, and clarification scores."""
    answers = data.get("answers")
    if not isinstance(answers, dict):
        return None
    mode_ans = answers.get("mode")
    if not isinstance(mode_ans, dict):
        return None
    winning = mode_ans.get("winning_option") or mode_ans.get("winningOption") or ""
    if not isinstance(winning, str) or not winning.strip():
        probs_raw = mode_ans.get("probabilities")
        if isinstance(probs_raw, dict) and probs_raw:
            winning = str(max(probs_raw.items(), key=lambda kv: float(kv[1] or 0.0))[0])
    mode = str(winning).strip().lower()
    if mode not in {"direct", "swarm", "plan", "grill"}:
        return None
    confidence_raw = mode_ans.get("confidence")
    confidence = float(confidence_raw) if isinstance(confidence_raw, (int, float)) else 0.0
    probs_raw = mode_ans.get("probabilities")
    probabilities: dict[str, float] = {}
    if isinstance(probs_raw, dict):
        for k, v in probs_raw.items():
            if isinstance(v, (int, float)):
                probabilities[str(k)] = round(float(v), 4)
    if confidence <= 0.0 and mode in probabilities:
        confidence = probabilities[mode]

    arena_prob = 0.0
    arena_ans = answers.get("needs_arena")
    if isinstance(arena_ans, dict):
        noul_val = arena_ans.get("noul")
        if isinstance(noul_val, (int, float)):
            arena_prob = float(noul_val)

    complexity_score: float | None = None
    complexity_ans = answers.get("complexity")
    if isinstance(complexity_ans, dict):
        score_val = complexity_ans.get("score")
        if isinstance(score_val, (int, float)):
            complexity_score = round(float(score_val), 4)

    clarification_prob: float | None = None
    clar_ans = answers.get("needs_clarification")
    if isinstance(clar_ans, dict):
        clar_val = clar_ans.get("noul")
        if isinstance(clar_val, (int, float)):
            clarification_prob = round(float(clar_val), 4)

    continues_prob: float | None = None
    cont_ans = answers.get("continues_previous_task")
    if isinstance(cont_ans, dict):
        cont_val = cont_ans.get("noul")
        if isinstance(cont_val, (int, float)):
            continues_prob = round(float(cont_val), 4)

    return {
        "mode": mode,
        "confidence": round(confidence, 4),
        "probabilities": probabilities,
        "needs_arena_prob": round(arena_prob, 4),
        "complexity_score": complexity_score,
        "needs_clarification_prob": clarification_prob,
        "continues_previous_task_prob": continues_prob,
    }


def call_decisions_api(
    prompt_text: str,
    api_key: str,
    autopilot_cfg: dict[str, Any],
) -> dict[str, Any] | None:
    """Call Gemini Decisions API (gemini-decision-1) with 0 output tokens and fail-open error handling."""
    url = str(autopilot_cfg.get("api_base_url") or DEFAULT_AUTOPILOT_CONFIG["api_base_url"])
    model = str(autopilot_cfg.get("model") or DEFAULT_AUTOPILOT_CONFIG["model"])
    timeout_s = float(autopilot_cfg.get("timeout_s") or DEFAULT_AUTOPILOT_CONFIG["timeout_s"])
    body = json.dumps(
        {
            "model": model,
            "input": prompt_text,
            "questions": AUTOPILOT_DECISION_QUESTIONS,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        parsed = json.loads(raw)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parse_decisions_response(parsed)


def wrap_autopilot_system_message(directive: str, banner: str, show_banner: bool) -> dict[str, Any]:
    """Build a PreInvocation injectSteps payload without metadata fields that break protojson parsing."""
    final_directive = (
        f'{directive} Start your response with this exact one-line banner on line 1, followed by a blank line: "{banner}"'
        if show_banner and banner
        else directive
    )
    return {"injectSteps": [{"systemMessage": {"systemMessage": final_directive}}]}


def evaluate_autopilot_decision_detailed(
    decision: dict[str, Any],
    autopilot_cfg: dict[str, Any],
    plugin_root: Path = PLUGIN_ROOT,
) -> tuple[str, str, dict[str, Any]]:
    """Resolve (effective_mode, route_reason, injectSteps_payload) via the 5-state decision matrix."""
    mode = str(decision.get("mode") or "direct")
    confidence = float(decision.get("confidence") or 0.0)
    probs = decision.get("probabilities") if isinstance(decision.get("probabilities"), dict) else {}
    threshold = float(autopilot_cfg.get("confidence_threshold", 0.65))
    arena_threshold = float(autopilot_cfg.get("arena_threshold", 0.70))
    complexity_min = float(autopilot_cfg.get("complexity_min_for_swarm", 2.2))
    clarification_threshold = float(autopilot_cfg.get("clarification_threshold", 0.90))
    boundary_plan_threshold = float(autopilot_cfg.get("boundary_plan_threshold", 0.75))
    needs_arena = float(decision.get("needs_arena_prob") or 0.0) >= arena_threshold
    complexity_score = decision.get("complexity_score")
    clarification_prob = decision.get("needs_clarification_prob")
    egm_plan_review = bool(autopilot_cfg.get("egm_plan_review", True))
    show_banner = bool(autopilot_cfg.get("show_banner", True))

    is_complex = isinstance(complexity_score, (int, float)) and float(complexity_score) >= complexity_min
    is_low_complexity = isinstance(complexity_score, (int, float)) and float(complexity_score) < complexity_min
    is_high_ambiguity = (
        isinstance(clarification_prob, (int, float)) and float(clarification_prob) >= clarification_threshold
    )
    use_egm_review = egm_plan_review and is_complex
    egm_note = " · Goldfish 설계 검증" if use_egm_review else ""
    egm_directive = (
        " If `egm-design`, `egm-zoom-out`, and `egm-implementation-readiness` subagents are available, "
        "invoke them in parallel (or via `egm-synthesizer`) to run a fresh-eyes Goldfish audit on your drafted plan before presenting the final plan."
        if use_egm_review
        else ""
    )

    if is_complex and is_high_ambiguity:
        clar_pct = f"{float(clarification_prob):.0%}"
        banner = (
            f"🧭 **[Podo Autopilot · Clarify]** 규모가 큰 요청이지만 스펙이 모호해 핵심 확인 질문을 먼저 드립니다. "
            f"*(모호도 {clar_pct} · 복잡도 {float(complexity_score):.1f}/4)*"
        )
        directive = (
            f"[Podo Autopilot Router] gemini-decision-1 flagged a complex but underspecified request "
            f"(complexity={float(complexity_score):.2f}, needs_clarification={float(clarification_prob):.2f}). "
            "Do NOT spawn swarm workers or edit code yet; ask 1-3 concise clarifying questions to lock down scope, target modules, and constraints first."
        )
        return "clarify", "high_ambiguity_clarify", wrap_autopilot_system_message(directive, banner, show_banner)

    if mode != "direct" and confidence >= threshold:
        conf_pct = f"{confidence:.0%}"
        if mode == "swarm":
            if is_low_complexity:
                return "direct", "low_complexity_downgrade", {}
            arena_note = " · Arena 권장" if needs_arena else ""
            arena_directive = (
                " The decision model also flagged high architectural risk (`needs_arena=true`), so consider dual-model Arena competition for the core module."
                if needs_arena
                else ""
            )
            swarm_skill_path = plugin_root / "skills" / "podo-swarm" / "SKILL.md"
            banner = f"🐝 **[Podo Autopilot · Swarm]** 대규모 구현 작업으로 판단되어 `podo-swarm` 파이프라인으로 자동 실행합니다. *(신뢰도 {conf_pct}{arena_note})*"
            directive = (
                f"[Podo Autopilot Router] gemini-decision-1 classified this request as SWARM (confidence={conf_pct}). "
                f"Do NOT ask the user to type /podo-swarm; immediately read {swarm_skill_path} and execute the phased parallel podo-swarm workflow using the configured role models in podo-config.json."
                + arena_directive
            )
            effective_mode = "swarm"
        elif mode == "grill":
            banner = f"🔥 **[Podo Autopilot · Grill]** 엄격한 코드·설계 검증 모드로 자동 전환합니다. *(신뢰도 {conf_pct})*"
            directive = (
                f"[Podo Autopilot Router] gemini-decision-1 classified this request as GRILL (confidence={conf_pct}). "
                "Act as an adversarial senior reviewer: scrutinize security holes, race conditions, hidden assumptions, edge cases, and architectural flaws before proposing fixes."
            )
            effective_mode = "grill"
        else:
            banner = f"🏗️ **[Podo Autopilot · Plan]** 구현 전 아키텍처·단계별 설계 모드로 자동 전환합니다. *(신뢰도 {conf_pct}{egm_note})*"
            directive = (
                f"[Podo Autopilot Router] gemini-decision-1 classified this request as PLAN (confidence={conf_pct}). "
                "Present a concise architecture design, key trade-offs, and a concrete step-by-step execution plan before making code edits."
                + egm_directive
            )
            effective_mode = "plan"

        return effective_mode, "confident_mode", wrap_autopilot_system_message(directive, banner, show_banner)

    swarm_plan_mass = float(probs.get("swarm") or 0.0) + float(probs.get("plan") or 0.0)
    if is_complex and swarm_plan_mass >= boundary_plan_threshold:
        mass_pct = f"{swarm_plan_mass:.0%}"
        banner = (
            f"🏗️ **[Podo Autopilot · Plan]** 규모가 큰 작업이지만 바로 구현/설계 경계선이라 실행 계획을 먼저 제안합니다. "
            f"*(설계+구현 확률 {mass_pct}{egm_note})*"
        )
        directive = (
            f"[Podo Autopilot Router] gemini-decision-1 saw a complex task on the swarm/plan boundary "
            f"(P(swarm)+P(plan)={mass_pct}, complexity={float(complexity_score):.2f}). "
            "Present a concise architecture and step-by-step execution plan first so the user can approve before spawning parallel workers."
            + egm_directive
        )
        return "plan", "complex_boundary_plan", wrap_autopilot_system_message(directive, banner, show_banner)

    return "direct", "direct_default", {}


def evaluate_autopilot_decision(
    decision: dict[str, Any],
    autopilot_cfg: dict[str, Any],
    plugin_root: Path = PLUGIN_ROOT,
) -> tuple[str, dict[str, Any]]:
    """Resolve effective mode and Antigravity PreInvocation injectSteps payload from a decision."""
    effective_mode, _, response_obj = evaluate_autopilot_decision_detailed(
        decision,
        autopilot_cfg,
        plugin_root=plugin_root,
    )
    return effective_mode, response_obj


def build_autopilot_injection(
    decision: dict[str, Any],
    autopilot_cfg: dict[str, Any],
    plugin_root: Path = PLUGIN_ROOT,
) -> dict[str, Any]:
    """Translate a Decisions API classification into an Antigravity PreInvocation injectSteps payload."""
    _, response_obj = evaluate_autopilot_decision(decision, autopilot_cfg, plugin_root=plugin_root)
    return response_obj


def resolve_autopilot_telemetry_target(
    repo: Path | None = None,
    artifact_dir: Path | None = None,
) -> Path | None:
    """Resolve JSONL telemetry target without auto-creating <repo>/.podo/."""
    env_path = os.environ.get("PODO_TELEMETRY_FILE", "").strip()
    if env_path:
        return Path(env_path).resolve()
    if repo is not None and (repo / ".podo").is_dir():
        return repo / ".podo" / "telemetry.jsonl"
    if artifact_dir is not None:
        return artifact_dir / "podo-telemetry.jsonl"
    return None


def emit_autopilot_telemetry(
    cfg: dict[str, Any],
    target_path: Path | None,
    *,
    conversation_id: str,
    model: str,
    decision: dict[str, Any],
    effective_mode: str,
    route_reason: str,
    previous_mode: str,
    injected: bool,
    duration_ms: float,
) -> bool:
    """Append an autopilot_route JSONL telemetry event with calibrated probability diagnostics."""
    if not is_telemetry_enabled(cfg) or target_path is None:
        return False
    record: dict[str, Any] = {
        "ts": round(time.time(), 3),
        "iso_ts": utc_now_iso(),
        "event": "autopilot_route",
        "conversation_id": conversation_id,
        "phase": "",
        "worker": "",
        "role": "autopilot",
        "model": model,
        "gate": "autopilot-router",
        "result": effective_mode,
        "route_reason": route_reason,
        "raw_mode": str(decision.get("mode") or "direct"),
        "confidence": decision.get("confidence", 0.0),
        "probabilities": decision.get("probabilities", {}),
        "needs_arena_prob": decision.get("needs_arena_prob", 0.0),
        "complexity_score": decision.get("complexity_score"),
        "needs_clarification_prob": decision.get("needs_clarification_prob"),
        "continues_previous_task_prob": decision.get("continues_previous_task_prob"),
        "previous_mode": previous_mode,
        "injected": bool(injected),
        "violation_count": 0,
        "files": [],
        "duration_ms": round(max(0.0, float(duration_ms)), 2),
    }
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with target_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def build_router_prompt(
    cleaned_input: str,
    recent_ctx: str = "",
    previous_mode: str = "",
) -> str:
    """Compose the classification prompt combining previous active mode, recent turns, and current input."""
    sections: list[str] = []
    if previous_mode:
        sections.append(f"Previous Active Mode: {previous_mode}")
    if recent_ctx:
        sections.append(f"Recent Conversation:\n{recent_ctx}")
    sections.append(f"Current User Request:\n{cleaned_input}")
    return "\n\n".join(sections)


def handle_hook_pre_invocation(config_path: Path = DEFAULT_CONFIG_PATH) -> int:
    """Route user requests automatically via Gemini Decisions API before the main model runs."""
    t0 = time.monotonic()
    env_toggle = os.environ.get("PODO_AUTOPILOT", "").strip().lower()
    if env_toggle in {"off", "false", "0"}:
        print("{}")
        return 0
    role = os.environ.get("PODO_ROLE", "").strip().lower()
    if role in AUTOPILOT_SKIP_ROLES:
        print("{}")
        return 0

    raw = sys.stdin.read().strip()
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        print("{}")
        return 0

    if not isinstance(payload, dict):
        print("{}")
        return 0

    inv_num = payload.get("invocationNum")
    if isinstance(inv_num, int) and inv_num > 1:
        print("{}")
        return 0

    user_input = payload.get("lastUserInput")
    if not isinstance(user_input, str) or not user_input.strip():
        print("{}")
        return 0
    cleaned_input = user_input.strip()
    if cleaned_input.startswith("/"):
        print("{}")
        return 0

    cfg = load_json_dict(config_path)
    ap_cfg = get_autopilot_config(cfg)
    if not ap_cfg.get("enabled", True):
        print("{}")
        return 0

    api_key = resolve_gemini_api_key()
    if not api_key:
        print("{}")
        return 0

    conv_id = str(payload.get("conversationId") or payload.get("conversation_id") or "").strip()
    artifact_raw = payload.get("artifactDirectoryPath") or payload.get("artifact_directory_path") or ""
    artifact_dir = (
        Path(artifact_raw.strip()).resolve()
        if isinstance(artifact_raw, str) and artifact_raw.strip()
        else None
    )
    transcript_raw = payload.get("transcriptPath") or payload.get("transcript_path")
    transcript_path = (
        Path(transcript_raw.strip()).resolve()
        if isinstance(transcript_raw, str) and transcript_raw.strip()
        else None
    )
    recent_ctx, latest_user_step = inspect_transcript_for_autopilot(transcript_path)

    state_path = resolve_autopilot_state_path(conv_id, artifact_dir=artifact_dir)
    prev_state = load_json_dict(state_path)
    turn_key = compute_turn_key(conv_id, cleaned_input, latest_user_step) if conv_id else ""
    if turn_key and prev_state.get("last_turn_key") == turn_key:
        print("{}")
        return 0

    previous_mode = str(prev_state.get("active_mode") or "").strip().lower()
    prompt_text = build_router_prompt(
        cleaned_input,
        recent_ctx=recent_ctx,
        previous_mode=previous_mode,
    )

    decision = call_decisions_api(prompt_text, api_key, ap_cfg)
    if decision is None:
        print("{}")
        return 0

    effective_mode, route_reason, response_obj = evaluate_autopilot_decision_detailed(
        decision,
        ap_cfg,
        plugin_root=config_path.parent,
    )
    if state_path is not None and turn_key:
        save_autopilot_state(
            state_path,
            {
                "conversation_id": conv_id,
                "last_turn_key": turn_key,
                "active_mode": effective_mode,
                "route_reason": route_reason,
                "raw_mode": decision.get("mode", "direct"),
                "updated_at": utc_now_iso(),
            },
        )

    cwd = payload.get("cwd") or (payload.get("workspacePaths") or [None])[0]
    repo = Path(cwd).resolve() if isinstance(cwd, str) and cwd and Path(cwd).is_dir() else None
    telemetry_target = resolve_autopilot_telemetry_target(repo=repo, artifact_dir=artifact_dir)
    emit_autopilot_telemetry(
        cfg,
        telemetry_target,
        conversation_id=conv_id,
        model=str(ap_cfg.get("model") or DEFAULT_AUTOPILOT_CONFIG["model"]),
        decision=decision,
        effective_mode=effective_mode,
        route_reason=route_reason,
        previous_mode=previous_mode,
        injected=bool(response_obj),
        duration_ms=(time.monotonic() - t0) * 1000.0,
    )

    print(json.dumps(response_obj, ensure_ascii=False))
    return 0
