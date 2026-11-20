#!/usr/bin/env python3
"""Deterministic guardrails, AST narration-comment & dead-code gates, DAG renderer, and hook engine for podo-stuff."""

from __future__ import annotations

import argparse
import ast
import fnmatch
import html
import io
import json
import os
import re
import subprocess
import sys
import textwrap
import time
import tokenize
import urllib.request  # noqa: F401
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from podo.autopilot import (
    AUTOPILOT_DECISION_QUESTIONS,
    AUTOPILOT_SKIP_ROLES,
    DEFAULT_AUTOPILOT_CONFIG,
    build_autopilot_injection,
    call_decisions_api,
    evaluate_autopilot_decision,
    extract_recent_transcript_context,
    get_autopilot_config,
    handle_hook_pre_invocation,
    parse_decisions_response,
    resolve_gemini_api_key,
)

__all__ = [
    "AUTOPILOT_DECISION_QUESTIONS",
    "AUTOPILOT_SKIP_ROLES",
    "DEFAULT_AUTOPILOT_CONFIG",
    "build_autopilot_injection",
    "call_decisions_api",
    "evaluate_autopilot_decision",
    "extract_recent_transcript_context",
    "get_autopilot_config",
    "handle_hook_pre_invocation",
    "parse_decisions_response",
    "resolve_gemini_api_key",
]

PLUGIN_ROOT = SCRIPTS_DIR.parent
DEFAULT_CONFIG_PATH = PLUGIN_ROOT / "podo-config.json"

DEFAULT_ALLOW_TAGS = ("WHY:", "INVARIANT:", "SAFETY:", "NOTE(popododo):")

DEFAULT_ROLES: dict[str, str] = {
    "scout": "gemini-4-hydrogen-medium",
    "builder": "gemini-pro-orchestrator-neon",
    "arena_a": "gemini-pro-orchestrator-neon",
    "arena_b": "claude-opus-5-5-medium",
    "interrogator": "claude-opus-5-5-medium",
    "monitor": "gemini-4-hydrogen-low",
}

READ_ONLY_ROLES = {"scout", "interrogator", "monitor", "coordinator"}
VALID_STOP_MODES = ("enforce", "observe", "off")

SOURCE_EXTENSIONS = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
    ".go",
    ".rs",
    ".java",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".cc",
    ".sh",
    ".bash",
    ".zsh",
}

PYTHON_EXTENSIONS = {".py"}
SHELL_EXTENSIONS = {".sh", ".bash", ".zsh"}
BRACE_EXTENSIONS = SOURCE_EXTENSIONS - PYTHON_EXTENSIONS - SHELL_EXTENSIONS
JS_REGEX_EXTENSIONS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}

REGEX_PRECEDING_KEYWORDS = {
    "return",
    "case",
    "throw",
    "typeof",
    "void",
    "delete",
    "in",
    "instanceof",
    "of",
    "new",
    "yield",
    "await",
    "else",
    "do",
}

IGNORED_DIRS = {
    ".git",
    ".podo",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    "target",
}

DIRECTIVE_PREFIXES = (
    "#!",
    "# type:",
    "# noqa",
    "# pragma",
    "# pylint",
    "# pyright",
    "# fmt:",
    "# isort:",
    "# ruff:",
    "# mypy:",
    "# coding:",
    "# -*- coding:",
    "// eslint-",
    "/* eslint-",
    "// @ts-",
    "// prettier-ignore",
    "/* prettier-ignore",
    "// nolint",
    "//go:",
    "///",
    "//!",
    "#[",
    "#![",
)

DEAD_CODE_PATTERNS = [
    re.compile(r"^\s*(?:const|let|var|return|import|export|function|class|if|for|while|switch)\b"),
    re.compile(r"^\s*(?:def|async\s+def|class|return|import|from\s+\S+\s+import|if|elif|for|while)\b"),
    re.compile(r"[;{}]\s*$"),
]

BARE_TODO_RE = re.compile(r"\b(?:TODO|FIXME|XXX|HACK)\b", re.IGNORECASE)
TRACKED_REF_RE = re.compile(r"(?:https?://|#\d+\b|\b[A-Z]{2,10}-\d+\b)")
NOQA_RE = re.compile(r"#\s*noqa\b", re.IGNORECASE)

TERMINATOR_NAMES = {
    ast.Return: "return",
    ast.Raise: "raise",
    ast.Break: "break",
    ast.Continue: "continue",
}

EDIT_TOOL_NAMES = {
    "edit_file",
    "write_file",
    "replace_file_content",
    "multi_replace_file_content",
    "write_to_file",
}

STATUS_LABELS = {
    "pending": ("Pending", "badge-pending"),
    "building": ("Building", "badge-building"),
    "script_checked": ("Script Checked", "badge-script"),
    "arena_running": ("Arena Competing", "badge-arena"),
    "phase_interrogating": ("Interrogating", "badge-interrogating"),
    "rejected": ("Rejected", "badge-rejected"),
    "verified": ("Verified", "badge-verified"),
}

MERMAID_STATUS_BADGE = {
    "pending": "⏳ pending",
    "building": "🔨 building",
    "script_checked": "🧪 script-ok",
    "arena_running": "⚔️ arena",
    "phase_interrogating": "🔍 interrogating",
    "rejected": "🔁 rejected",
    "verified": "✅ verified",
}

DEFAULT_TELEMETRY_ROLE_AVG_TOKENS: dict[str, int] = {
    "scout": 12000,
    "builder": 28000,
    "arena_a": 32000,
    "arena_b": 32000,
    "interrogator": 25000,
    "monitor": 4000,
}

DEFAULT_TELEMETRY_ANOMALY_THRESHOLDS: dict[str, float] = {
    "interrogator_calls_per_phase_warn": 2.0,
    "gate_fail_rate_warn": 0.35,
    "interrogator_history_multiplier_warn": 2.0,
}

ACTIVE_DAG_STATUSES = {"building", "arena_running", "script_checked", "phase_interrogating"}
TERMINAL_DAG_STATUSES = {"verified", "rejected"}
INTERROGATOR_CALL_STATUSES = {"phase_interrogating", "rejected", "verified"}
VIOLATION_FILE_RE = re.compile(r"^\[[^\]]+\]\s+([^\s:]+):\d+")


def default_config() -> dict[str, Any]:
    """Return a fresh copy of the default podo-config schema."""
    return {
        "default_roles": dict(DEFAULT_ROLES),
        "roles": dict(DEFAULT_ROLES),
        "gates": {
            "stop_hook_mode": "enforce",
            "per_task_deterministic_script": True,
            "dead_code_check": True,
            "interrogator_cadence": "per_dag_phase",
            "allow_comment_tags": list(DEFAULT_ALLOW_TAGS),
        },
        "telemetry": {
            "enabled": True,
            "role_avg_tokens": dict(DEFAULT_TELEMETRY_ROLE_AVG_TOKENS),
            "anomaly_thresholds": dict(DEFAULT_TELEMETRY_ANOMALY_THRESHOLDS),
        },
        "autopilot": dict(DEFAULT_AUTOPILOT_CONFIG),
    }


