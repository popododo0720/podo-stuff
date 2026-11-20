#!/usr/bin/env python3
"""Offline/live golden evaluation harness for the podo-stuff Autopilot Router."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from podo.autopilot import (
    DEFAULT_CONFIG_PATH,
    PLUGIN_ROOT,
    build_router_prompt,
    call_decisions_api,
    evaluate_autopilot_decision_detailed,
    get_autopilot_config,
    load_json_dict,
    resolve_gemini_api_key,
)


def evaluate_golden_dataset(
    golden_path: Path,
    config_path: Path = DEFAULT_CONFIG_PATH,
) -> dict[str, Any]:
    """Run all golden test cases against gemini-decision-1 and return accuracy metrics."""
    cfg = load_json_dict(config_path)
    ap_cfg = get_autopilot_config(cfg)
    api_key = resolve_gemini_api_key()
    if not api_key:
        raise RuntimeError("Gemini API key not found (set GEMINI_API_KEY or ~/.config/gemini-api/config.yaml)")

    cases = json.loads(golden_path.read_text(encoding="utf-8"))
    if not isinstance(cases, list):
        raise ValueError(f"Expected JSON array in {golden_path}")

    results: list[dict[str, Any]] = []
    confusion: dict[str, dict[str, int]] = {}
    passed = 0
    total_ms = 0.0

    for item in cases:
        case_id = str(item.get("id") or "unnamed")
        user_input = str(item.get("input") or "")
        recent_ctx = str(item.get("recent_context") or "")
        prev_mode = str(item.get("previous_mode") or "")
        expected_modes = [str(m).lower() for m in (item.get("expected") or [])]
        expect_arena = item.get("expect_arena")
        expect_continuation = item.get("expect_continuation")

        prompt = build_router_prompt(user_input, recent_ctx=recent_ctx, previous_mode=prev_mode)
        t0 = time.monotonic()
        decision = call_decisions_api(prompt, api_key, ap_cfg)
        elapsed_ms = round((time.monotonic() - t0) * 1000.0, 2)
        total_ms += elapsed_ms

        if decision is None:
            effective_mode = "error"
            route_reason = "api_error"
            arena_ok = False
            cont_ok = False
        else:
            effective_mode, route_reason, _ = evaluate_autopilot_decision_detailed(
                decision,
                ap_cfg,
                plugin_root=config_path.parent,
            )
            arena_prob = float(decision.get("needs_arena_prob") or 0.0)
            arena_thresh = float(ap_cfg.get("arena_threshold", 0.70))
            arena_ok = True if expect_arena is None else ((arena_prob >= arena_thresh) == bool(expect_arena))
            cont_prob = decision.get("continues_previous_task_prob")
            cont_ok = (
                True
                if expect_continuation is None or not isinstance(cont_prob, (int, float))
                else ((float(cont_prob) >= 0.5) == bool(expect_continuation))
            )

        mode_ok = effective_mode in expected_modes
        ok = mode_ok and arena_ok and cont_ok
        if ok:
            passed += 1

        exp_key = expected_modes[0] if expected_modes else "unknown"
        row = confusion.setdefault(exp_key, {})
        row[effective_mode] = row.get(effective_mode, 0) + 1

        results.append(
            {
                "id": case_id,
                "ok": ok,
                "expected": expected_modes,
                "actual": effective_mode,
                "route_reason": route_reason,
                "raw_mode": decision.get("mode") if decision else None,
                "confidence": decision.get("confidence") if decision else None,
                "complexity_score": decision.get("complexity_score") if decision else None,
                "needs_clarification_prob": decision.get("needs_clarification_prob") if decision else None,
                "continues_previous_task_prob": decision.get("continues_previous_task_prob") if decision else None,
                "needs_arena_prob": decision.get("needs_arena_prob") if decision else None,
                "duration_ms": elapsed_ms,
            }
        )

    total = len(results)
    accuracy = round(passed / total, 4) if total else 0.0
    return {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "accuracy": accuracy,
        "total_duration_ms": round(total_ms, 2),
        "avg_duration_ms": round(total_ms / total, 2) if total else 0.0,
        "confusion": confusion,
        "cases": results,
    }


def main() -> int:
    """CLI entrypoint for running golden evaluation against the Autopilot Router."""
    parser = argparse.ArgumentParser(prog="eval-autopilot.py")
    parser.add_argument(
        "--golden",
        default=str(PLUGIN_ROOT / "tests" / "autopilot_golden.json"),
        help="Path to golden test cases JSON file",
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to podo-config.json")
    parser.add_argument("--json", action="store_true", help="Output full report as JSON")
    args = parser.parse_args()

    report = evaluate_golden_dataset(Path(args.golden).resolve(), Path(args.config).resolve())
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if report["failed"] == 0 else 1

    print(
        f"=== Autopilot Golden Eval: {report['passed']}/{report['total']} "
        f"({report['accuracy']:.1%}) · avg {report['avg_duration_ms']:.0f} ms ==="
    )
    for row in report["cases"]:
        mark = "PASS" if row["ok"] else "FAIL"
        print(
            f"[{mark}] {row['id']:30s} expected={','.join(row['expected']):14s} "
            f"actual={row['actual']:8s} reason={row['route_reason']:24s} "
            f"raw={str(row['raw_mode']):6s} conf={row['confidence']} comp={row['complexity_score']} "
            f"clar={row['needs_clarification_prob']} cont={row['continues_previous_task_prob']} ({row['duration_ms']:.0f}ms)"
        )
    print("Confusion:", json.dumps(report["confusion"], ensure_ascii=False))
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