def load_config(config_path: Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    """Load podo-config.json from the plugin directory or fallback defaults."""
    if config_path.is_file():
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                return loaded
        except (json.JSONDecodeError, OSError):
            return default_config()
    return default_config()


def save_config(cfg: dict[str, Any], config_path: Path = DEFAULT_CONFIG_PATH) -> None:
    """Persist updated configuration to podo-config.json."""
    config_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def get_allow_tags(cfg: dict[str, Any]) -> tuple[str, ...]:
    """Return allowed rationale prefixes from configuration."""
    tags = cfg.get("gates", {}).get("allow_comment_tags")
    if isinstance(tags, list) and tags:
        return tuple(str(t) for t in tags)
    return DEFAULT_ALLOW_TAGS


def is_dead_code_check_enabled(cfg: dict[str, Any]) -> bool:
    """Return whether the deterministic Python AST dead-code gate is enabled."""
    gates = cfg.get("gates", {})
    if isinstance(gates, dict) and "dead_code_check" in gates:
        return bool(gates["dead_code_check"])
    return True


def is_telemetry_enabled(cfg: dict[str, Any]) -> bool:
    """Return whether JSONL telemetry emission is enabled."""
    telemetry = cfg.get("telemetry", {})
    if isinstance(telemetry, dict) and "enabled" in telemetry:
        return bool(telemetry["enabled"])
    return True


def get_role_avg_tokens(cfg: dict[str, Any]) -> dict[str, int]:
    """Return per-role average token estimates merged with defaults."""
    merged = dict(DEFAULT_TELEMETRY_ROLE_AVG_TOKENS)
    telemetry = cfg.get("telemetry", {})
    if isinstance(telemetry, dict):
        raw = telemetry.get("role_avg_tokens")
        if isinstance(raw, dict):
            for role, val in raw.items():
                if isinstance(val, (int, float)) and val >= 0:
                    merged[str(role)] = int(val)
    return merged


def get_anomaly_thresholds(cfg: dict[str, Any]) -> dict[str, float]:
    """Return telemetry anomaly thresholds merged with defaults."""
    merged = dict(DEFAULT_TELEMETRY_ANOMALY_THRESHOLDS)
    telemetry = cfg.get("telemetry", {})
    if isinstance(telemetry, dict):
        raw = telemetry.get("anomaly_thresholds")
        if isinstance(raw, dict):
            for key, val in raw.items():
                if isinstance(val, (int, float)) and val >= 0:
                    merged[str(key)] = float(val)
    return merged


def get_role_model(cfg: dict[str, Any], role: str) -> str:
    """Return configured model id for a role, or empty string if unknown."""
    if not role:
        return ""
    roles = cfg.get("roles", {})
    if isinstance(roles, dict):
        model = roles.get(role)
        if isinstance(model, str) and model.strip():
            return model.strip()
    return DEFAULT_ROLES.get(role, "")


def resolve_stop_hook_mode(cfg: dict[str, Any]) -> str:
    """Resolve effective Stop hook mode with PODO_GUARD_MODE overriding config."""
    env_mode = os.environ.get("PODO_GUARD_MODE", "").strip().lower()
    if env_mode in VALID_STOP_MODES:
        return env_mode
    gates = cfg.get("gates", {})
    cfg_mode = str(gates.get("stop_hook_mode", "enforce")).strip().lower() if isinstance(gates, dict) else "enforce"
    if cfg_mode in VALID_STOP_MODES:
        return cfg_mode
    return "enforce"


def get_interrogator_model(cfg: dict[str, Any] | None = None) -> str:
    """Return the active interrogator model configured in podo-config.json."""
    resolved = cfg if cfg is not None else load_config(DEFAULT_CONFIG_PATH)
    return get_role_model(resolved, "interrogator") or DEFAULT_ROLES["interrogator"]


def strip_comment_marker(comment_text: str) -> str:
    """Extract readable body text from a comment token or line."""
    text = comment_text.strip()
    if text.startswith("///") or text.startswith("//!"):
        return text[3:].strip()
    if text.startswith("//"):
        return text[2:].strip()
    if text.startswith("#"):
        return text[1:].strip()
    if text.startswith("/*"):
        text = text[2:]
        if text.endswith("*/"):
            text = text[:-2]
        cleaned_lines = [ln.strip().lstrip("*").strip() for ln in text.splitlines()]
        return "\n".join(ln for ln in cleaned_lines if ln).strip()
    return text


def is_allowed_comment(
    raw_comment: str,
    allow_tags: tuple[str, ...],
    allow_bare_header: bool = False,
) -> tuple[bool, str, bool]:
    """Return (allowed, rejection_reason, always_block) for a comment."""
    stripped = raw_comment.strip()
    for prefix in DIRECTIVE_PREFIXES:
        if stripped.startswith(prefix):
            return True, "", False

    body = strip_comment_marker(stripped)
    if not body:
        return True, "", False

    if BARE_TODO_RE.search(body) and not TRACKED_REF_RE.search(body):
        return False, "Untracked TODO/FIXME placeholder (link an issue/PR or resolve now)", True

    for tag in allow_tags:
        if body.startswith(tag):
            return True, "", False

    for line_part in body.splitlines():
        for pattern in DEAD_CODE_PATTERNS:
            if pattern.search(line_part):
                return False, "Commented-out code detected; delete dead code instead of commenting it out", True

    if allow_bare_header:
        return True, "", False

    return (
        False,
        "Remove inline narration comment or prefix non-obvious constraint with "
        + " / ".join(f"'{t}'" for t in allow_tags[:3]),
        False,
    )


def parse_unified_diff(diff_text: str) -> dict[str, dict[int, str]]:
    """Parse unified diff output into {rel_path: {added_line_no: line_text}}."""
    added_by_file: dict[str, dict[int, str]] = {}
    current_file: str | None = None
    next_line_no = 0
    hunk_re = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            if target == "/dev/null":
                current_file = None
                continue
            if target.startswith("b/"):
                target = target[2:]
            current_file = target
            added_by_file.setdefault(current_file, {})
            continue
        if line.startswith("--- ") or line.startswith("diff --git "):
            continue
        if current_file is None:
            continue
        hunk_match = hunk_re.match(line)
        if hunk_match:
            next_line_no = int(hunk_match.group(1))
            continue
        if line.startswith("+") and not line.startswith("+++"):
            added_by_file[current_file][next_line_no] = line[1:]
            next_line_no += 1
        elif line.startswith("-") and not line.startswith("---"):
            continue
        elif line.startswith("\\ No newline at end of file"):
            continue
        else:
            next_line_no += 1

    return added_by_file


def normalize_rel_path(raw: str, repo: Path) -> str | None:
    """Normalize a repo-relative or absolute file path into a POSIX repo-relative string."""
    cleaned = raw.strip()
    if not cleaned:
        return None
    candidate = Path(cleaned)
    if candidate.is_absolute():
        try:
            rel = candidate.resolve().relative_to(repo.resolve()).as_posix()
        except ValueError:
            return None
        return rel
    posix = candidate.as_posix()
    while posix.startswith("./"):
        posix = posix[2:]
    return posix or None


def parse_files_csv(files_arg: str | None, repo: Path) -> set[str] | None:
    """Parse a comma-separated --files argument into normalized relative paths."""
    if files_arg is None:
        return None
    result: set[str] = set()
    for part in files_arg.split(","):
        norm = normalize_rel_path(part, repo)
        if norm:
            result.add(norm)
    return result


def python_function_ranges(source: str) -> list[tuple[int, int]] | None:
    """Return 1-indexed (start, end) line ranges for Python functions if syntax is valid."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    ranges: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", node.lineno)
            ranges.append((node.lineno, end))
    return ranges


def python_module_header_end(source: str) -> int:
    """Return the last line before the first non-docstring Python statement."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return 0
    for stmt in tree.body:
        if (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        ):
            continue
        return max(0, stmt.lineno - 1)
    return len(source.splitlines())


def tokenize_python_comments(source: str) -> tuple[list[tuple[int, str]], bool]:
    """Extract (1-indexed line_no, comment_text) pairs via tokenize; report completeness."""
    comments: list[tuple[int, str]] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                comments.append((tok.start[0], tok.string))
        return comments, True
    except (tokenize.TokenError, IndentationError):
        return comments, False


def check_python_comments(
    rel_path: str,
    added_lines: dict[int, str],
    full_source: str | None,
    allow_tags: tuple[str, ...],
) -> list[str]:
    """Check added Python lines for forbidden narration or placeholder comments."""
    violations: list[str] = []
    if full_source is not None:
        func_ranges = python_function_ranges(full_source)
        header_end = python_module_header_end(full_source)
        comments, _ = tokenize_python_comments(full_source)
        source_lines = {i + 1: text for i, text in enumerate(full_source.splitlines())}
        for tok_line, tok_text in comments:
            if tok_line not in added_lines:
                continue
            raw_line = source_lines.get(tok_line, "")
            is_indented = (len(raw_line) - len(raw_line.lstrip(" \t"))) > 0
            if func_ranges is not None:
                in_function = any(start <= tok_line <= end for start, end in func_ranges)
                in_scope = in_function
            else:
                in_function = is_indented
                in_scope = is_indented
            in_header = tok_line <= header_end and not is_indented
            ok, reason, always_block = is_allowed_comment(
                tok_text,
                allow_tags,
                allow_bare_header=(in_header and not in_function),
            )
            if not ok and (in_scope or always_block):
                violations.append(f"[NO-COMMENTS] {rel_path}:{tok_line}: {tok_text.strip()} -> {reason}")
        return violations

    ordered = sorted(added_lines.items())
    if not ordered:
        return violations

    ordered_line_nos = [ln for ln, _ in ordered]
    orig_lines = [text for _, text in ordered]
    dedented_source = textwrap.dedent("\n".join(orig_lines))
    func_ranges = python_function_ranges(dedented_source)
    header_end = python_module_header_end(dedented_source) if func_ranges is not None else 0

    comments, complete = tokenize_python_comments(dedented_source)
    if not complete:
        comments = []
        for idx, raw_text in enumerate(orig_lines, start=1):
            line_comments, line_ok = tokenize_python_comments(raw_text.lstrip(" \t"))
            if line_ok:
                for _, tok_text in line_comments:
                    comments.append((idx, tok_text))
            else:
                stripped = raw_text.lstrip(" \t")
                if stripped.startswith("#"):
                    comments.append((idx, stripped))

    for tok_line, tok_text in comments:
        if tok_line < 1 or tok_line > len(ordered_line_nos):
            continue
        actual_line = ordered_line_nos[tok_line - 1]
        orig_line = orig_lines[tok_line - 1]
        orig_indented = (len(orig_line) - len(orig_line.lstrip(" \t"))) > 0
        in_func = (
            any(start <= tok_line <= end for start, end in func_ranges)
            if func_ranges is not None
            else False
        )
        has_code_prefix = not orig_line.lstrip(" \t").startswith("#")
        in_body = in_func or orig_indented or has_code_prefix
        in_header = tok_line <= header_end and not in_body
        ok, reason, always_block = is_allowed_comment(
            tok_text,
            allow_tags,
            allow_bare_header=in_header,
        )
        if not ok and (in_body or always_block):
            violations.append(f"[NO-COMMENTS] {rel_path}:{actual_line}: {tok_text.strip()} -> {reason}")
    return violations


def scan_brace_language_comments(
    source: str,
    suffix: str = "",
) -> list[tuple[int, str, int, bool]]:
    """Extract (line_no, comment_text, brace_depth, is_header) from C-style brace code."""
    results: list[tuple[int, str, int, bool]] = []
    depth = 0
    seen_code = False
    i = 0
    n = len(source)
    line_no = 1
    in_string: str | None = None
    in_regex = False
    in_regex_class = False
    in_line_comment = False
    in_block_comment = False
    block_is_jsdoc = False
    comment_start_line = 1
    comment_start_depth = 0
    comment_is_header = True
    comment_buf: list[str] = []
    expect_regex = True

    lower_suffix = suffix.lower()
    enable_js_regex = lower_suffix in JS_REGEX_EXTENSIONS or lower_suffix == ""
    is_rust = lower_suffix == ".rs"

    while i < n:
        ch = source[i]
        nxt = source[i + 1] if i + 1 < n else ""

        if in_line_comment:
            if ch == "\n":
                results.append((comment_start_line, "".join(comment_buf), comment_start_depth, comment_is_header))
                in_line_comment = False
                comment_buf = []
                line_no += 1
            else:
                comment_buf.append(ch)
            i += 1
            continue

        if in_block_comment:
            comment_buf.append(ch)
            if ch == "\n":
                line_no += 1
            elif ch == "*" and nxt == "/":
                comment_buf.append("/")
                if not (block_is_jsdoc and comment_start_depth == 0):
                    full_block = "".join(comment_buf)
                    for covered_line in range(comment_start_line, line_no + 1):
                        results.append((covered_line, full_block, comment_start_depth, comment_is_header))
                in_block_comment = False
                block_is_jsdoc = False
                comment_buf = []
                i += 2
                continue
            i += 1
            continue

        if in_regex:
            if ch == "\\" and i + 1 < n:
                if nxt == "\n":
                    in_regex = False
                    in_regex_class = False
                    line_no += 1
                    i += 2
                    continue
                i += 2
                continue
            if ch == "\n":
                in_regex = False
                in_regex_class = False
                line_no += 1
                i += 1
                continue
            if ch == "[":
                in_regex_class = True
            elif ch == "]":
                in_regex_class = False
            elif ch == "/" and not in_regex_class:
                in_regex = False
                expect_regex = False
            i += 1
            continue

        if in_string is not None:
            if ch == "\\" and i + 1 < n:
                if nxt == "\n":
                    line_no += 1
                i += 2
                continue
            if ch == in_string:
                in_string = None
                expect_regex = False
            elif ch == "\n":
                line_no += 1
            i += 1
            continue

        if ch == "/" and nxt == "/":
            in_line_comment = True
            comment_start_line = line_no
            comment_start_depth = depth
            comment_is_header = not seen_code
            comment_buf = ["//"]
            i += 2
            continue

        if ch == "/" and nxt == "*":
            in_block_comment = True
            block_is_jsdoc = i + 2 < n and source[i + 2] == "*"
            comment_start_line = line_no
            comment_start_depth = depth
            comment_is_header = not seen_code
            comment_buf = ["/*"]
            i += 2
            continue

        if ch == "/" and enable_js_regex and expect_regex:
            seen_code = True
            in_regex = True
            in_regex_class = False
            i += 1
            continue

        if is_rust and ch == "'":
            seen_code = True
            if i + 2 < n and nxt != "\\" and nxt != "\n" and source[i + 2] == "'":
                i += 3
                expect_regex = False
                continue
            if nxt == "\\":
                j = i + 3
                while j < n and j < i + 18 and source[j] != "\n":
                    if source[j] == "'":
                        j += 1
                        break
                    j += 1
                i = j
                expect_regex = False
                continue
            j = i + 1
            while j < n and (source[j].isalnum() or source[j] == "_"):
                j += 1
            i = j
            expect_regex = False
            continue

        if ch in ('"', "'", "`"):
            seen_code = True
            in_string = ch
            i += 1
            continue

        if ch == "{":
            seen_code = True
            depth += 1
            expect_regex = True
        elif ch == "}":
            seen_code = True
            depth = max(0, depth - 1)
            expect_regex = True
        elif ch == "\n":
            line_no += 1
        elif ch.isalpha() or ch in ("_", "$"):
            seen_code = True
            j = i + 1
            while j < n and (source[j].isalnum() or source[j] in ("_", "$")):
                j += 1
            word = source[i:j]
            expect_regex = word in REGEX_PRECEDING_KEYWORDS
            i = j
            continue
        elif ch.isdigit():
            seen_code = True
            j = i + 1
            while j < n and (source[j].isalnum() or source[j] in (".", "_")):
                j += 1
            expect_regex = False
            i = j
            continue
        elif (ch == "+" and nxt == "+") or (ch == "-" and nxt == "-"):
            seen_code = True
            expect_regex = False
            i += 2
            continue
        elif ch in (")", "]"):
            seen_code = True
            expect_regex = False
        elif not ch.isspace():
            seen_code = True
            expect_regex = True

        i += 1

    if in_line_comment and comment_buf:
        results.append((comment_start_line, "".join(comment_buf), comment_start_depth, comment_is_header))
    elif in_block_comment and comment_buf:
        if not (block_is_jsdoc and comment_start_depth == 0):
            full_block = "".join(comment_buf)
            for covered_line in range(comment_start_line, line_no + 1):
                results.append((covered_line, full_block, comment_start_depth, comment_is_header))
    return results


def format_comment_preview(comment_text: str) -> str:
    """Format a single-line readable preview of a comment for violation diagnostics."""
    compact = " ".join(comment_text.split())
    if len(compact) > 120:
        return compact[:117] + "..."
    return compact


def check_brace_comments(
    rel_path: str,
    added_lines: dict[int, str],
    full_source: str | None,
    allow_tags: tuple[str, ...],
) -> list[str]:
    """Check added brace-language lines for forbidden inline narration or placeholder comments."""
    violations: list[str] = []
    suffix = Path(rel_path).suffix.lower()
    if full_source is not None:
        scanned = scan_brace_language_comments(full_source, suffix=suffix)
        for line_no, comment_text, depth, is_header in scanned:
            if line_no not in added_lines:
                continue
            ok, reason, always_block = is_allowed_comment(
                comment_text,
                allow_tags,
                allow_bare_header=(is_header and depth == 0),
            )
            if not ok and (depth >= 1 or always_block):
                preview = format_comment_preview(comment_text)
                violations.append(f"[NO-COMMENTS] {rel_path}:{line_no}: {preview} -> {reason}")
        return violations

    ordered = sorted(added_lines.items())
    if not ordered:
        return violations

    ordered_line_nos = [ln for ln, _ in ordered]
    orig_lines = [text for _, text in ordered]
    snippet = "\n".join(orig_lines)
    scanned = scan_brace_language_comments(snippet, suffix=suffix)
    for tok_line, comment_text, depth, is_header in scanned:
        if tok_line < 1 or tok_line > len(ordered_line_nos):
            continue
        actual_line = ordered_line_nos[tok_line - 1]
        orig_line = orig_lines[tok_line - 1]
        orig_indented = (len(orig_line) - len(orig_line.lstrip(" \t"))) > 0
        in_body = depth >= 1 or orig_indented
        ok, reason, always_block = is_allowed_comment(
            comment_text,
            allow_tags,
            allow_bare_header=(is_header and not in_body),
        )
        if not ok and (in_body or always_block):
            preview = format_comment_preview(comment_text)
            violations.append(f"[NO-COMMENTS] {rel_path}:{actual_line}: {preview} -> {reason}")
    return violations


def check_shell_comments(
    rel_path: str,
    added_lines: dict[int, str],
    allow_tags: tuple[str, ...],
) -> list[str]:
    """Check added shell lines for forbidden indented narration or placeholder comments."""
    violations: list[str] = []
    for line_no, text in sorted(added_lines.items()):
        stripped = text.lstrip(" \t")
        if not stripped.startswith("#"):
            continue
        is_indented = len(text) > len(stripped)
        ok, reason, always_block = is_allowed_comment(stripped, allow_tags, allow_bare_header=not is_indented)
        if not ok and (is_indented or always_block):
            violations.append(f"[NO-COMMENTS] {rel_path}:{line_no}: {stripped} -> {reason}")
    return violations


def is_type_checking_guard(test: ast.expr) -> bool:
    """Return True if an ast.If test represents TYPE_CHECKING or typing.TYPE_CHECKING."""
    if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
        return True
    if isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING":
        return True
    return False


def extract_all_exports(tree: ast.Module) -> set[str]:
    """Extract string symbol names declared in top-level __all__ assignments."""
    exports: set[str] = set()

    def collect_elts(value: ast.expr | None) -> None:
        if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
            for elt in value.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    exports.add(elt.value)

    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == "__all__" for t in stmt.targets):
                collect_elts(stmt.value)
        elif isinstance(stmt, ast.AnnAssign):
            if isinstance(stmt.target, ast.Name) and stmt.target.id == "__all__":
                collect_elts(stmt.value)
        elif isinstance(stmt, ast.AugAssign):
            if isinstance(stmt.target, ast.Name) and stmt.target.id == "__all__":
                collect_elts(stmt.value)
    return exports


def collect_loaded_names(root: ast.AST, exclude_node: ast.AST | None = None) -> tuple[set[str], set[str]]:
    """Collect (loaded_name_ids, attribute_names) under root, optionally skipping exclude_node."""
    loaded: set[str] = set()
    attrs: set[str] = set()

    def parse_string_annotation(expr: ast.expr | None) -> None:
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            try:
                parsed = ast.parse(expr.value, mode="eval")
            except SyntaxError:
                return
            for sub in ast.walk(parsed):
                if isinstance(sub, ast.Name):
                    loaded.add(sub.id)
                elif isinstance(sub, ast.Attribute):
                    attrs.add(sub.attr)

    def visit(node: ast.AST) -> None:
        if exclude_node is not None and node is exclude_node:
            return
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Load, ast.Del)):
            loaded.add(node.id)
        elif isinstance(node, ast.Attribute):
            attrs.add(node.attr)
        elif isinstance(node, ast.arg) and node.annotation is not None:
            parse_string_annotation(node.annotation)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns is not None:
            parse_string_annotation(node.returns)
        elif isinstance(node, ast.AnnAssign) and node.annotation is not None:
            parse_string_annotation(node.annotation)
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(root)
    return loaded, attrs


def line_has_noqa(lines: list[str], start_line: int, end_line: int) -> bool:
    """Return True if any line in [start_line, end_line] (1-indexed) carries a # noqa marker."""
    lo = max(1, start_line)
    hi = min(len(lines), max(start_line, end_line))
    for ln in range(lo, hi + 1):
        if NOQA_RE.search(lines[ln - 1]):
            return True
    return False


def check_python_dead_code(
    rel_path: str,
    added_lines: dict[int, str],
    full_source: str | None,
) -> list[str]:
    """Run deterministic Python AST dead-code checks for unreachable statements, unused imports, and unused private helpers."""
    violations: list[str] = []
    if full_source is not None:
        source_text = full_source
        ordered_line_nos: list[int] | None = None
    else:
        ordered = sorted(added_lines.items())
        if not ordered:
            return violations
        ordered_line_nos = [ln for ln, _ in ordered]
        source_text = textwrap.dedent("\n".join(text for _, text in ordered))

    try:
        tree = ast.parse(source_text)
    except SyntaxError:
        return violations

    source_lines = source_text.splitlines()
    all_exports = extract_all_exports(tree)
    is_init_file = Path(rel_path).name == "__init__.py"
    module_loaded, _ = collect_loaded_names(tree)

    def map_line(ast_line: int) -> int:
        if ordered_line_nos is None:
            return ast_line
        if 1 <= ast_line <= len(ordered_line_nos):
            return ordered_line_nos[ast_line - 1]
        return ast_line

    def inspect_stmt_list(stmts: list[ast.stmt]) -> None:
        for idx, stmt in enumerate(stmts):
            term_name = TERMINATOR_NAMES.get(type(stmt))
            if term_name is not None and idx + 1 < len(stmts):
                next_stmt = stmts[idx + 1]
                actual_line = map_line(next_stmt.lineno)
                violations.append(
                    f"[DEAD-CODE] {rel_path}:{actual_line}: Unreachable statement immediately following '{term_name}'"
                )
                break

    for node in ast.walk(tree):
        for attr in ("body", "orelse", "finalbody"):
            seq = getattr(node, attr, None)
            if isinstance(seq, list) and seq and isinstance(seq[0], ast.stmt):
                inspect_stmt_list(seq)

    def walk_scope(
        stmts: list[ast.stmt],
        in_type_checking: bool,
        in_class: bool,
        enclosing_fn: ast.AST | None,
    ) -> None:
        for stmt in stmts:
            if isinstance(stmt, ast.If):
                guard_tc = in_type_checking or is_type_checking_guard(stmt.test)
                walk_scope(stmt.body, guard_tc, in_class, enclosing_fn)
                walk_scope(stmt.orelse, in_type_checking, in_class, enclosing_fn)
                continue

            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                if not is_init_file and not in_type_checking:
                    if isinstance(stmt, ast.ImportFrom) and stmt.module == "__future__":
                        continue
                    scope_loaded = module_loaded
                    if enclosing_fn is not None:
                        scope_loaded, _ = collect_loaded_names(enclosing_fn)
                    stmt_end = getattr(stmt, "end_lineno", stmt.lineno) or stmt.lineno
                    for alias in stmt.names:
                        if alias.name == "*":
                            continue
                        alias_ast_line = getattr(alias, "lineno", None) or stmt.lineno
                        actual_line = map_line(alias_ast_line)
                        if actual_line not in added_lines:
                            continue
                        if line_has_noqa(source_lines, alias_ast_line, alias_ast_line) or line_has_noqa(
                            source_lines, stmt.lineno, stmt_end
                        ):
                            continue
                        if isinstance(stmt, ast.Import):
                            bound_name = alias.asname or alias.name.split(".")[0]
                        else:
                            bound_name = alias.asname or alias.name
                        if bound_name in all_exports or bound_name in scope_loaded:
                            continue
                        violations.append(
                            f"[DEAD-CODE] {rel_path}:{actual_line}: Unused import '{bound_name}'"
                        )
                continue

            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fn_name = stmt.name
                is_private = fn_name.startswith("_") and not (fn_name.startswith("__") and fn_name.endswith("__"))
                if (
                    not in_class
                    and is_private
                    and fn_name != "_"
                    and not stmt.decorator_list
                    and fn_name not in all_exports
                ):
                    other_loaded, other_attrs = collect_loaded_names(tree, exclude_node=stmt)
                    if fn_name not in other_loaded and fn_name not in other_attrs:
                        actual_line = map_line(stmt.lineno)
                        violations.append(
                            f"[DEAD-CODE] {rel_path}:{actual_line}: Unused private helper function '{fn_name}'"
                        )
                walk_scope(stmt.body, in_type_checking, False, stmt)
                continue

            if isinstance(stmt, ast.ClassDef):
                walk_scope(stmt.body, in_type_checking, True, enclosing_fn)
                continue

            for attr in ("body", "orelse", "finalbody"):
                child_seq = getattr(stmt, attr, None)
                if isinstance(child_seq, list) and child_seq and isinstance(child_seq[0], ast.stmt):
                    walk_scope(child_seq, in_type_checking, in_class, enclosing_fn)
            handlers = getattr(stmt, "handlers", None)
            if isinstance(handlers, list):
                for handler in handlers:
                    if isinstance(handler, ast.ExceptHandler):
                        walk_scope(handler.body, in_type_checking, in_class, enclosing_fn)
            cases = getattr(stmt, "cases", None)
            if isinstance(cases, list):
                for case in cases:
                    case_body = getattr(case, "body", None)
                    if isinstance(case_body, list):
                        walk_scope(case_body, in_type_checking, in_class, enclosing_fn)

    walk_scope(tree.body, False, False, None)
    return violations


def collect_git_added_lines(
    repo: Path,
    base_ref: str | None = None,
    file_filter: set[str] | None = None,
    errors: list[str] | None = None,
) -> dict[str, dict[int, str]]:
    """Collect added lines from tracked git diff (relative to base_ref or HEAD) plus untracked source files."""
    if not (repo / ".git").exists():
        if base_ref and errors is not None:
            errors.append(f"[GIT-BASE] invalid base ref '{base_ref}' (not a git repository)")
        return {}

    if base_ref:
        verify_proc = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", f"{base_ref}^{{commit}}"],
            capture_output=True,
            text=True,
        )
        if verify_proc.returncode != 0:
            if errors is not None:
                errors.append(f"[GIT-BASE] invalid base ref '{base_ref}'")
            return {}
        mb_proc = subprocess.run(
            ["git", "-C", str(repo), "merge-base", base_ref, "HEAD"],
            capture_output=True,
            text=True,
        )
        diff_target = mb_proc.stdout.strip() if mb_proc.returncode == 0 and mb_proc.stdout.strip() else base_ref
        diff_cmd = ["git", "-C", str(repo), "diff", diff_target, "--unified=0", "--no-color"]
    else:
        has_head = (
            subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "--verify", "HEAD"],
                capture_output=True,
                text=True,
            ).returncode
            == 0
        )
        diff_cmd = (
            ["git", "-C", str(repo), "diff", "HEAD", "--unified=0", "--no-color"]
            if has_head
            else ["git", "-C", str(repo), "diff", "--cached", "--unified=0", "--no-color"]
        )

    diff_proc = subprocess.run(diff_cmd, capture_output=True, text=True)
    if diff_proc.returncode != 0:
        if base_ref and errors is not None:
            errors.append(f"[GIT-BASE] invalid base ref '{base_ref}' (git diff exited {diff_proc.returncode})")
        return {}
    added_by_file = parse_unified_diff(diff_proc.stdout)

    untracked_proc = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "--others", "--exclude-standard"],
        capture_output=True,
        text=True,
    )
    if untracked_proc.returncode == 0:
        for rel in untracked_proc.stdout.splitlines():
            rel = rel.strip()
            if not rel:
                continue
            p = repo / rel
            if p.suffix.lower() not in SOURCE_EXTENSIONS or not p.is_file() or p.stat().st_size > 512_000:
                continue
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            added_by_file[rel] = {idx + 1: text for idx, text in enumerate(lines)}

    if file_filter is not None:
        added_by_file = {k: v for k, v in added_by_file.items() if k in file_filter}

    return added_by_file


def run_comment_and_dead_code_gates(
    repo: Path,
    allow_tags: tuple[str, ...],
    diff_text: str | None = None,
    base_ref: str | None = None,
    file_filter: set[str] | None = None,
    dead_code_enabled: bool = True,
) -> list[str]:
    """Run deterministic narration-comment and Python AST dead-code checks across diff or workspace changes."""
    violations: list[str] = []
    if diff_text is not None:
        added_by_file = parse_unified_diff(diff_text)
        if file_filter is not None:
            added_by_file = {k: v for k, v in added_by_file.items() if k in file_filter}
        use_disk = False
    else:
        added_by_file = collect_git_added_lines(
            repo,
            base_ref=base_ref,
            file_filter=file_filter,
            errors=violations,
        )
        use_disk = True
    for rel_path, added_lines in sorted(added_by_file.items()):
        if not added_lines:
            continue
        p = Path(rel_path)
        if any(part in IGNORED_DIRS for part in p.parts):
            continue
        suffix = p.suffix.lower()
        if suffix not in SOURCE_EXTENSIONS:
            continue

        disk_file = repo / rel_path
        full_source: str | None = None
        if use_disk and disk_file.is_file() and disk_file.stat().st_size <= 512_000:
            full_source = disk_file.read_text(encoding="utf-8", errors="replace")

        if suffix in PYTHON_EXTENSIONS:
            violations.extend(check_python_comments(rel_path, added_lines, full_source, allow_tags))
            if dead_code_enabled:
                violations.extend(check_python_dead_code(rel_path, added_lines, full_source))
        elif suffix in SHELL_EXTENSIONS:
            violations.extend(check_shell_comments(rel_path, added_lines, allow_tags))
        elif suffix in BRACE_EXTENSIONS:
            violations.extend(check_brace_comments(rel_path, added_lines, full_source, allow_tags))
    return violations


def run_comment_gate(
    repo: Path,
    allow_tags: tuple[str, ...],
    diff_text: str | None = None,
    base_ref: str | None = None,
    file_filter: set[str] | None = None,
) -> list[str]:
    """Run deterministic narration-comment checks without the dead-code gate."""
    return run_comment_and_dead_code_gates(
        repo,
        allow_tags,
        diff_text=diff_text,
        base_ref=base_ref,
        file_filter=file_filter,
        dead_code_enabled=False,
    )


def iter_repo_files(repo: Path) -> list[Path]:
    """Walk non-ignored files in repo for deterministic structural regex checks."""
    matched: list[Path] = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = sorted(d for d in dirs if d not in IGNORED_DIRS)
        for fname in sorted(files):
            fp = Path(root) / fname
            if fp.is_file() and fp.stat().st_size <= 512_000:
                matched.append(fp)
    return matched


def glob_matches(rel_path: str, pattern: str) -> bool:
    """Match relative path or basename against a glob pattern."""
    if fnmatch.fnmatch(rel_path, pattern):
        return True
    basename = Path(rel_path).name
    if "/" not in pattern and fnmatch.fnmatch(basename, pattern):
        return True
    if pattern.startswith("**/") and fnmatch.fnmatch(rel_path, pattern[3:]):
        return True
    return False


def run_codified_checks(
    repo: Path,
    cmd_timeout: int = 30,
    deadline: float | None = None,
    file_filter: set[str] | None = None,
) -> list[str]:
    """Execute structural checks stored in <repo>/.podo/checks.json with optional deadline budget."""
    checks_file = repo / ".podo" / "checks.json"
    if not checks_file.is_file():
        return []

    try:
        data = json.loads(checks_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return [f"[STRUCTURAL-CHECK] Failed to parse .podo/checks.json: {exc}"]

    if not isinstance(data, dict):
        return ["[STRUCTURAL-CHECK] Invalid .podo/checks.json: expected a JSON object"]

    checks = data.get("checks", [])
    if not isinstance(checks, list):
        return ["[STRUCTURAL-CHECK] Invalid .podo/checks.json: 'checks' must be a list"]

    violations: list[str] = []
    repo_files: list[Path] | None = None

    for item in checks:
        if not isinstance(item, dict):
            continue
        check_id = item.get("id", "unnamed-check")
        ctype = item.get("type")
        fix_msg = item.get("fix", "Resolve structural violation")

        if ctype == "forbidden_regex":
            pattern_str = item.get("pattern", "")
            glob_pat = item.get("glob", "*")
            try:
                regex = re.compile(pattern_str)
            except re.error as exc:
                violations.append(f"[STRUCTURAL-CHECK:{check_id}] Invalid regex '{pattern_str}': {exc}")
                continue

            if repo_files is None:
                repo_files = iter_repo_files(repo)

            for fp in repo_files:
                rel_str = fp.relative_to(repo).as_posix()
                if file_filter is not None and rel_str not in file_filter:
                    continue
                if not glob_matches(rel_str, glob_pat):
                    continue
                content = fp.read_text(encoding="utf-8", errors="replace")
                for line_no, line in enumerate(content.splitlines(), start=1):
                    if regex.search(line):
                        violations.append(
                            f"[STRUCTURAL-CHECK:{check_id}] {rel_str}:{line_no} matched '{pattern_str}' -> {fix_msg}"
                        )

        elif ctype == "required_command":
            command = item.get("command", "")
            if not command:
                continue
            effective_timeout = float(cmd_timeout)
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0.2:
                    violations.append(
                        f"[STRUCTURAL-CHECK:{check_id}] Skipped command due to Stop hook deadline budget: {command}"
                    )
                    break
                effective_timeout = min(effective_timeout, remaining)
            try:
                proc = subprocess.run(
                    command,
                    shell=True,
                    cwd=str(repo),
                    capture_output=True,
                    text=True,
                    timeout=effective_timeout,
                )
            except subprocess.TimeoutExpired:
                violations.append(
                    f"[STRUCTURAL-CHECK:{check_id}] Command timed out ({effective_timeout:.1f}s): {command} -> {fix_msg}"
                )
                continue
            if proc.returncode != 0:
                output = (proc.stdout + "\n" + proc.stderr).strip()
                if len(output) > 600:
                    output = output[:600] + "..."
                violations.append(
                    f"[STRUCTURAL-CHECK:{check_id}] Command failed: {command}\n{output}\nFix: {fix_msg}"
                )

    return violations


def run_verify_script(repo: Path) -> list[str]:
    """Run <repo>/.podo/verify.sh if present."""
    verify_script = repo / ".podo" / "verify.sh"
    if not verify_script.is_file():
        return []
    proc = subprocess.run(
        ["bash", str(verify_script)],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        output = (proc.stdout + "\n" + proc.stderr).strip()
        if len(output) > 800:
            output = output[:800] + "..."
        return [f"[VERIFY-SCRIPT] .podo/verify.sh exited with {proc.returncode}:\n{output}"]
    return []


def safe_existing_file(value: str) -> Path | None:
    """Return Path if value names an existing file, avoiding OSError on long inline payloads."""
    if not value or "\n" in value or len(value) > 4096:
        return None
    try:
        candidate = Path(value)
        return candidate if candidate.is_file() else None
    except OSError:
        return None


def resolve_diff_text_arg(arg: str | None) -> str | None:
    """Load unified diff text from stdin ('-'), file path, or inline string."""
    if arg is None:
        return None
    if arg == "-":
        return sys.stdin.read()
    existing = safe_existing_file(arg)
    if existing is not None:
        return existing.read_text(encoding="utf-8")
    return arg


def utc_now_iso() -> str:
    """Return current UTC timestamp formatted in ISO-8601 Zulu form."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso_timestamp(value: str | None) -> float | None:
    """Parse an ISO-8601 timestamp string into POSIX epoch seconds."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def compute_duration_s(started_at: str | None, finished_at: str | None) -> float | None:
    """Return non-negative duration in seconds between two ISO timestamps."""
    t_start = parse_iso_timestamp(started_at)
    t_end = parse_iso_timestamp(finished_at)
    if t_start is None or t_end is None:
        return None
    return round(max(0.0, t_end - t_start), 2)


def format_duration_s(duration_s: float | None) -> str:
    """Format duration in seconds into a compact human-readable label."""
    if duration_s is None:
        return "-"
    if duration_s < 60.0:
        return f"{duration_s:.1f}s"
    total_sec = int(round(duration_s))
    if total_sec < 3600:
        mins, secs = divmod(total_sec, 60)
        return f"{mins}m {secs:02d}s"
    hours, rem = divmod(total_sec, 3600)
    return f"{hours}h {rem // 60:02d}m"


def format_compact_time_range(started_at: str | None, finished_at: str | None) -> str:
    """Format ISO start/finish timestamps compactly while keeping date when bounds cross midnight."""
    if not started_at and not finished_at:
        return "-"

    def split_iso(ts: str | None) -> tuple[str, str]:
        if not ts:
            return "", "…"
        text = ts.strip()
        if "T" in text:
            date_part, rest = text.split("T", 1)
            time_part = rest.rstrip("Z").split("+", 1)[0].split(".", 1)[0]
            return date_part, time_part
        return "", text

    d_start, t_start = split_iso(started_at)
    d_end, t_end = split_iso(finished_at)
    if d_start and d_end and d_start != d_end:
        return f"{d_start[5:]} {t_start} → {d_end[5:]} {t_end} UTC"
    return f"{t_start} → {t_end} UTC"


def sanitize_identifier(raw: str) -> str:
    """Convert an identifier into a filesystem-safe filename token."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", raw.strip())
    return cleaned.strip("._") or "session"


def classify_violation_gate(violations: list[str]) -> str:
    """Classify the deterministic gate category represented by violation lines."""
    if not violations:
        return "all"
    tags: set[str] = set()
    for line in violations:
        if line.startswith("[GIT-BASE]"):
            tags.add("git_base")
        elif line.startswith("[NO-COMMENTS]"):
            tags.add("no_comments")
        elif line.startswith("[DEAD-CODE]"):
            tags.add("dead_code")
        elif line.startswith("[STRUCTURAL-CHECK"):
            tags.add("structural")
        elif line.startswith("[VERIFY-SCRIPT]"):
            tags.add("verify_script")
    return ",".join(sorted(tags)) if tags else "check"


def extract_violation_files(violations: list[str], fallback_files: set[str] | None = None) -> list[str]:
    """Extract sorted relative file paths referenced by violations or fallback filter."""
    found: set[str] = set()
    for line in violations:
        match = VIOLATION_FILE_RE.match(line)
        if match:
            found.add(match.group(1))
    if not found and fallback_files:
        found.update(fallback_files)
    return sorted(found)


def resolve_telemetry_target(
    explicit_path: str | None = None,
    repo: Path | None = None,
    state_path: Path | None = None,
) -> Path | None:
    """Resolve JSONL telemetry target path without auto-creating <repo>/.podo/."""
    if explicit_path and explicit_path.strip():
        return Path(explicit_path.strip()).resolve()
    env_path = os.environ.get("PODO_TELEMETRY_FILE", "").strip()
    if env_path:
        return Path(env_path).resolve()
    if state_path is not None:
        return state_path.resolve().parent / "podo-telemetry.jsonl"
    if repo is not None and (repo / ".podo").is_dir():
        return repo / ".podo" / "telemetry.jsonl"
    return None


def emit_telemetry_event(
    cfg: dict[str, Any],
    target_path: Path | None,
    event: str,
    *,
    conversation_id: str = "",
    phase: str = "",
    worker: str = "",
    role: str = "",
    model: str = "",
    gate: str = "",
    result: str = "",
    violation_count: int = 0,
    files: list[str] | None = None,
    duration_ms: float = 0.0,
) -> bool:
    """Append a structured JSONL telemetry event if telemetry is enabled and target exists."""
    if not is_telemetry_enabled(cfg) or target_path is None:
        return False
    record = {
        "ts": round(time.time(), 3),
        "iso_ts": utc_now_iso(),
        "event": event,
        "conversation_id": conversation_id,
        "phase": phase,
        "worker": worker,
        "role": role,
        "model": model,
        "gate": gate,
        "result": result,
        "violation_count": int(violation_count),
        "files": list(files or []),
        "duration_ms": round(max(0.0, float(duration_ms)), 2),
    }
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with target_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def read_telemetry_events(telemetry_path: Path | None) -> list[dict[str, Any]]:
    """Read structured JSONL telemetry events safely, skipping malformed lines."""
    if telemetry_path is None or not telemetry_path.is_file():
        return []
    events: list[dict[str, Any]] = []
    try:
        lines = telemetry_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def collect_telemetry_paths(
    *,
    explicit_path: Path | None = None,
    repo: Path | None = None,
    state_path: Path | None = None,
    artifact_dir: Path | None = None,
) -> list[Path]:
    """Collect deduplicated existing telemetry JSONL paths for a session."""
    if explicit_path is not None:
        resolved = explicit_path.resolve()
        return [resolved] if resolved.is_file() else []

    env_path = os.environ.get("PODO_TELEMETRY_FILE", "").strip()
    if env_path:
        resolved = Path(env_path).resolve()
        return [resolved] if resolved.is_file() else []

    candidates: list[Path] = []
    if repo is not None:
        candidates.append(repo / ".podo" / "telemetry.jsonl")
    if state_path is not None:
        state_dir = state_path.resolve().parent
        candidates.append(state_dir / "podo-telemetry.jsonl")
        candidates.append(state_dir / "telemetry.jsonl")
    if artifact_dir is not None:
        art_dir = artifact_dir.resolve()
        candidates.append(art_dir / "podo-telemetry.jsonl")
        candidates.append(art_dir / "telemetry.jsonl")

    seen: set[Path] = set()
    existing: list[Path] = []
    for cand in candidates:
        resolved = cand.resolve()
        if resolved not in seen and resolved.is_file():
            seen.add(resolved)
            existing.append(resolved)
    return existing


def read_merged_telemetry_events(paths: list[Path]) -> list[dict[str, Any]]:
    """Read and chronologically merge JSONL events across multiple telemetry files."""
    merged: list[dict[str, Any]] = []
    for path in paths:
        merged.extend(read_telemetry_events(path))
    merged.sort(key=lambda ev: float(ev.get("ts") or 0.0))
    return merged


def extract_tool_call_info(tc: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Extract normalized (tool_name, args_dict) from a transcript tool_call object."""
    fn_obj = tc.get("function") if isinstance(tc.get("function"), dict) else {}
    tool_name = str(tc.get("name") or tc.get("tool_name") or fn_obj.get("name") or "").strip()
    raw_args = tc.get("args") or tc.get("arguments") or fn_obj.get("arguments") or {}
    if isinstance(raw_args, str):
        try:
            raw_args = json.loads(raw_args)
        except json.JSONDecodeError:
            raw_args = {}
    if not isinstance(raw_args, dict):
        raw_args = {}
    return tool_name, raw_args


def summarize_transcript(transcript_path: Path | None) -> dict[str, Any]:
    """Parse transcript.jsonl safely to summarize steps, user turns, and tool call counts."""
    counts: dict[str, int] = {}
    steps = 0
    user_turns = 0
    if transcript_path is not None and transcript_path.is_file():
        try:
            raw_lines = transcript_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            raw_lines = []
        for line in raw_lines:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            steps += 1
            if obj.get("type") == "USER_INPUT":
                user_turns += 1
            tool_calls = obj.get("tool_calls")
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    tool_name, _ = extract_tool_call_info(tc)
                    if tool_name:
                        counts[tool_name] = counts.get(tool_name, 0) + 1
    return {
        "transcript_steps_count": steps,
        "user_turns_count": user_turns,
        "tool_call_counts": dict(sorted(counts.items())),
    }


def infer_node_role(node: dict[str, Any], known_roles: set[str]) -> str:
    """Infer configured role name from explicit node role or worker prefix."""
    explicit = str(node.get("role") or "").strip().lower()
    if explicit in known_roles:
        return explicit
    worker = str(node.get("worker") or "").strip().lower()
    if not worker:
        return "builder"
    if worker in known_roles:
        return worker
    if "arena-a" in worker or "arena_a" in worker:
        return "arena_a"
    if "arena-b" in worker or "arena_b" in worker:
        return "arena_b"
    if worker.startswith("scout"):
        return "scout"
    if worker.startswith("interrogator"):
        return "interrogator"
    if worker.startswith("monitor"):
        return "monitor"
    return "builder"


def collect_historical_interrogator_calls(
    snapshots_dir: Path | None,
    exclude_conversation_id: str = "",
) -> list[int]:
    """Read interrogator_calls counts from prior session snapshots."""
    if snapshots_dir is None or not snapshots_dir.is_dir():
        return []
    exclude_stem = sanitize_identifier(exclude_conversation_id) if exclude_conversation_id else ""
    history: list[int] = []
    for snap_file in sorted(snapshots_dir.glob("*.json")):
        if exclude_stem and snap_file.stem == exclude_stem:
            continue
        try:
            snap = json.loads(snap_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(snap, dict):
            continue
        if exclude_conversation_id and snap.get("conversation_id") == exclude_conversation_id:
            continue
        calls: int | None = None
        tok = snap.get("token_estimates")
        if isinstance(tok, dict):
            by_role = tok.get("by_role")
            if isinstance(by_role, dict):
                ig = by_role.get("interrogator")
                if isinstance(ig, dict) and isinstance(ig.get("calls"), (int, float)):
                    calls = int(ig["calls"])
        if calls is None:
            dag_sum = snap.get("dag_summary")
            if isinstance(dag_sum, dict) and isinstance(dag_sum.get("total_interrogator_calls"), (int, float)):
                calls = int(dag_sum["total_interrogator_calls"])
        if calls is not None and calls >= 0:
            history.append(calls)
    return history


def compute_observability_snapshot(
    *,
    cfg: dict[str, Any],
    state: dict[str, Any] | None = None,
    events: list[dict[str, Any]] | None = None,
    repo: Path | None = None,
    conversation_id: str = "",
    transcript_path: Path | None = None,
    artifact_dir: Path | None = None,
    telemetry_path: Path | None = None,
    snapshot_path: Path | None = None,
    state_path: Path | None = None,
    snapshots_dir: Path | None = None,
) -> dict[str, Any]:
    """Compute a complete observability snapshot combining DAG, telemetry, token estimates, anomalies, and traces."""
    now = utc_now_iso()
    all_events = list(events or [])
    role_avg_tokens = get_role_avg_tokens(cfg)
    thresholds = get_anomaly_thresholds(cfg)
    known_roles = set(role_avg_tokens.keys())

    phases = state.get("phases", []) if isinstance(state, dict) else []
    phase_summaries: list[dict[str, Any]] = []
    total_nodes = 0
    total_verified = 0
    total_rejected = 0
    total_active = 0
    total_pending = 0
    dag_interrogator_calls = 0
    dag_role_calls: dict[str, int] = {}
    last_verdict = "pending"
    phase_durations: list[float] = []

    for ph in phases:
        ph_nodes = ph.get("nodes", [])
        n_total = len(ph_nodes)
        n_verified = sum(1 for n in ph_nodes if n.get("status") == "verified")
        n_rejected = sum(1 for n in ph_nodes if n.get("status") == "rejected")
        n_active = sum(1 for n in ph_nodes if n.get("status") in ACTIVE_DAG_STATUSES)
        n_pending = max(0, n_total - n_verified - n_rejected - n_active)

        total_nodes += n_total
        total_verified += n_verified
        total_rejected += n_rejected
        total_active += n_active
        total_pending += n_pending

        gate_status = str(ph.get("gate_status") or "pending")
        if gate_status in INTERROGATOR_CALL_STATUSES:
            last_verdict = gate_status
        ph_calls = int(ph.get("interrogator_calls") or 0)
        if ph_calls == 0 and gate_status in INTERROGATOR_CALL_STATUSES:
            ph_calls = 1
        dag_interrogator_calls += ph_calls

        for node in ph_nodes:
            if node.get("worker") or node.get("status") != "pending":
                inferred = infer_node_role(node, known_roles)
                dag_role_calls[inferred] = dag_role_calls.get(inferred, 0) + 1

        ph_dur = ph.get("duration_s")
        if ph_dur is None:
            ph_dur = compute_duration_s(ph.get("started_at"), ph.get("finished_at"))
        if isinstance(ph_dur, (int, float)):
            phase_durations.append(float(ph_dur))

        phase_summaries.append(
            {
                "id": str(ph.get("id") or ""),
                "name": str(ph.get("name") or ""),
                "gate_status": gate_status,
                "started_at": ph.get("started_at"),
                "finished_at": ph.get("finished_at"),
                "duration_s": ph_dur,
                "interrogator_calls": ph_calls,
                "node_counts": {
                    "total": n_total,
                    "verified": n_verified,
                    "rejected": n_rejected,
                    "active": n_active,
                    "pending": n_pending,
                },
            }
        )

    if dag_interrogator_calls > 0:
        dag_role_calls["interrogator"] = dag_interrogator_calls

    pass_count = 0
    fail_count = 0
    observe_count = 0
    by_gate: dict[str, dict[str, int]] = {}
    by_phase: dict[str, dict[str, int]] = {}
    by_worker: dict[str, dict[str, int]] = {}
    event_role_calls: dict[str, int] = {}
    event_phases: set[str] = set()

    def bump_bucket(table: dict[str, dict[str, int]], key: str, outcome: str) -> None:
        bucket = table.setdefault(key, {"pass": 0, "fail": 0, "observe": 0, "total": 0})
        bucket[outcome] += 1
        bucket["total"] += 1

    for ev in all_events:
        ev_type = str(ev.get("event") or "")
        ev_result = str(ev.get("result") or "")
        ev_role = str(ev.get("role") or "").strip().lower()
        ev_phase = str(ev.get("phase") or "").strip()
        ev_worker = str(ev.get("worker") or "").strip()
        ev_gate = str(ev.get("gate") or "").strip()

        if ev_phase:
            event_phases.add(ev_phase)
        if ev_role in known_roles:
            if ev_type in {"check", "hook_stop", "dag_update"}:
                event_role_calls[ev_role] = event_role_calls.get(ev_role, 0) + 1
        if ev_type == "dag_update" and ev_gate == "interrogator_gate" and ev_result in INTERROGATOR_CALL_STATUSES:
            last_verdict = ev_result

        if ev_type in {"check", "hook_stop"} and ev_result in {"pass", "fail", "observe"}:
            if ev_result == "pass":
                pass_count += 1
            elif ev_result == "fail":
                fail_count += 1
            else:
                observe_count += 1
            bump_bucket(by_gate, ev_gate or "all", ev_result)
            bump_bucket(by_phase, ev_phase or "unassigned", ev_result)
            bump_bucket(by_worker, ev_worker or "unassigned", ev_result)

    total_gate_runs = pass_count + fail_count + observe_count
    failure_rate = round(fail_count / total_gate_runs, 4) if total_gate_runs > 0 else 0.0

    by_role: dict[str, dict[str, int]] = {}
    all_seen_roles = sorted(set(dag_role_calls.keys()) | set(event_role_calls.keys()))
    for role_name in all_seen_roles:
        calls = max(dag_role_calls.get(role_name, 0), event_role_calls.get(role_name, 0))
        if calls <= 0:
            continue
        avg_tok = int(role_avg_tokens.get(role_name, 0))
        by_role[role_name] = {
            "calls": calls,
            "avg_tokens": avg_tok,
            "estimated_tokens": calls * avg_tok,
        }

    total_estimated_tokens = sum(item["estimated_tokens"] for item in by_role.values())
    interrogator_avg = int(role_avg_tokens.get("interrogator", 25000))
    batched_saved = max(0, total_nodes - len(phases)) if phases else 0
    tier1_saved_tokens = (fail_count + observe_count + batched_saved) * interrogator_avg

    phase_divisor = max(1, len(phases) if phases else len(event_phases))
    interrogator_calls_total = by_role.get("interrogator", {}).get("calls", 0)
    anomalies: list[dict[str, Any]] = []

    churn_threshold = thresholds["interrogator_calls_per_phase_warn"]
    if interrogator_calls_total > 0 and (interrogator_calls_total / phase_divisor) >= churn_threshold:
        ratio = round(interrogator_calls_total / phase_divisor, 2)
        anomalies.append(
            {
                "code": "HIGH_INTERROGATOR_CHURN",
                "severity": "warn",
                "value": ratio,
                "threshold": churn_threshold,
                "message": f"Interrogator churn {ratio:.2f} calls/phase >= {churn_threshold}",
            }
        )

    fail_threshold = thresholds["gate_fail_rate_warn"]
    if total_gate_runs > 0 and failure_rate >= fail_threshold:
        anomalies.append(
            {
                "code": "HIGH_GATE_FAIL_RATE",
                "severity": "warn",
                "value": failure_rate,
                "threshold": fail_threshold,
                "message": f"Gate fail rate {failure_rate:.2%} >= {fail_threshold:.2%}",
            }
        )

    resolved_snapshots_dir = snapshots_dir
    if resolved_snapshots_dir is None and repo is not None and (repo / ".podo" / "snapshots").is_dir():
        resolved_snapshots_dir = repo / ".podo" / "snapshots"

    prior_calls = collect_historical_interrogator_calls(
        resolved_snapshots_dir,
        exclude_conversation_id=conversation_id,
    )
    if prior_calls:
        historical_avg = sum(prior_calls) / len(prior_calls)
        mult_threshold = thresholds["interrogator_history_multiplier_warn"]
        if historical_avg > 0 and interrogator_calls_total >= mult_threshold * historical_avg:
            anomalies.append(
                {
                    "code": "INTERROGATOR_SPIKE_VS_HISTORY",
                    "severity": "warn",
                    "value": interrogator_calls_total,
                    "historical_avg": round(historical_avg, 2),
                    "threshold": mult_threshold,
                    "message": (
                        f"Interrogator calls ({interrogator_calls_total}) >= "
                        f"{mult_threshold}x historical avg ({historical_avg:.2f})"
                    ),
                }
            )

    transcript_stats = summarize_transcript(transcript_path)
    trace_link = {
        "conversation_id": conversation_id,
        "updated_at": now,
        "transcript_path": str(transcript_path) if transcript_path else "",
        "artifact_dir": str(artifact_dir) if artifact_dir else "",
        "telemetry_file": str(telemetry_path) if telemetry_path else "",
        "snapshot_file": str(snapshot_path) if snapshot_path else "",
        "dag_state_file": str(state_path) if state_path else "",
        "transcript_steps_count": transcript_stats["transcript_steps_count"],
        "user_turns_count": transcript_stats["user_turns_count"],
        "tool_call_counts": transcript_stats["tool_call_counts"],
    }

    return {
        "conversation_id": conversation_id,
        "updated_at": now,
        "dag_summary": {
            "title": str(state.get("title") or "") if isinstance(state, dict) else "",
            "phase_count": len(phases),
            "phases": phase_summaries,
            "node_counts": {
                "total": total_nodes,
                "verified": total_verified,
                "rejected": total_rejected,
                "active": total_active,
                "pending": total_pending,
            },
            "total_duration_s": round(sum(phase_durations), 2) if phase_durations else None,
            "total_interrogator_calls": interrogator_calls_total,
            "last_interrogator_verdict": last_verdict,
        },
        "gate_metrics": {
            "total": total_gate_runs,
            "pass": pass_count,
            "fail": fail_count,
            "observe": observe_count,
            "failure_rate": failure_rate,
            "by_gate": by_gate,
            "by_phase": by_phase,
            "by_worker": by_worker,
        },
        "token_estimates": {
            "by_role": by_role,
            "total_estimated_tokens": total_estimated_tokens,
            "tier1_saved_tokens": tier1_saved_tokens,
            "anomalies": anomalies,
        },
        "trace_link": trace_link,
    }


def update_trace_index(index_path: Path, trace_entry: dict[str, Any]) -> None:
    """Upsert a session trace entry inside trace-index.json."""
    if index_path.is_file():
        try:
            data = json.loads(index_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {"sessions": {}}
        except (json.JSONDecodeError, OSError):
            data = {"sessions": {}}
    else:
        data = {"sessions": {}}
    sessions = data.setdefault("sessions", {})
    if not isinstance(sessions, dict):
        sessions = {}
        data["sessions"] = sessions
    session_key = str(trace_entry.get("conversation_id") or "").strip() or "session"
    sessions[session_key] = trace_entry
    data["updated_at"] = trace_entry.get("updated_at") or utc_now_iso()
    try:
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError:
        pass


def discover_observability_paths(
    repo: Path,
    state_file_arg: str | None = None,
    telemetry_file_arg: str | None = None,
    artifact_dir_arg: str | None = None,
) -> tuple[Path | None, Path | None, Path | None]:
    """Resolve (state_path, telemetry_path, artifact_dir) from explicit args or repo defaults."""
    artifact_dir = Path(artifact_dir_arg).resolve() if artifact_dir_arg else None

    state_path: Path | None = None
    if state_file_arg:
        state_path = Path(state_file_arg).resolve()
    elif artifact_dir and (artifact_dir / "podo-dag.state.json").is_file():
        state_path = artifact_dir / "podo-dag.state.json"
    elif (repo / ".podo" / "podo-dag.state.json").is_file():
        state_path = repo / ".podo" / "podo-dag.state.json"

    telemetry_path: Path | None = None
    if telemetry_file_arg:
        telemetry_path = Path(telemetry_file_arg).resolve()
    elif os.environ.get("PODO_TELEMETRY_FILE", "").strip():
        telemetry_path = Path(os.environ["PODO_TELEMETRY_FILE"].strip()).resolve()
    elif state_path and (state_path.parent / "podo-telemetry.jsonl").is_file():
        telemetry_path = state_path.parent / "podo-telemetry.jsonl"
    elif (repo / ".podo" / "telemetry.jsonl").is_file():
        telemetry_path = repo / ".podo" / "telemetry.jsonl"
    elif artifact_dir and (artifact_dir / "podo-telemetry.jsonl").is_file():
        telemetry_path = artifact_dir / "podo-telemetry.jsonl"

    if state_path is None and telemetry_path and (telemetry_path.parent / "podo-dag.state.json").is_file():
        state_path = telemetry_path.parent / "podo-dag.state.json"

    return state_path, telemetry_path, artifact_dir


def load_json_dict(path: Path | None) -> dict[str, Any] | None:
    """Read a JSON object from disk, returning None on absence or parse failure."""
    if path is None or not path.is_file():
        return None
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
        return parsed if isinstance(parsed, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def cmd_check(args: argparse.Namespace) -> int:
    """Execute deterministic comment, AST dead-code, structural check, and optional verify.sh gates."""
    t0 = time.monotonic()
    repo = Path(args.repo).resolve()
    cfg = load_config(Path(args.config).resolve() if args.config else DEFAULT_CONFIG_PATH)
    allow_tags = get_allow_tags(cfg)
    dead_code_enabled = is_dead_code_check_enabled(cfg)
    diff_text = resolve_diff_text_arg(args.diff_text)
    file_filter = parse_files_csv(args.files, repo)

    violations: list[str] = []
    violations.extend(
        run_comment_and_dead_code_gates(
            repo,
            allow_tags,
            diff_text=diff_text,
            base_ref=args.base,
            file_filter=file_filter,
            dead_code_enabled=dead_code_enabled,
        )
    )
    violations.extend(run_codified_checks(repo, file_filter=file_filter))
    if args.run_verify:
        violations.extend(run_verify_script(repo))

    has_git_base_error = any(v.startswith("[GIT-BASE]") for v in violations)
    if not violations:
        outcome = "pass"
        exit_code = 0
    elif args.mode == "observe" and not has_git_base_error:
        outcome = "observe"
        exit_code = 0
    else:
        outcome = "fail"
        exit_code = 1

    phase = (args.phase or os.environ.get("PODO_PHASE", "")).strip()
    worker = (args.worker or os.environ.get("PODO_WORKER", "")).strip()
    role = (args.role or os.environ.get("PODO_ROLE", "")).strip().lower()
    conv_id = os.environ.get("PODO_CONVERSATION_ID", "").strip()
    telemetry_target = resolve_telemetry_target(explicit_path=args.telemetry_file, repo=repo)
    emit_telemetry_event(
        cfg,
        telemetry_target,
        "check",
        conversation_id=conv_id,
        phase=phase,
        worker=worker,
        role=role,
        model=get_role_model(cfg, role),
        gate=classify_violation_gate(violations),
        result=outcome,
        violation_count=len(violations),
        files=extract_violation_files(violations, fallback_files=file_filter),
        duration_ms=(time.monotonic() - t0) * 1000.0,
    )

    if violations:
        if outcome == "observe":
            print(f"[podo-guard] OBSERVE (non-blocking): {len(violations)} violation(s) detected:")
            for line in violations:
                print(line)
            return 0
        for line in violations:
            print(line)
        return exit_code

    print("[podo-guard] OK: 0 violations (no-comments, dead-code & structural checks passed).")
    return 0


def cmd_codify(args: argparse.Namespace) -> int:
    """Record a deterministic structural check in <repo>/.podo/checks.json."""
    t0 = time.monotonic()
    if not args.regex and not args.cmd:
        print("Error: specify either --regex or --cmd", file=sys.stderr)
        return 2

    repo = Path(args.repo).resolve()
    cfg = load_config(Path(args.config).resolve() if args.config else DEFAULT_CONFIG_PATH)
    podo_dir = repo / ".podo"
    podo_dir.mkdir(parents=True, exist_ok=True)
    checks_file = podo_dir / "checks.json"

    if checks_file.is_file():
        try:
            data = json.loads(checks_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"Error: failed to read existing {checks_file}: {exc}", file=sys.stderr)
            return 2
    else:
        data = {"checks": []}

    entry: dict[str, Any] = {
        "id": args.id,
        "fix": args.fix,
        "added_at": datetime.now(timezone.utc).isoformat(),
    }
    if args.regex:
        entry["type"] = "forbidden_regex"
        entry["glob"] = args.glob or "*"
        entry["pattern"] = args.regex
    else:
        entry["type"] = "required_command"
        entry["command"] = args.cmd

    checks = [c for c in data.get("checks", []) if c.get("id") != args.id]
    checks.append(entry)
    data["checks"] = checks
    checks_file.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    telemetry_target = resolve_telemetry_target(explicit_path=getattr(args, "telemetry_file", None), repo=repo)
    emit_telemetry_event(
        cfg,
        telemetry_target,
        "codify",
        conversation_id=os.environ.get("PODO_CONVERSATION_ID", "").strip(),
        phase=os.environ.get("PODO_PHASE", "").strip(),
        worker=os.environ.get("PODO_WORKER", "").strip(),
        role=os.environ.get("PODO_ROLE", "").strip().lower(),
        gate="structural",
        result="ok",
        violation_count=0,
        files=[".podo/checks.json"],
        duration_ms=(time.monotonic() - t0) * 1000.0,
    )
    print(f"[podo-guard] Codified structural check '{args.id}' into {checks_file}")
    return 0


def apply_status_timestamps(
    item: dict[str, Any],
    status: str | None,
    now: str,
    started_override: str | None = None,
    finished_override: str | None = None,
    fallback_started_at: str | None = None,
) -> None:
    """Update started_at, finished_at, and duration_s on a DAG phase or node."""
    if started_override:
        item["started_at"] = started_override
    if finished_override:
        item["finished_at"] = finished_override

    if status in ACTIVE_DAG_STATUSES:
        if not item.get("started_at"):
            item["started_at"] = fallback_started_at or now
        if not finished_override:
            item["finished_at"] = None
            item["duration_s"] = None
    elif status in TERMINAL_DAG_STATUSES:
        if not item.get("started_at"):
            item["started_at"] = fallback_started_at or now
        if not item.get("finished_at"):
            item["finished_at"] = now

    item["duration_s"] = compute_duration_s(item.get("started_at"), item.get("finished_at"))


def normalize_phases(raw: Any, now: str | None = None) -> list[dict[str, Any]]:
    """Normalize input phase specification into a consistent schema with timing metadata."""
    phases = raw.get("phases", []) if isinstance(raw, dict) else raw
    if not isinstance(phases, list):
        raise ValueError("phases JSON must be a list or an object with a 'phases' list")
    ts_now = now or utc_now_iso()

    normalized: list[dict[str, Any]] = []
    for idx, ph in enumerate(phases, start=1):
        ph_id = str(ph.get("id") or f"phase-{idx}")
        ph_name = str(ph.get("name") or ph.get("title") or f"Phase {idx}")
        gate_status = str(ph.get("gate_status") or "pending")
        gate_note = str(ph.get("gate_note") or "")
        ph_started = ph.get("started_at") or None
        ph_finished = ph.get("finished_at") or None
        ph_calls = int(ph.get("interrogator_calls") or 0)
        if ph_calls == 0 and gate_status in INTERROGATOR_CALL_STATUSES:
            ph_calls = 1

        nodes: list[dict[str, Any]] = []
        for nidx, node in enumerate(ph.get("nodes", []), start=1):
            n_status = str(node.get("status") or "pending")
            n_started = node.get("started_at") or None
            n_finished = node.get("finished_at") or None
            node_entry: dict[str, Any] = {
                "id": str(node.get("id") or f"{ph_id}-task-{nidx}"),
                "title": str(node.get("title") or node.get("id") or f"Task {nidx}"),
                "worker": str(node.get("worker") or ""),
                "role": str(node.get("role") or ""),
                "model": str(node.get("model") or ""),
                "files": [str(f) for f in node.get("files", [])],
                "depends_on": [str(d) for d in node.get("depends_on", [])],
                "status": n_status,
                "note": str(node.get("note") or ""),
                "started_at": n_started,
                "finished_at": n_finished,
                "duration_s": node.get("duration_s"),
            }
            apply_status_timestamps(node_entry, n_status, ts_now)
            if node_entry["started_at"] and not ph_started:
                ph_started = node_entry["started_at"]
            nodes.append(node_entry)

        phase_entry: dict[str, Any] = {
            "id": ph_id,
            "name": ph_name,
            "gate_status": gate_status,
            "gate_note": gate_note,
            "started_at": ph_started,
            "finished_at": ph_finished,
            "duration_s": ph.get("duration_s"),
            "interrogator_calls": ph_calls,
            "nodes": nodes,
        }
        apply_status_timestamps(phase_entry, gate_status, ts_now)
        normalized.append(phase_entry)
    return normalized


def compute_gantt_rows(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Compute relative horizontal Gantt timing bars across the DAG session window."""
    phases = state.get("phases", [])
    timestamps: list[float] = []
    for ph in phases:
        for field in ("started_at", "finished_at"):
            parsed = parse_iso_timestamp(ph.get(field))
            if parsed is not None:
                timestamps.append(parsed)
        for node in ph.get("nodes", []):
            for field in ("started_at", "finished_at"):
                parsed = parse_iso_timestamp(node.get(field))
                if parsed is not None:
                    timestamps.append(parsed)
    if not timestamps:
        for key in ("created_at", "updated_at"):
            parsed = parse_iso_timestamp(state.get(key))
            if parsed is not None:
                timestamps.append(parsed)

    window_start = min(timestamps) if timestamps else 0.0
    window_end = max(timestamps) if timestamps else 1.0
    span = max(1.0, window_end - window_start)

    def make_row(
        kind: str,
        row_id: str,
        label: str,
        status: str,
        started_at: str | None,
        finished_at: str | None,
        duration_s: float | None,
        worker: str = "",
        title: str = "",
    ) -> dict[str, Any]:
        t_start = parse_iso_timestamp(started_at)
        t_end = parse_iso_timestamp(finished_at)
        resolved_dur = duration_s
        if resolved_dur is None and t_start is not None and t_end is not None:
            resolved_dur = round(max(0.0, t_end - t_start), 2)
        if t_start is not None:
            effective_end = max(t_start, t_end if t_end is not None else window_end)
            left_pct = round(max(0.0, min(98.0, ((t_start - window_start) / span) * 100.0)), 2)
            raw_width = ((effective_end - t_start) / span) * 100.0
            width_pct = round(max(2.0, min(100.0 - left_pct, raw_width)), 2)
        else:
            left_pct = 0.0
            width_pct = 0.0
        return {
            "kind": kind,
            "id": row_id,
            "label": label,
            "title": title,
            "worker": worker,
            "status": status,
            "started_at": started_at or "-",
            "finished_at": finished_at or "-",
            "compact_range": format_compact_time_range(started_at, finished_at),
            "full_range": f"{started_at or '-'} → {finished_at or '-'}",
            "duration_s": resolved_dur,
            "duration_label": format_duration_s(resolved_dur),
            "left_pct": left_pct,
            "width_pct": width_pct,
        }

    rows: list[dict[str, Any]] = []
    for ph in phases:
        rows.append(
            make_row(
                "phase",
                str(ph.get("id") or ""),
                str(ph.get("name") or ph.get("id") or ""),
                str(ph.get("gate_status") or "pending"),
                ph.get("started_at"),
                ph.get("finished_at"),
                ph.get("duration_s"),
                worker="",
                title="",
            )
        )
        for node in ph.get("nodes", []):
            worker_name = str(node.get("worker") or "")
            node_title = str(node.get("title") or "")
            worker_suffix = f" (@{worker_name})" if worker_name else ""
            rows.append(
                make_row(
                    "node",
                    str(node.get("id") or ""),
                    f"{node.get('id')}: {node_title}{worker_suffix}",
                    str(node.get("status") or "pending"),
                    node.get("started_at"),
                    node.get("finished_at"),
                    node.get("duration_s"),
                    worker=worker_name,
                    title=node_title,
                )
            )
    return rows


def gantt_bar_color_var(status: str) -> str:
    """Map DAG node/phase status to an Antigravity semantic CSS color variable."""
    if status == "verified":
        return "var(--success)"
    if status == "rejected":
        return "var(--error)"
    if status in {"arena_running", "phase_interrogating"}:
        return "var(--warning)"
    if status in {"building", "script_checked"}:
        return "var(--primary)"
    return "var(--muted-foreground)"


def render_dag_markdown(
    state: dict[str, Any],
    cfg: dict[str, Any] | None = None,
    observability: dict[str, Any] | None = None,
) -> str:
    """Render DAG state into Markdown with Mermaid flowchart, Gantt timeline, and observability panel."""
    resolved_cfg = cfg if cfg is not None else load_config(DEFAULT_CONFIG_PATH)
    interrogator_model = get_interrogator_model(resolved_cfg)
    obs = observability or compute_observability_snapshot(cfg=resolved_cfg, state=state)
    lines = [
        f"# Podo Swarm DAG — {state['title']}",
        "",
        f"- **Updated**: `{state['updated_at']}`",
        "- **Tier 1 (Per-Task)**: `podo-guard.py check` (0 tokens)",
        f"- **Tier 2 (Per-Phase Batch)**: Interrogator Gate (`{interrogator_model}`)",
        "",
        "```mermaid",
        "flowchart LR",
    ]

    phases = state.get("phases", [])
    prev_phase_last_gate: str | None = None
    for ph in phases:
        gate_node = f"{ph['id']}_gate"
        gate_badge = MERMAID_STATUS_BADGE.get(ph["gate_status"], ph["gate_status"])
        lines.append(f"  subgraph {ph['id']}[\"{ph['name']}\"]")
        for node in ph.get("nodes", []):
            badge = MERMAID_STATUS_BADGE.get(node["status"], node["status"])
            safe_title = node["title"].replace('"', "'")
            worker_label = f" ({node['worker']})" if node.get("worker") else ""
            lines.append(f"    {node['id']}[\"{safe_title}{worker_label}<br/>{badge}\"]")
        lines.append(f"    {gate_node}{{\"Interrogator Gate<br/>{gate_badge}\"}}")
        lines.append("  end")

        for node in ph.get("nodes", []):
            for dep in node.get("depends_on", []):
                lines.append(f"  {dep} --> {node['id']}")
            if prev_phase_last_gate and not node.get("depends_on"):
                lines.append(f"  {prev_phase_last_gate} --> {node['id']}")
            lines.append(f"  {node['id']} --> {gate_node}")
        prev_phase_last_gate = gate_node

    lines.extend(
        [
            "```",
            "",
            "| Phase | Task | Worker | Model | Status | Duration | Note |",
            "| :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
        ]
    )

    for ph in phases:
        for node in ph.get("nodes", []):
            badge = MERMAID_STATUS_BADGE.get(node["status"], node["status"])
            dur_str = format_duration_s(node.get("duration_s"))
            lines.append(
                f"| {ph['name']} | `{node['id']}` {node['title']} | `{node.get('worker') or '-'}` | `{node.get('model') or '-'}` | {badge} | `{dur_str}` | {node.get('note') or '-'} |"
            )
        gate_badge = MERMAID_STATUS_BADGE.get(ph["gate_status"], ph["gate_status"])
        ph_dur_str = format_duration_s(ph.get("duration_s"))
        lines.append(
            f"| {ph['name']} | **Interrogator Gate** | `interrogator` | `{interrogator_model}` | {gate_badge} | `{ph_dur_str}` | {ph.get('gate_note') or '-'} |"
        )

    gantt_rows = compute_gantt_rows(state)
    lines.extend(
        [
            "",
            "## Phase & Worker Timeline (Gantt View)",
            "",
            "| Scope | Status | Started | Finished | Duration | Window |",
            "| :--- | :--- | :--- | :--- | :--- | :--- |",
        ]
    )
    for row in gantt_rows:
        prefix = "**" if row["kind"] == "phase" else "↳ "
        suffix = "**" if row["kind"] == "phase" else ""
        window_str = (
            f"`{row['left_pct']:.1f}%..{min(100.0, row['left_pct'] + row['width_pct']):.1f}%`"
            if row["width_pct"] > 0
            else "-"
        )
        lines.append(
            f"| {prefix}{row['label']}{suffix} | `{row['status']}` | `{row['started_at']}` | `{row['finished_at']}` | `{row['duration_label']}` | {window_str} |"
        )

    tok_est = obs.get("token_estimates", {})
    by_role = tok_est.get("by_role", {})
    anomalies = tok_est.get("anomalies", [])
    gate_metrics = obs.get("gate_metrics", {})

    lines.extend(
        [
            "",
            "## Observability & Token Estimate",
            "",
            f"- **Estimated Total Tokens**: `{tok_est.get('total_estimated_tokens', 0):,}`",
            f"- **Tier-1 Saved Token Estimate**: `{tok_est.get('tier1_saved_tokens', 0):,}`",
            (
                f"- **Gate Checks**: `{gate_metrics.get('total', 0)}` "
                f"(pass=`{gate_metrics.get('pass', 0)}`, fail=`{gate_metrics.get('fail', 0)}`, "
                f"observe=`{gate_metrics.get('observe', 0)}`, fail_rate=`{gate_metrics.get('failure_rate', 0.0):.2%}`)"
            ),
            "",
            "| Role | Calls | Avg Tokens/Call | Estimated Tokens |",
            "| :--- | :--- | :--- | :--- |",
        ]
    )
    if by_role:
        for role_name, info in sorted(by_role.items()):
            lines.append(
                f"| `{role_name}` | `{info.get('calls', 0)}` | `{info.get('avg_tokens', 0):,}` | `{info.get('estimated_tokens', 0):,}` |"
            )
    else:
        lines.append("| `-` | `0` | `0` | `0` |")

    if anomalies:
        lines.append("")
        lines.append("### Anomaly Alerts")
        for alert in anomalies:
            lines.append(f"- **{alert.get('code')}**: {alert.get('message')}")

    lines.append("")
    return "\n".join(lines)


def render_dag_html(
    state: dict[str, Any],
    cfg: dict[str, Any] | None = None,
    observability: dict[str, Any] | None = None,
) -> str:
    """Render DAG state into a self-contained offline HTML dashboard with Gantt timeline and observability."""
    resolved_cfg = cfg if cfg is not None else load_config(DEFAULT_CONFIG_PATH)
    interrogator_model = get_interrogator_model(resolved_cfg)
    obs = observability or compute_observability_snapshot(cfg=resolved_cfg, state=state)
    phases = state.get("phases", [])
    total_nodes = sum(len(ph.get("nodes", [])) for ph in phases)
    verified_nodes = sum(
        1 for ph in phases for n in ph.get("nodes", []) if n.get("status") == "verified"
    )
    rejected_nodes = sum(
        1 for ph in phases for n in ph.get("nodes", []) if n.get("status") == "rejected"
    )
    dag_summary = obs.get("dag_summary", {})
    total_duration_label = format_duration_s(dag_summary.get("total_duration_s"))

    phase_cards_html: list[str] = []
    for ph in phases:
        gate_label, gate_cls = STATUS_LABELS.get(
            ph.get("gate_status", "pending"),
            (ph.get("gate_status", "pending"), "badge-pending"),
        )
        node_cards: list[str] = []
        for node in ph.get("nodes", []):
            st_label, st_cls = STATUS_LABELS.get(
                node.get("status", "pending"),
                (node.get("status", "pending"), "badge-pending"),
            )
            deps_html = "".join(
                f'<span class="chip chip-dep">← {html.escape(d)}</span>'
                for d in node.get("depends_on", [])
            )
            files_html = "".join(
                f'<span class="chip chip-file">{html.escape(f)}</span>'
                for f in node.get("files", [])
            )
            note_html = (
                f'<div class="node-note">{html.escape(node["note"])}</div>'
                if node.get("note")
                else ""
            )
            dur_str = format_duration_s(node.get("duration_s"))
            worker_disp = html.escape(node.get("worker") or "unassigned")
            model_disp = html.escape(node.get("model") or "inherit")
            chips_row = f'<div class="chips">{deps_html}{files_html}</div>' if (deps_html or files_html) else ""
            node_cards.append(
                f"""
                <div class="node-card">
                  <div class="node-top">
                    <div class="min-w-0">
                      <div class="node-id mono">{html.escape(node["id"])}</div>
                      <div class="node-title">{html.escape(node["title"])}</div>
                    </div>
                    <span class="badge {st_cls}"><span class="dot"></span>{html.escape(st_label)}</span>
                  </div>
                  <div class="node-meta-line">
                    <span class="meta-worker mono">@{worker_disp}</span>
                    <span class="meta-sep">·</span>
                    <span>{html.escape(dur_str)}</span>
                    <span class="meta-sep">·</span>
                    <span class="mono">{model_disp}</span>
                  </div>
                  {chips_row}
                  {note_html}
                </div>
                """
            )

        gate_note_html = (
            f'<div class="gate-note">{html.escape(ph["gate_note"])}</div>'
            if ph.get("gate_note")
            else ""
        )
        ph_dur_str = format_duration_s(ph.get("duration_s"))
        ph_calls = int(ph.get("interrogator_calls") or 0)
        phase_cards_html.append(
            f"""
            <section class="phase-card">
              <div class="phase-header">
                <div class="phase-title-group">
                  <span class="phase-id">{html.escape(ph["id"])}</span>
                  <h2 class="phase-name">{html.escape(ph["name"])}</h2>
                  <span class="dur-pill mono">{html.escape(ph_dur_str)}</span>
                </div>
                <div class="gate-group">
                  <span class="muted">Interrogator Gate ({html.escape(interrogator_model)} · {ph_calls}x)</span>
                  <span class="badge {gate_cls}"><span class="dot"></span>{html.escape(gate_label)}</span>
                </div>
              </div>
              <div class="nodes-grid">
                {"".join(node_cards)}
              </div>
              {gate_note_html}
            </section>
            """
        )

    gantt_rows = compute_gantt_rows(state)
    gantt_rows_html: list[str] = []
    for row in gantt_rows:
        is_phase = row["kind"] == "phase"
        item_cls = "gantt-item gantt-phase" if is_phase else "gantt-item gantt-node"
        bar_color = gantt_bar_color_var(row["status"])
        bar_html = (
            f'<div class="gantt-bar" style="left: {row["left_pct"]}%; width: {row["width_pct"]}%; background: {bar_color};"></div>'
            if row["width_pct"] > 0
            else '<div class="gantt-bar-empty"></div>'
        )
        tag_html = (
            '<span class="gantt-tag gantt-tag-phase">PHASE</span>'
            if is_phase
            else (
                f'<span class="gantt-tag">@{html.escape(row["worker"])}</span>'
                if row.get("worker")
                else '<span class="gantt-tag">TASK</span>'
            )
        )
        head_label = row["label"] if is_phase else row["id"]
        sub_html = f'<div class="gantt-sub muted">{html.escape(row["title"])}</div>' if row.get("title") else ""
        gantt_rows_html.append(
            f"""
            <div class="{item_cls}">
              <div class="gantt-head">
                <div class="gantt-title-wrap">
                  {tag_html}
                  <span class="gantt-label">{html.escape(head_label)}</span>
                </div>
                <div class="gantt-meta">
                  <span class="gantt-range mono muted" title="{html.escape(row["full_range"])}">{html.escape(row["compact_range"])}</span>
                  <span class="dur-pill mono">{html.escape(row["duration_label"])}</span>
                </div>
              </div>
              {sub_html}
              <div class="gantt-track" aria-label="Timeline window">
                {bar_html}
              </div>
            </div>
            """
        )

    tok_est = obs.get("token_estimates", {})
    by_role = tok_est.get("by_role", {})
    total_estimated = int(tok_est.get("total_estimated_tokens", 0))
    tier1_saved = int(tok_est.get("tier1_saved_tokens", 0))
    anomalies = tok_est.get("anomalies", [])
    gate_metrics = obs.get("gate_metrics", {})

    role_colors = {
        "builder": "var(--primary)",
        "interrogator": "var(--warning)",
        "scout": "var(--success)",
        "arena_a": "var(--primary)",
        "arena_b": "var(--secondary-foreground)",
        "monitor": "var(--muted-foreground)",
    }

    role_share_segments: list[str] = []
    role_legend_items: list[str] = []
    role_rows_html: list[str] = []
    for role_name, info in sorted(by_role.items()):
        est_tok = int(info.get("estimated_tokens", 0))
        share_pct = (est_tok / total_estimated * 100.0) if total_estimated > 0 else 0.0
        seg_color = role_colors.get(role_name, "var(--secondary-foreground)")
        if share_pct > 0:
            role_share_segments.append(
                f'<div class="share-seg" style="width: {share_pct:.2f}%; background: {seg_color};" title="{html.escape(role_name)}: {share_pct:.1f}%"></div>'
            )
        role_legend_items.append(
            f'<span class="legend-chip"><span class="legend-dot" style="background: {seg_color};"></span><span class="mono">{html.escape(role_name)}</span> <span class="muted mono">{share_pct:.0f}%</span></span>'
        )
        role_rows_html.append(
            f"""
            <tr>
              <td class="mono">{html.escape(role_name)}</td>
              <td class="mono num">{int(info.get("calls", 0))}</td>
              <td class="mono num">{int(info.get("avg_tokens", 0)):,}</td>
              <td class="mono num">{est_tok:,}</td>
              <td class="mono num muted">{share_pct:.1f}%</td>
            </tr>
            """
        )
    if not role_rows_html:
        role_rows_html.append('<tr><td colspan="5" class="muted">No role invocations recorded yet.</td></tr>')

    share_bar_html = (
        f"""
        <div class="share-wrap">
          <div class="share-bar">{"".join(role_share_segments)}</div>
          <div class="share-legend">{"".join(role_legend_items)}</div>
        </div>
        """
        if role_share_segments
        else ""
    )

    if anomalies:
        anomalies_html = "".join(
            f'<div class="alert-item"><strong>{html.escape(str(a.get("code", "")))}</strong>: {html.escape(str(a.get("message", "")))}</div>'
            for a in anomalies
        )
    else:
        anomalies_html = '<div class="alert-ok">No anomalies detected.</div>'

    rejected_kpi_html = (
        f"""
        <div class="kpi-card">
          <span class="kpi-label">Rejected</span>
          <span class="kpi-val kpi-error">{rejected_nodes}</span>
        </div>
        """
        if rejected_nodes > 0
        else ""
    )

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Podo Swarm DAG — {html.escape(state["title"])}</title>
  <style>
    :root {{
      --background: #101013;
      --card: #17171c;
      --foreground: #f2f4f6;
      --muted-foreground: #8b95a1;
      --border: rgba(255, 255, 255, 0.06);
      --card-border: rgba(255, 255, 255, 0.04);
      --primary: #3182f6;
      --secondary: #202027;
      --secondary-foreground: #d1d6db;
      --success: #15c47e;
      --warning: #f59f00;
      --error: #f04452;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      padding: 16px;
      background: var(--background);
      color: var(--foreground);
      font-family: -apple-system, BlinkMacSystemFont, "Pretendard", "Apple SD Gothic Neo", "Segoe UI", Roboto, sans-serif;
      letter-spacing: -0.015em;
      line-height: 1.5;
      -webkit-font-smoothing: antialiased;
    }}
    .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }}
    .muted {{ color: var(--muted-foreground); }}
    .min-w-0 {{ min-width: 0; }}
    .page {{ max-width: 960px; margin: 0 auto; display: flex; flex-direction: column; gap: 14px; }}
    .header-card, .phase-card, .panel-card {{
      background: var(--card);
      border: none;
      border-radius: 22px;
      padding: 20px;
    }}
    .header-top {{
      display: flex;
      flex-wrap: wrap;
      align-items: flex-start;
      justify-content: space-between;
      gap: 10px;
    }}
    .page-title {{
      margin: 0;
      font-size: 20px;
      font-weight: 700;
      letter-spacing: -0.03em;
      color: var(--foreground);
    }}
    .subtitle {{
      margin-top: 6px;
      font-size: 12px;
      color: var(--muted-foreground);
    }}
    .kpi-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
      gap: 10px;
      margin-top: 16px;
    }}
    .kpi-card {{
      padding: 14px 16px;
      border-radius: 16px;
      background: var(--secondary);
      border: none;
      display: flex;
      flex-direction: column;
      gap: 6px;
    }}
    .kpi-label {{
      font-size: 12px;
      font-weight: 500;
      color: var(--muted-foreground);
    }}
    .kpi-val {{
      font-size: 20px;
      font-weight: 700;
      letter-spacing: -0.02em;
      font-variant-numeric: tabular-nums;
      color: var(--foreground);
    }}
    .kpi-primary {{ color: var(--primary); }}
    .kpi-success {{ color: var(--success); }}
    .kpi-error {{ color: var(--error); }}
    .phase-card, .panel-card {{ display: flex; flex-direction: column; gap: 14px; }}
    .phase-header, .panel-header {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
    }}
    .phase-title-group, .gate-group {{ display: flex; flex-wrap: wrap; align-items: center; gap: 8px; font-size: 12px; }}
    .phase-id {{
      padding: 3px 9px;
      border-radius: 8px;
      background: var(--secondary);
      color: var(--muted-foreground);
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 11px;
      font-weight: 500;
    }}
    .phase-name, .panel-title {{
      margin: 0;
      font-size: 16px;
      font-weight: 700;
      letter-spacing: -0.02em;
      color: var(--foreground);
    }}
    .dur-pill {{
      padding: 3px 9px;
      border-radius: 8px;
      background: rgba(255, 255, 255, 0.06);
      color: var(--secondary-foreground);
      font-size: 11px;
      font-weight: 600;
      font-variant-numeric: tabular-nums;
    }}
    .nodes-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 10px;
    }}
    .node-card {{
      background: var(--secondary);
      border: none;
      border-radius: 16px;
      padding: 16px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      gap: 10px;
    }}
    .node-top {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; }}
    .node-id {{ font-size: 11px; color: var(--muted-foreground); }}
    .node-title {{
      font-size: 14px;
      font-weight: 600;
      letter-spacing: -0.01em;
      color: var(--foreground);
      margin-top: 3px;
      line-height: 1.4;
    }}
    .node-meta-line {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 6px;
      font-size: 12px;
      color: var(--muted-foreground);
      font-variant-numeric: tabular-nums;
    }}
    .meta-worker {{ color: var(--secondary-foreground); font-weight: 500; }}
    .meta-sep {{ opacity: 0.45; }}
    .chips {{ display: flex; flex-wrap: wrap; gap: 6px; }}
    .chip {{
      padding: 3px 8px;
      border-radius: 6px;
      font-size: 11px;
      border: none;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    }}
    .chip-dep {{ background: rgba(49, 130, 246, 0.14); color: var(--primary); }}
    .chip-file {{ background: rgba(255, 255, 255, 0.04); color: var(--muted-foreground); }}
    .node-note {{ font-size: 12px; color: var(--muted-foreground); word-break: break-word; }}
    .gate-note {{
      font-size: 12px;
      color: var(--secondary-foreground);
      padding: 12px 14px;
      border-radius: 12px;
      background: rgba(49, 130, 246, 0.08);
      border: none;
      line-height: 1.5;
    }}
    .badge {{
      flex-shrink: 0;
      display: inline-flex;
      align-items: center;
      gap: 5px;
      padding: 4px 10px;
      border-radius: 999px;
      border: none;
      font-size: 11px;
      font-weight: 600;
      color: var(--muted-foreground);
    }}
    .dot {{ width: 6px; height: 6px; border-radius: 50%; background: currentColor; }}
    .badge-pending {{ background: var(--card); color: var(--muted-foreground); }}
    .badge-building, .badge-script {{ background: rgba(49, 130, 246, 0.14); color: var(--primary); }}
    .badge-arena, .badge-interrogating {{ background: rgba(245, 159, 0, 0.14); color: var(--warning); }}
    .badge-rejected {{ background: rgba(240, 68, 82, 0.14); color: var(--error); }}
    .badge-verified {{ background: rgba(21, 196, 126, 0.14); color: var(--success); }}
    .gantt-ruler {{
      display: flex;
      justify-content: space-between;
      font-size: 11px;
      color: var(--muted-foreground);
      padding: 0 2px;
    }}
    .gantt-list {{ display: flex; flex-direction: column; gap: 10px; }}
    .gantt-item {{
      background: var(--secondary);
      border: none;
      border-radius: 16px;
      padding: 14px 16px;
      display: flex;
      flex-direction: column;
      gap: 9px;
    }}
    .gantt-phase {{ background: rgba(49, 130, 246, 0.09); border-left: none; }}
    .gantt-node {{ margin-left: 0; }}
    .gantt-head {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      font-size: 13px;
    }}
    .gantt-title-wrap {{ display: flex; align-items: center; gap: 8px; min-width: 0; flex: 1 1 180px; }}
    .gantt-tag {{
      flex-shrink: 0;
      padding: 3px 8px;
      border-radius: 6px;
      background: rgba(255, 255, 255, 0.06);
      color: var(--secondary-foreground);
      font-size: 11px;
      font-weight: 600;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    }}
    .gantt-tag-phase {{ background: rgba(49, 130, 246, 0.16); color: var(--primary); }}
    .gantt-label {{ color: var(--foreground); font-weight: 600; word-break: break-word; }}
    .gantt-sub {{ font-size: 12px; line-height: 1.45; word-break: break-word; }}
    .gantt-meta {{ display: flex; align-items: center; gap: 8px; flex-shrink: 0; }}
    .gantt-range {{ font-size: 11px; white-space: nowrap; font-variant-numeric: tabular-nums; }}
    .gantt-track {{
      position: relative;
      width: 100%;
      height: 8px;
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.06);
      border: none;
      overflow: hidden;
    }}
    .gantt-bar {{
      position: absolute;
      top: 0;
      bottom: 0;
      border-radius: 999px;
      opacity: 1;
    }}
    .gantt-bar-empty {{ height: 100%; }}
    .stats {{ display: flex; flex-wrap: wrap; align-items: center; gap: 8px; font-size: 12px; }}
    .stat-pill {{
      padding: 6px 11px;
      border-radius: 10px;
      background: var(--secondary);
      border: none;
    }}
    .stat-value {{
      font-weight: 600;
      margin-left: 4px;
      color: var(--foreground);
      font-variant-numeric: tabular-nums;
    }}
    .stat-success {{ color: var(--success); }}
    .share-wrap {{ display: flex; flex-direction: column; gap: 8px; }}
    .share-bar {{
      display: flex;
      width: 100%;
      height: 8px;
      border-radius: 999px;
      overflow: hidden;
      background: rgba(255, 255, 255, 0.06);
      border: none;
      gap: 2px;
    }}
    .share-seg {{ height: 100%; border-radius: 999px; }}
    .share-legend {{ display: flex; flex-wrap: wrap; gap: 12px; font-size: 12px; }}
    .legend-chip {{ display: inline-flex; align-items: center; gap: 6px; }}
    .legend-dot {{ width: 8px; height: 8px; border-radius: 50%; }}
    .obs-stack {{ display: flex; flex-direction: column; gap: 14px; }}
    .obs-table-wrap {{ width: 100%; overflow-x: auto; }}
    .obs-table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
    .obs-table th, .obs-table td {{
      text-align: left;
      padding: 10px 8px;
      border-bottom: 1px solid var(--card-border);
    }}
    .obs-table tbody tr:last-child td {{ border-bottom: none; }}
    .obs-table th.num, .obs-table td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
    .obs-table th {{ color: var(--muted-foreground); font-weight: 500; font-size: 11px; }}
    .alert-item {{
      padding: 13px 16px;
      border-radius: 14px;
      border: none;
      background: rgba(245, 159, 0, 0.12);
      color: var(--warning);
      font-size: 12px;
      line-height: 1.5;
      margin-bottom: 6px;
    }}
    .alert-ok {{
      padding: 13px 16px;
      border-radius: 14px;
      border: none;
      background: rgba(21, 196, 126, 0.12);
      color: var(--success);
      font-size: 12px;
      line-height: 1.5;
    }}
  </style>
</head>
<body>
  <div class="page">
    <header class="header-card">
      <div class="header-top">
        <div>
          <h1 class="page-title">{html.escape(state["title"])}</h1>
          <div class="subtitle">
            Tier 1: Deterministic Guard · Tier 2: Interrogator Gate ({html.escape(interrogator_model)})
          </div>
        </div>
        <div class="muted mono" style="font-size:11px;">Updated {html.escape(state["updated_at"])}</div>
      </div>
      <div class="kpi-grid">
        <div class="kpi-card">
          <span class="kpi-label">Verified</span>
          <span class="kpi-val">{verified_nodes}/{total_nodes}</span>
        </div>
        <div class="kpi-card">
          <span class="kpi-label">Duration</span>
          <span class="kpi-val kpi-primary">{html.escape(total_duration_label)}</span>
        </div>
        <div class="kpi-card">
          <span class="kpi-label">Est. Tokens</span>
          <span class="kpi-val">{total_estimated:,}</span>
        </div>
        <div class="kpi-card">
          <span class="kpi-label">Tier-1 Saved</span>
          <span class="kpi-val kpi-success">{tier1_saved:,}</span>
        </div>
        {rejected_kpi_html}
      </div>
    </header>
    <main class="page">
      {"".join(phase_cards_html)}
      <section class="panel-card">
        <div class="panel-header">
          <h2 class="panel-title">Phase &amp; Worker Timeline (Gantt View)</h2>
          <span class="muted mono" style="font-size:11px;">{len(gantt_rows)} items · {html.escape(total_duration_label)}</span>
        </div>
        <div class="gantt-ruler mono">
          <span>0%</span><span>25%</span><span>50%</span><span>75%</span><span>100%</span>
        </div>
        <div class="gantt-list">
          {"".join(gantt_rows_html)}
        </div>
      </section>
      <section class="panel-card">
        <div class="panel-header">
          <h2 class="panel-title">Observability &amp; Token Estimate</h2>
          <div class="stats">
            <div class="stat-pill">
              <span class="muted">Gate Checks:</span>
              <span class="stat-value mono">{int(gate_metrics.get("total", 0))} ({int(gate_metrics.get("pass", 0))} pass / {int(gate_metrics.get("fail", 0))} fail)</span>
            </div>
            <div class="stat-pill">
              <span class="muted">Fail Rate:</span>
              <span class="stat-value mono">{float(gate_metrics.get("failure_rate", 0.0)):.1%}</span>
            </div>
            <div class="stat-pill">
              <span class="muted">Tier-1 Saved:</span>
              <span class="stat-value stat-success mono">{tier1_saved:,}</span>
            </div>
          </div>
        </div>
        {share_bar_html}
        <div class="obs-stack">
          <div class="obs-table-wrap">
            <table class="obs-table">
              <thead>
                <tr><th>Role</th><th class="num">Calls</th><th class="num">Avg/Call</th><th class="num">Est. Tokens</th><th class="num">Share</th></tr>
              </thead>
              <tbody>
                {"".join(role_rows_html)}
              </tbody>
            </table>
          </div>
          <div>
            {anomalies_html}
          </div>
        </div>
      </section>
    </main>
  </div>
</body>
</html>
"""


def write_dag_outputs(
    state: dict[str, Any],
    state_path: Path,
    cfg: dict[str, Any] | None = None,
    telemetry_path: Path | None = None,
    explicit_telemetry: bool = False,
    repo: Path | None = None,
) -> None:
    """Persist DAG JSON state and re-render configured Markdown and HTML views."""
    resolved_cfg = cfg if cfg is not None else load_config(DEFAULT_CONFIG_PATH)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    tel_paths = collect_telemetry_paths(
        explicit_path=telemetry_path if explicit_telemetry else None,
        repo=repo,
        state_path=state_path,
    )
    events = read_merged_telemetry_events(tel_paths)
    primary_tel = (telemetry_path if explicit_telemetry else None) or (tel_paths[0] if tel_paths else telemetry_path)
    snapshots_dir = state_path.parent / "snapshots" if (state_path.parent / "snapshots").is_dir() else None
    if snapshots_dir is None and repo is not None and (repo / ".podo" / "snapshots").is_dir():
        snapshots_dir = repo / ".podo" / "snapshots"
    obs = compute_observability_snapshot(
        cfg=resolved_cfg,
        state=state,
        events=events,
        repo=repo,
        telemetry_path=primary_tel,
        state_path=state_path,
        snapshots_dir=snapshots_dir,
    )
    if not events:
        snap_candidate = load_json_dict(state_path.parent / "podo-session-snapshot.json")
        if snap_candidate and isinstance(snap_candidate.get("gate_metrics"), dict):
            obs["gate_metrics"] = snap_candidate["gate_metrics"]

    md_out = state.get("md_out")
    if md_out:
        md_path = Path(md_out)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(render_dag_markdown(state, cfg=resolved_cfg, observability=obs), encoding="utf-8")

    html_out = state.get("html_out")
    if html_out:
        html_path = Path(html_out)
        html_path.parent.mkdir(parents=True, exist_ok=True)
        html_path.write_text(render_dag_html(state, cfg=resolved_cfg, observability=obs), encoding="utf-8")


def cmd_dag(args: argparse.Namespace) -> int:
    """Initialize or update the live Podo Swarm DAG state and artifacts."""
    t0 = time.monotonic()
    state_path = Path(args.state_file).resolve()
    cfg = load_config(Path(args.config).resolve() if args.config else DEFAULT_CONFIG_PATH)
    now = utc_now_iso()
    explicit_tel_arg = getattr(args, "telemetry_file", None)
    explicit_tel = bool((explicit_tel_arg and explicit_tel_arg.strip()) or os.environ.get("PODO_TELEMETRY_FILE", "").strip())
    repo_arg = getattr(args, "repo", None)
    dag_repo = Path(repo_arg).resolve() if repo_arg else (state_path.parent.parent if state_path.parent.name == ".podo" else None)
    telemetry_target = resolve_telemetry_target(
        explicit_path=explicit_tel_arg,
        state_path=state_path,
    )

    if args.dag_action == "init":
        raw_arg = args.phases_json
        existing = safe_existing_file(raw_arg)
        if existing is not None:
            raw_json = json.loads(existing.read_text(encoding="utf-8"))
        else:
            raw_json = json.loads(raw_arg)
        phases = normalize_phases(raw_json, now=now)
        md_out = str(Path(args.md_out).resolve()) if args.md_out else str(state_path.with_suffix(".md"))
        html_out = str(Path(args.html_out).resolve()) if args.html_out else str(state_path.with_suffix(".html"))
        state = {
            "title": args.title,
            "created_at": now,
            "updated_at": now,
            "md_out": md_out,
            "html_out": html_out,
            "phases": phases,
        }
        emit_telemetry_event(
            cfg,
            telemetry_target,
            "dag_init",
            conversation_id=os.environ.get("PODO_CONVERSATION_ID", "").strip(),
            worker="coordinator",
            role="coordinator",
            gate="dag",
            result="ok",
            violation_count=0,
            files=[state_path.name],
            duration_ms=(time.monotonic() - t0) * 1000.0,
        )
        write_dag_outputs(
            state,
            state_path,
            cfg=cfg,
            telemetry_path=telemetry_target,
            explicit_telemetry=explicit_tel,
            repo=dag_repo,
        )
        print(f"[podo-guard] Initialized DAG state={state_path} md={md_out} html={html_out}")
        return 0

    if not state_path.is_file():
        print(f"Error: DAG state file not found: {state_path}", file=sys.stderr)
        return 2

    state = json.loads(state_path.read_text(encoding="utf-8"))
    if args.md_out:
        state["md_out"] = str(Path(args.md_out).resolve())
    if args.html_out:
        state["html_out"] = str(Path(args.html_out).resolve())

    batch_updates: dict[str, str] = {}
    if args.set_nodes:
        for raw_item in args.set_nodes.split(","):
            item = raw_item.strip()
            if not item:
                continue
            if "=" not in item:
                print(f"Error: --set-nodes expects id=status pairs, got '{item}'", file=sys.stderr)
                return 2
            nid, nstatus = item.split("=", 1)
            nid = nid.strip()
            nstatus = nstatus.strip()
            if not nid or not nstatus:
                print(f"Error: invalid --set-nodes pair '{item}'", file=sys.stderr)
                return 2
            batch_updates[nid] = nstatus

    matched_batch: set[str] = set()
    updated = False
    started_override = getattr(args, "started_at", None)
    finished_override = getattr(args, "finished_at", None)
    fallback_start = state.get("created_at") or state.get("updated_at") or now

    for ph in state.get("phases", []):
        ph.setdefault("started_at", None)
        ph.setdefault("finished_at", None)
        ph.setdefault("duration_s", None)
        ph.setdefault("interrogator_calls", 0)
        phase_fallback_start = ph.get("started_at") or fallback_start

        for node in ph.get("nodes", []):
            node.setdefault("started_at", None)
            node.setdefault("finished_at", None)
            node.setdefault("duration_s", None)
            node_id = node.get("id")
            node_status_changed: str | None = None
            node_touched = False

            if args.node and node_id == args.node:
                if args.status:
                    node["status"] = args.status
                    node_status_changed = args.status
                if args.worker is not None:
                    node["worker"] = args.worker
                if args.model is not None:
                    node["model"] = args.model
                if args.note is not None:
                    node["note"] = args.note
                node_touched = True
            if node_id in batch_updates:
                node["status"] = batch_updates[node_id]
                node_status_changed = batch_updates[node_id]
                matched_batch.add(node_id)
                node_touched = True

            if node_touched:
                apply_status_timestamps(
                    node,
                    node_status_changed,
                    now,
                    started_override=started_override,
                    finished_override=finished_override,
                    fallback_started_at=phase_fallback_start,
                )
                node_start_ts = parse_iso_timestamp(node.get("started_at"))
                ph_start_ts = parse_iso_timestamp(ph.get("started_at"))
                if node.get("started_at") and (
                    ph_start_ts is None or (node_start_ts is not None and node_start_ts < ph_start_ts)
                ):
                    ph["started_at"] = node["started_at"]
                    ph["duration_s"] = compute_duration_s(ph.get("started_at"), ph.get("finished_at"))
                updated = True

        node_starts = [
            (parse_iso_timestamp(n.get("started_at")), n.get("started_at"))
            for n in ph.get("nodes", [])
            if parse_iso_timestamp(n.get("started_at")) is not None
        ]
        earliest_node_start = min(node_starts, key=lambda pair: pair[0])[1] if node_starts else None
        node_finishes = [
            (parse_iso_timestamp(n.get("finished_at")), n.get("finished_at"))
            for n in ph.get("nodes", [])
            if parse_iso_timestamp(n.get("finished_at")) is not None
        ]
        latest_node_finish = max(node_finishes, key=lambda pair: pair[0])[1] if node_finishes else None

        if args.phase and ph.get("id") == args.phase:
            if args.gate_status:
                ph["gate_status"] = args.gate_status
                if args.gate_status in INTERROGATOR_CALL_STATUSES:
                    ph["interrogator_calls"] = int(ph.get("interrogator_calls") or 0) + 1
            if args.note is not None:
                ph["gate_note"] = args.note
            apply_status_timestamps(
                ph,
                args.gate_status,
                now,
                started_override=started_override,
                finished_override=finished_override,
                fallback_started_at=earliest_node_start or fallback_start,
            )
            if ph.get("finished_at") and latest_node_finish and not finished_override:
                ph_fin_ts = parse_iso_timestamp(ph.get("finished_at"))
                latest_ts = parse_iso_timestamp(latest_node_finish)
                if ph_fin_ts is not None and latest_ts is not None and latest_ts > ph_fin_ts:
                    ph["finished_at"] = latest_node_finish
                    ph["duration_s"] = compute_duration_s(ph.get("started_at"), ph.get("finished_at"))
            updated = True

    missing_nodes = sorted(set(batch_updates.keys()) - matched_batch)
    if missing_nodes:
        print(f"Error: target node(s) not found in DAG state: {', '.join(missing_nodes)}", file=sys.stderr)
        return 2

    if not updated:
        print("Error: target --node, --set-nodes, or --phase not found in DAG state", file=sys.stderr)
        return 2

    state["updated_at"] = now
    is_interrogator_call = bool(args.gate_status in INTERROGATOR_CALL_STATUSES)
    ev_role = "interrogator" if is_interrogator_call else ""
    ev_worker = "interrogator" if is_interrogator_call else (args.worker or "")
    ev_model = get_interrogator_model(cfg) if is_interrogator_call else (args.model or "")
    emit_telemetry_event(
        cfg,
        telemetry_target,
        "dag_update",
        conversation_id=os.environ.get("PODO_CONVERSATION_ID", "").strip(),
        phase=args.phase or "",
        worker=ev_worker,
        role=ev_role,
        model=ev_model,
        gate="interrogator_gate" if is_interrogator_call else "dag",
        result=args.gate_status or args.status or "ok",
        violation_count=1 if args.gate_status == "rejected" else 0,
        files=[state_path.name],
        duration_ms=(time.monotonic() - t0) * 1000.0,
    )
    write_dag_outputs(
        state,
        state_path,
        cfg=cfg,
        telemetry_path=telemetry_target,
        explicit_telemetry=explicit_tel,
        repo=dag_repo,
    )
    print(f"[podo-guard] Updated DAG state={state_path}")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    """Display or update podo-config.json role mappings and gate modes."""
    config_path = Path(args.config).resolve() if args.config else DEFAULT_CONFIG_PATH
    cfg = load_config(config_path)
    changed = False

    do_reset = bool(args.reset)
    if args.preset:
        if args.preset == "podo-default":
            do_reset = True
        else:
            print(
                f"Error: unknown preset '{args.preset}'. Use --reset (or --preset podo-default) and --set <role>=<model>.",
                file=sys.stderr,
            )
            return 2

    if do_reset:
        defaults = cfg.get("default_roles")
        if not isinstance(defaults, dict) or not defaults:
            defaults = dict(DEFAULT_ROLES)
            cfg["default_roles"] = dict(defaults)
        cfg["roles"] = dict(defaults)
        changed = True

    for assignment in args.set or []:
        if "=" not in assignment:
            print(f"Error: --set expects role=model, got '{assignment}'", file=sys.stderr)
            return 2
        role, model = assignment.split("=", 1)
        role = role.strip()
        model = model.strip()
        if not role or not model:
            print(f"Error: --set expects non-empty role=model, got '{assignment}'", file=sys.stderr)
            return 2
        cfg.setdefault("roles", {})[role] = model
        changed = True

    if args.mode:
        cfg.setdefault("gates", {})["stop_hook_mode"] = args.mode
        changed = True

    if getattr(args, "autopilot", None):
        cfg.setdefault("autopilot", dict(DEFAULT_AUTOPILOT_CONFIG))["enabled"] = args.autopilot == "on"
        changed = True

    if changed:
        save_config(cfg, config_path)

    if args.show or not changed:
        print(json.dumps(cfg, indent=2, ensure_ascii=False))
    return 0


def unquote_tool_arg(value: Any) -> str:
    """Decode plain or JSON-quoted string tool arguments from transcript logs."""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        try:
            parsed = json.loads(text)
            if isinstance(parsed, str):
                return parsed
        except json.JSONDecodeError:
            return text[1:-1]
    return text


def inspect_turn_transcript(transcript_path: Path, repo: Path) -> tuple[bool, bool, set[str]] | None:
    """Inspect transcript entries after the last USER_INPUT step.

    Returns (saw_file_edit, saw_run_command, edited_repo_rel_paths), or None if unreadable.
    """
    if not transcript_path.is_file():
        return None
    try:
        raw_lines = transcript_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None

    entries: list[dict[str, Any]] = []
    for line in raw_lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            entries.append(obj)

    last_user_idx = -1
    for idx, entry in enumerate(entries):
        if entry.get("type") == "USER_INPUT":
            last_user_idx = idx

    turn_entries = entries[last_user_idx + 1 :]
    saw_file_edit = False
    saw_run_command = False
    edited_files: set[str] = set()

    for entry in turn_entries:
        tool_calls = entry.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            tool_name, raw_args = extract_tool_call_info(tc)

            if tool_name == "run_command":
                saw_run_command = True
            elif tool_name in EDIT_TOOL_NAMES:
                saw_file_edit = True
                for key in ("Path", "path", "file_path", "TargetFile", "target_file"):
                    if key in raw_args:
                        decoded = unquote_tool_arg(raw_args[key])
                        norm = normalize_rel_path(decoded, repo)
                        if norm:
                            edited_files.add(norm)
                        break

    return saw_file_edit, saw_run_command, edited_files


def write_session_snapshot(
    *,
    cfg: dict[str, Any],
    repo: Path,
    state_path: Path | None,
    telemetry_path: Path | None,
    conversation_id: str,
    transcript_path: Path | None,
    artifact_dir: Path | None,
    out_path: Path | None = None,
    explicit_telemetry: bool = False,
) -> tuple[dict[str, Any], Path | None]:
    """Build and persist a session observability snapshot and trace-index.json entry."""
    podo_dir = repo / ".podo"
    has_podo = podo_dir.is_dir()
    safe_conv = sanitize_identifier(conversation_id)

    primary_path: Path | None = None
    if out_path is not None:
        primary_path = out_path.resolve()
    elif has_podo:
        primary_path = podo_dir / "snapshots" / f"{safe_conv}.json"
    elif artifact_dir is not None:
        primary_path = artifact_dir / "podo-session-snapshot.json"

    state = load_json_dict(state_path)
    tel_paths = collect_telemetry_paths(
        explicit_path=telemetry_path if explicit_telemetry else None,
        repo=repo,
        state_path=state_path,
        artifact_dir=artifact_dir,
    )
    if not explicit_telemetry and telemetry_path is not None and telemetry_path.is_file():
        resolved_extra = telemetry_path.resolve()
        if resolved_extra not in tel_paths:
            tel_paths.append(resolved_extra)
    events = read_merged_telemetry_events(tel_paths)
    primary_tel = (telemetry_path if explicit_telemetry else None) or (tel_paths[0] if tel_paths else telemetry_path)
    snapshots_dir = podo_dir / "snapshots" if (podo_dir / "snapshots").is_dir() else None

    snap = compute_observability_snapshot(
        cfg=cfg,
        state=state,
        events=events,
        repo=repo,
        conversation_id=conversation_id,
        transcript_path=transcript_path,
        artifact_dir=artifact_dir,
        telemetry_path=primary_tel,
        snapshot_path=primary_path,
        state_path=state_path,
        snapshots_dir=snapshots_dir,
    )

    if primary_path is not None:
        try:
            primary_path.parent.mkdir(parents=True, exist_ok=True)
            primary_path.write_text(json.dumps(snap, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        except OSError:
            pass

    if has_podo and artifact_dir is not None and out_path is None:
        mirror_path = artifact_dir / "podo-session-snapshot.json"
        if mirror_path != primary_path:
            try:
                mirror_path.parent.mkdir(parents=True, exist_ok=True)
                mirror_path.write_text(json.dumps(snap, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            except OSError:
                pass

    if has_podo:
        update_trace_index(podo_dir / "trace-index.json", snap["trace_link"])
    elif out_path is not None:
        update_trace_index(out_path.resolve().parent / "trace-index.json", snap["trace_link"])

    return snap, primary_path


def format_telemetry_report(snap: dict[str, Any]) -> str:
    """Format an observability snapshot into a concise human-readable terminal report."""
    dag = snap.get("dag_summary", {})
    gates = snap.get("gate_metrics", {})
    tok = snap.get("token_estimates", {})
    trace = snap.get("trace_link", {})

    lines = [
        f"=== Podo Telemetry Report ({snap.get('updated_at', '')}) ===",
        (
            f"DAG: title='{dag.get('title') or '-'}' phases={dag.get('phase_count', 0)} "
            f"tasks={dag.get('node_counts', {}).get('verified', 0)}/{dag.get('node_counts', {}).get('total', 0)} "
            f"interrogator_calls={dag.get('total_interrogator_calls', 0)} "
            f"last_verdict={dag.get('last_interrogator_verdict', 'pending')} "
            f"duration={format_duration_s(dag.get('total_duration_s'))}"
        ),
        (
            f"Gates: total={gates.get('total', 0)} pass={gates.get('pass', 0)} "
            f"fail={gates.get('fail', 0)} observe={gates.get('observe', 0)} "
            f"failure_rate={float(gates.get('failure_rate', 0.0)):.2%}"
        ),
        (
            f"Tokens: estimated={int(tok.get('total_estimated_tokens', 0)):,} "
            f"tier1_saved={int(tok.get('tier1_saved_tokens', 0)):,}"
        ),
    ]

    by_role = tok.get("by_role", {})
    if by_role:
        lines.append("Roles:")
        for role_name, info in sorted(by_role.items()):
            lines.append(
                f"  - {role_name}: calls={info.get('calls', 0)} "
                f"avg={int(info.get('avg_tokens', 0)):,} est={int(info.get('estimated_tokens', 0)):,}"
            )

    anomalies = tok.get("anomalies", [])
    if anomalies:
        lines.append("Anomalies:")
        for alert in anomalies:
            lines.append(f"  - [{alert.get('code')}] {alert.get('message')}")
    else:
        lines.append("Anomalies: none")

    if trace.get("conversation_id") or trace.get("transcript_path"):
        lines.append(
            f"Trace: conversation={trace.get('conversation_id') or '-'} "
            f"steps={trace.get('transcript_steps_count', 0)} "
            f"user_turns={trace.get('user_turns_count', 0)}"
        )

    return "\n".join(lines)


def cmd_telemetry(args: argparse.Namespace) -> int:
    """Execute telemetry report or snapshot subcommands."""
    repo = Path(args.repo).resolve()
    cfg = load_config(Path(args.config).resolve() if args.config else DEFAULT_CONFIG_PATH)
    explicit_tel = bool(
        (args.telemetry_file and args.telemetry_file.strip())
        or os.environ.get("PODO_TELEMETRY_FILE", "").strip()
    )

    if args.telemetry_action == "report":
        state_path, telemetry_path, _ = discover_observability_paths(
            repo,
            state_file_arg=args.state_file,
            telemetry_file_arg=args.telemetry_file,
        )
        state = load_json_dict(state_path)
        tel_paths = collect_telemetry_paths(
            explicit_path=telemetry_path if explicit_tel else None,
            repo=repo,
            state_path=state_path,
        )
        events = read_merged_telemetry_events(tel_paths)
        primary_tel = (telemetry_path if explicit_tel else None) or (tel_paths[0] if tel_paths else telemetry_path)
        snap = compute_observability_snapshot(
            cfg=cfg,
            state=state,
            events=events,
            repo=repo,
            telemetry_path=primary_tel,
            state_path=state_path,
        )
        if args.json:
            print(json.dumps(snap, indent=2, ensure_ascii=False))
        else:
            print(format_telemetry_report(snap))
        return 0

    state_path, telemetry_path, artifact_dir = discover_observability_paths(
        repo,
        state_file_arg=args.state_file,
        telemetry_file_arg=args.telemetry_file,
        artifact_dir_arg=args.artifact_dir,
    )
    conv_id = (args.conversation_id or os.environ.get("PODO_CONVERSATION_ID", "")).strip()
    transcript_path = Path(args.transcript_path).resolve() if args.transcript_path else None
    out_path = Path(args.out).resolve() if args.out else None

    snap, _ = write_session_snapshot(
        cfg=cfg,
        repo=repo,
        state_path=state_path,
        telemetry_path=telemetry_path,
        conversation_id=conv_id,
        transcript_path=transcript_path,
        artifact_dir=artifact_dir,
        out_path=out_path,
        explicit_telemetry=explicit_tel,
    )
    print(json.dumps(snap, indent=2, ensure_ascii=False))
    return 0


def handle_hook_stop(config_path: Path = DEFAULT_CONFIG_PATH) -> int:
    """Enforce deterministic no-comments, dead-code, and codified checks on Antigravity Stop hook."""
    t0 = time.monotonic()
    role = os.environ.get("PODO_ROLE", "").strip().lower()
    if role in READ_ONLY_ROLES:
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
    if payload.get("continuationActive"):
        print("{}")
        return 0
    if payload.get("terminationReason") != "NO_TOOL_CALL":
        print("{}")
        return 0

    cwd = payload.get("cwd") or (payload.get("workspacePaths") or [None])[0]
    if not cwd or not Path(cwd).is_dir():
        print("{}")
        return 0

    repo = Path(cwd).resolve()
    cfg = load_config(config_path)
    mode = resolve_stop_hook_mode(cfg)
    if mode == "off":
        print("{}")
        return 0

    file_filter: set[str] | None = None
    transcript_raw = payload.get("transcriptPath") or payload.get("transcript_path")
    transcript_path = (
        Path(transcript_raw.strip()).resolve()
        if isinstance(transcript_raw, str) and transcript_raw.strip()
        else None
    )
    if transcript_path is not None:
        turn_info = inspect_turn_transcript(transcript_path, repo)
        if turn_info is not None:
            saw_file_edit, saw_run_command, edited_files = turn_info
            if not saw_file_edit and not saw_run_command:
                print("{}")
                return 0
            if saw_file_edit and not saw_run_command:
                if not edited_files:
                    print("{}")
                    return 0
                file_filter = edited_files

    allow_tags = get_allow_tags(cfg)
    dead_code_enabled = is_dead_code_check_enabled(cfg)
    deadline = time.monotonic() + 10.0

    violations: list[str] = []
    violations.extend(
        run_comment_and_dead_code_gates(
            repo,
            allow_tags,
            file_filter=file_filter,
            dead_code_enabled=dead_code_enabled,
        )
    )
    violations.extend(run_codified_checks(repo, cmd_timeout=8, deadline=deadline, file_filter=file_filter))

    conv_id = str(payload.get("conversationId") or payload.get("conversation_id") or "").strip()
    artifact_raw = payload.get("artifactDirectoryPath") or payload.get("artifact_directory_path") or ""
    artifact_dir = (
        Path(artifact_raw.strip()).resolve()
        if isinstance(artifact_raw, str) and artifact_raw.strip()
        else None
    )
    outcome = "pass" if not violations else ("observe" if mode == "observe" else "fail")
    telemetry_target = resolve_telemetry_target(repo=repo)
    emit_telemetry_event(
        cfg,
        telemetry_target,
        "hook_stop",
        conversation_id=conv_id,
        phase=os.environ.get("PODO_PHASE", "").strip(),
        worker=os.environ.get("PODO_WORKER", "").strip(),
        role=role,
        model=get_role_model(cfg, role),
        gate=classify_violation_gate(violations),
        result=outcome,
        violation_count=len(violations),
        files=extract_violation_files(violations, fallback_files=file_filter),
        duration_ms=(time.monotonic() - t0) * 1000.0,
    )

    if (repo / ".podo").is_dir() or artifact_dir is not None:
        explicit_tel = bool(os.environ.get("PODO_TELEMETRY_FILE", "").strip())
        state_path, resolved_telemetry, resolved_artifact = discover_observability_paths(
            repo,
            artifact_dir_arg=str(artifact_dir) if artifact_dir else None,
        )
        write_session_snapshot(
            cfg=cfg,
            repo=repo,
            state_path=state_path,
            telemetry_path=telemetry_target if explicit_tel else (resolved_telemetry or telemetry_target),
            conversation_id=conv_id,
            transcript_path=transcript_path,
            artifact_dir=resolved_artifact or artifact_dir,
            explicit_telemetry=explicit_tel,
        )

    if violations:
        if mode == "observe":
            warning_text = (
                f"[podo-guard:observe] {len(violations)} warning(s) detected (non-blocking):\n"
                + "\n".join(violations[:20])
                + "\n"
            )
            print(warning_text, file=sys.stderr, end="")
            podo_dir = repo / ".podo"
            if podo_dir.is_dir():
                try:
                    (podo_dir / "last-observe.log").write_text(warning_text, encoding="utf-8")
                except OSError:
                    pass
            print("{}")
            return 0

        reason = "[podo-guard] Deterministic per-task check failed. Fix these issues before completing:\n" + "\n".join(
            violations[:20]
        )
        print(json.dumps({"decision": "continue", "reason": reason}, ensure_ascii=False))
        return 0

    print("{}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser for podo-guard."""
    parser = argparse.ArgumentParser(prog="podo-guard.py")
    parser.add_argument("--config", help="Optional path to podo-config.json")
    parser.add_argument("--hook-stop", action="store_true", help="Run as Antigravity Stop lifecycle hook")
    parser.add_argument(
        "--hook-pre-invocation",
        action="store_true",
        help="Run as Antigravity PreInvocation autopilot router hook",
    )

    sub = parser.add_subparsers(dest="command")

    p_check = sub.add_parser("check", help="Run deterministic comment, dead-code, and structural checks")
    p_check.add_argument("--repo", default=".", help="Repository path to inspect")
    p_check.add_argument("--diff-text", help="Unified diff file path, '-' for stdin, or raw diff string")
    p_check.add_argument("--base", help="Git base ref for inspecting committed + workspace changes")
    p_check.add_argument("--files", help="Comma-separated repo-relative paths to scope checks to")
    p_check.add_argument("--mode", choices=("enforce", "observe"), default="enforce", help="Enforcement mode")
    p_check.add_argument("--run-verify", action="store_true", help="Also execute .podo/verify.sh if present")
    p_check.add_argument("--telemetry-file", help="Optional JSONL telemetry output path")
    p_check.add_argument("--phase", help="Optional phase identifier for telemetry attribution")
    p_check.add_argument("--worker", help="Optional worker name for telemetry attribution")
    p_check.add_argument("--role", help="Optional role name for telemetry attribution")

    p_codify = sub.add_parser("codify", help="Add a deterministic check to .podo/checks.json")
    p_codify.add_argument("--repo", default=".", help="Repository path")
    p_codify.add_argument("--id", required=True, help="Unique check identifier")
    p_codify.add_argument("--regex", help="Forbidden regex pattern")
    p_codify.add_argument("--glob", default="*", help="File glob for --regex")
    p_codify.add_argument("--cmd", help="Required shell command that must exit 0")
    p_codify.add_argument("--fix", required=True, help="Actionable remediation message")
    p_codify.add_argument("--telemetry-file", help="Optional JSONL telemetry output path")

    p_dag = sub.add_parser("dag", help="Initialize or update live DAG state and artifacts")
    dag_sub = p_dag.add_subparsers(dest="dag_action", required=True)

    p_dag_init = dag_sub.add_parser("init")
    p_dag_init.add_argument("--state-file", required=True)
    p_dag_init.add_argument("--title", required=True)
    p_dag_init.add_argument("--md-out")
    p_dag_init.add_argument("--html-out")
    p_dag_init.add_argument("--phases-json", required=True)
    p_dag_init.add_argument("--repo")
    p_dag_init.add_argument("--telemetry-file")

    p_dag_upd = dag_sub.add_parser("update")
    p_dag_upd.add_argument("--state-file", required=True)
    p_dag_upd.add_argument("--node")
    p_dag_upd.add_argument("--status")
    p_dag_upd.add_argument("--set-nodes", help="Batch node status updates as id1=status1,id2=status2")
    p_dag_upd.add_argument("--phase")
    p_dag_upd.add_argument("--gate-status")
    p_dag_upd.add_argument("--worker")
    p_dag_upd.add_argument("--model")
    p_dag_upd.add_argument("--note")
    p_dag_upd.add_argument("--started-at")
    p_dag_upd.add_argument("--finished-at")
    p_dag_upd.add_argument("--md-out")
    p_dag_upd.add_argument("--html-out")
    p_dag_upd.add_argument("--repo")
    p_dag_upd.add_argument("--telemetry-file")

    p_tel = sub.add_parser("telemetry", help="Inspect observability metrics and generate session snapshots")
    tel_sub = p_tel.add_subparsers(dest="telemetry_action", required=True)

    p_tel_rep = tel_sub.add_parser("report", help="Render telemetry report from DAG and JSONL events")
    p_tel_rep.add_argument("--repo", default=".", help="Repository path")
    p_tel_rep.add_argument("--state-file", help="Path to podo-dag.state.json")
    p_tel_rep.add_argument("--telemetry-file", help="Path to telemetry JSONL log")
    p_tel_rep.add_argument("--json", action="store_true", help="Output report as JSON")

    p_tel_snap = tel_sub.add_parser("snapshot", help="Write session snapshot and update trace-index.json")
    p_tel_snap.add_argument("--repo", default=".", help="Repository path")
    p_tel_snap.add_argument("--state-file", help="Path to podo-dag.state.json")
    p_tel_snap.add_argument("--telemetry-file", help="Path to telemetry JSONL log")
    p_tel_snap.add_argument("--conversation-id", help="Antigravity conversation ID")
    p_tel_snap.add_argument("--transcript-path", help="Path to conversation transcript.jsonl")
    p_tel_snap.add_argument("--artifact-dir", help="Antigravity artifact directory path")
    p_tel_snap.add_argument("--out", help="Explicit output path for snapshot JSON")

    p_cfg = sub.add_parser("config", help="Inspect or update role models and gate modes")
    p_cfg.add_argument("--show", action="store_true")
    p_cfg.add_argument("--reset", action="store_true")
    p_cfg.add_argument("--preset")
    p_cfg.add_argument("--mode", choices=VALID_STOP_MODES)
    p_cfg.add_argument("--autopilot", choices=("on", "off"))
    p_cfg.add_argument("--set", action="append")

    return parser


def main() -> int:
    """Entry point for CLI and lifecycle hook invocations."""
    parser = build_parser()
    args = parser.parse_args()

    if args.hook_pre_invocation:
        config_path = Path(args.config).resolve() if args.config else DEFAULT_CONFIG_PATH
        return handle_hook_pre_invocation(config_path=config_path)
    if args.hook_stop:
        config_path = Path(args.config).resolve() if args.config else DEFAULT_CONFIG_PATH
        return handle_hook_stop(config_path=config_path)
    if args.command == "check":
        return cmd_check(args)
    if args.command == "codify":
        return cmd_codify(args)
    if args.command == "dag":
        return cmd_dag(args)
    if args.command == "telemetry":
        return cmd_telemetry(args)
    if args.command == "config":
        return cmd_config(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
