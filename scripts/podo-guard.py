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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
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
    roles = resolved.get("roles", {})
    if isinstance(roles, dict):
        model = roles.get("interrogator")
        if isinstance(model, str) and model.strip():
            return model.strip()
    return DEFAULT_ROLES["interrogator"]


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


def cmd_check(args: argparse.Namespace) -> int:
    """Execute deterministic comment, AST dead-code, structural check, and optional verify.sh gates."""
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

    if violations:
        has_git_base_error = any(v.startswith("[GIT-BASE]") for v in violations)
        if args.mode == "observe" and not has_git_base_error:
            print(f"[podo-guard] OBSERVE (non-blocking): {len(violations)} violation(s) detected:")
            for line in violations:
                print(line)
            return 0
        for line in violations:
            print(line)
        return 1

    print("[podo-guard] OK: 0 violations (no-comments, dead-code & structural checks passed).")
    return 0


def cmd_codify(args: argparse.Namespace) -> int:
    """Record a deterministic structural check in <repo>/.podo/checks.json."""
    if not args.regex and not args.cmd:
        print("Error: specify either --regex or --cmd", file=sys.stderr)
        return 2

    repo = Path(args.repo).resolve()
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
    print(f"[podo-guard] Codified structural check '{args.id}' into {checks_file}")
    return 0


def normalize_phases(raw: Any) -> list[dict[str, Any]]:
    """Normalize input phase specification into a consistent schema."""
    phases = raw.get("phases", []) if isinstance(raw, dict) else raw
    if not isinstance(phases, list):
        raise ValueError("phases JSON must be a list or an object with a 'phases' list")

    normalized: list[dict[str, Any]] = []
    for idx, ph in enumerate(phases, start=1):
        ph_id = str(ph.get("id") or f"phase-{idx}")
        ph_name = str(ph.get("name") or f"Phase {idx}")
        gate_status = str(ph.get("gate_status") or "pending")
        gate_note = str(ph.get("gate_note") or "")
        nodes: list[dict[str, Any]] = []
        for nidx, node in enumerate(ph.get("nodes", []), start=1):
            nodes.append(
                {
                    "id": str(node.get("id") or f"{ph_id}-task-{nidx}"),
                    "title": str(node.get("title") or node.get("id") or f"Task {nidx}"),
                    "worker": str(node.get("worker") or ""),
                    "model": str(node.get("model") or ""),
                    "files": [str(f) for f in node.get("files", [])],
                    "depends_on": [str(d) for d in node.get("depends_on", [])],
                    "status": str(node.get("status") or "pending"),
                    "note": str(node.get("note") or ""),
                }
            )
        normalized.append(
            {
                "id": ph_id,
                "name": ph_name,
                "gate_status": gate_status,
                "gate_note": gate_note,
                "nodes": nodes,
            }
        )
    return normalized


def render_dag_markdown(state: dict[str, Any], cfg: dict[str, Any] | None = None) -> str:
    """Render DAG state into a Markdown summary with Mermaid flowchart and role-based Interrogator Gate table."""
    interrogator_model = get_interrogator_model(cfg)
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
            "| Phase | Task | Worker | Model | Status | Note |",
            "| :--- | :--- | :--- | :--- | :--- | :--- |",
        ]
    )

    for ph in phases:
        for node in ph.get("nodes", []):
            badge = MERMAID_STATUS_BADGE.get(node["status"], node["status"])
            lines.append(
                f"| {ph['name']} | `{node['id']}` {node['title']} | `{node.get('worker') or '-'}` | `{node.get('model') or '-'}` | {badge} | {node.get('note') or '-'} |"
            )
        gate_badge = MERMAID_STATUS_BADGE.get(ph["gate_status"], ph["gate_status"])
        lines.append(
            f"| {ph['name']} | **Interrogator Gate** | `interrogator` | `{interrogator_model}` | {gate_badge} | {ph.get('gate_note') or '-'} |"
        )

    lines.append("")
    return "\n".join(lines)


def render_dag_html(state: dict[str, Any], cfg: dict[str, Any] | None = None) -> str:
    """Render DAG state into a self-contained offline HTML dashboard using Antigravity semantic tokens."""
    interrogator_model = get_interrogator_model(cfg)
    phases = state.get("phases", [])
    total_nodes = sum(len(ph.get("nodes", [])) for ph in phases)
    verified_nodes = sum(
        1 for ph in phases for n in ph.get("nodes", []) if n.get("status") == "verified"
    )
    active_nodes = sum(
        1
        for ph in phases
        for n in ph.get("nodes", [])
        if n.get("status") in {"building", "script_checked", "arena_running", "phase_interrogating"}
    )
    rejected_nodes = sum(
        1 for ph in phases for n in ph.get("nodes", []) if n.get("status") == "rejected"
    )

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
            chips_row = f'<div class="chips">{deps_html}{files_html}</div>' if (deps_html or files_html) else ""
            node_cards.append(
                f"""
                <div class="node-card">
                  <div class="node-top">
                    <div>
                      <div class="node-id">{html.escape(node["id"])}</div>
                      <div class="node-title">{html.escape(node["title"])}</div>
                    </div>
                    <span class="badge {st_cls}">{html.escape(st_label)}</span>
                  </div>
                  <div class="node-meta">
                    <span class="meta-worker">@{html.escape(node.get("worker") or "unassigned")}</span>
                    <span>·</span>
                    <span class="mono">{html.escape(node.get("model") or "inherit")}</span>
                  </div>
                  {chips_row}
                  {note_html}
                </div>
                """
            )

        gate_note_html = (
            f'<div class="gate-note">Gate Note: {html.escape(ph["gate_note"])}</div>'
            if ph.get("gate_note")
            else ""
        )
        phase_cards_html.append(
            f"""
            <section class="phase-card">
              <div class="phase-header">
                <div class="phase-title-group">
                  <span class="phase-id">{html.escape(ph["id"])}</span>
                  <h2 class="phase-name">{html.escape(ph["name"])}</h2>
                </div>
                <div class="gate-group">
                  <span class="muted">Interrogator Gate ({html.escape(interrogator_model)}):</span>
                  <span class="badge {gate_cls}">{html.escape(gate_label)}</span>
                </div>
              </div>
              <div class="nodes-grid">
                {"".join(node_cards)}
              </div>
              {gate_note_html}
            </section>
            """
        )

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Podo Swarm DAG — {html.escape(state["title"])}</title>
  <style>
    :root {{
      --background: #0f1117;
      --card: #171a23;
      --foreground: #e6e8ef;
      --muted-foreground: #9aa2b2;
      --border: #2a3040;
      --card-border: #252b3b;
      --primary: #5b9cf6;
      --secondary: #222838;
      --secondary-foreground: #cfd6e6;
      --success: #34d399;
      --warning: #fbbf24;
      --error: #f87171;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      padding: 20px;
      background: var(--background);
      color: var(--foreground);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      line-height: 1.45;
      -webkit-font-smoothing: antialiased;
    }}
    .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }}
    .muted {{ color: var(--muted-foreground); }}
    .page {{ max-width: 1200px; margin: 0 auto; display: flex; flex-direction: column; gap: 16px; }}
    .header-card, .phase-card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 16px;
    }}
    .header-card {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
    }}
    .eyebrow {{
      font-size: 11px;
      font-weight: 600;
      letter-spacing: 0.04em;
      color: var(--primary);
    }}
    .page-title {{ margin: 4px 0 0; font-size: 18px; font-weight: 600; color: var(--foreground); }}
    .subtitle {{ margin-top: 4px; font-size: 12px; color: var(--muted-foreground); }}
    .stats {{ display: flex; flex-wrap: wrap; align-items: center; gap: 10px; font-size: 12px; }}
    .stat-pill {{
      padding: 6px 12px;
      border-radius: 8px;
      background: var(--background);
      border: 1px solid var(--border);
    }}
    .stat-value {{ font-weight: 600; margin-left: 4px; color: var(--foreground); }}
    .stat-primary {{ color: var(--primary); }}
    .stat-error {{ color: var(--error); }}
    .phase-card {{ display: flex; flex-direction: column; gap: 12px; }}
    .phase-header {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      padding-bottom: 10px;
      border-bottom: 1px solid var(--card-border);
    }}
    .phase-title-group, .gate-group {{ display: flex; align-items: center; gap: 8px; font-size: 12px; }}
    .phase-id {{
      padding: 2px 8px;
      border-radius: 6px;
      background: var(--secondary);
      color: var(--secondary-foreground);
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 12px;
    }}
    .phase-name {{ margin: 0; font-size: 14px; font-weight: 600; color: var(--foreground); }}
    .nodes-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
      gap: 12px;
    }}
    .node-card {{
      background: var(--background);
      border: 1px solid var(--card-border);
      border-radius: 8px;
      padding: 12px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      gap: 8px;
    }}
    .node-top {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; }}
    .node-id {{
      font-size: 11px;
      color: var(--muted-foreground);
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    }}
    .node-title {{ font-size: 14px; font-weight: 500; color: var(--foreground); margin-top: 2px; }}
    .node-meta {{ display: flex; flex-wrap: wrap; align-items: center; gap: 6px; font-size: 12px; color: var(--muted-foreground); }}
    .meta-worker {{
      color: var(--foreground);
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    }}
    .chips {{ display: flex; flex-wrap: wrap; gap: 4px; }}
    .chip {{
      padding: 2px 6px;
      border-radius: 4px;
      font-size: 11px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    }}
    .chip-dep {{ background: var(--secondary); color: var(--secondary-foreground); }}
    .chip-file {{ border: 1px solid var(--border); color: var(--muted-foreground); }}
    .node-note {{ font-size: 12px; color: var(--muted-foreground); word-break: break-word; }}
    .gate-note {{
      font-size: 12px;
      color: var(--muted-foreground);
      padding-top: 8px;
      border-top: 1px solid var(--card-border);
    }}
    .badge {{
      flex-shrink: 0;
      padding: 2px 8px;
      border-radius: 999px;
      border: 1px solid var(--border);
      font-size: 11px;
      font-weight: 500;
      color: var(--muted-foreground);
    }}
    .badge-pending {{ border-color: var(--border); color: var(--muted-foreground); }}
    .badge-building {{ border-color: var(--primary); color: var(--primary); }}
    .badge-script {{ border-color: var(--primary); color: var(--primary); }}
    .badge-arena {{ border-color: var(--warning); color: var(--warning); }}
    .badge-interrogating {{ border-color: var(--warning); color: var(--warning); }}
    .badge-rejected {{ border-color: var(--error); color: var(--error); }}
    .badge-verified {{ border-color: var(--success); color: var(--success); }}
  </style>
</head>
<body>
  <div class="page">
    <header class="header-card">
      <div>
        <div class="eyebrow mono">PODO-STUFF · PHASED DAG ORCHESTRATOR</div>
        <h1 class="page-title">{html.escape(state["title"])}</h1>
        <div class="subtitle">
          Tier 1: Per-Task Deterministic Script (0 tokens) · Tier 2: Per-Phase Interrogator Gate ({html.escape(interrogator_model)})
        </div>
      </div>
      <div class="stats">
        <div class="stat-pill">
          <span class="muted">Tasks:</span>
          <span class="stat-value mono">{verified_nodes}/{total_nodes}</span>
        </div>
        <div class="stat-pill">
          <span class="muted">Active:</span>
          <span class="stat-value stat-primary mono">{active_nodes}</span>
        </div>
        <div class="stat-pill">
          <span class="muted">Rejected:</span>
          <span class="stat-value stat-error mono">{rejected_nodes}</span>
        </div>
        <div class="muted mono">Updated: {html.escape(state["updated_at"])}</div>
      </div>
    </header>
    <main class="page">
      {"".join(phase_cards_html)}
    </main>
  </div>
</body>
</html>
"""


def write_dag_outputs(
    state: dict[str, Any],
    state_path: Path,
    cfg: dict[str, Any] | None = None,
) -> None:
    """Persist DAG JSON state and re-render configured Markdown and HTML views."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    md_out = state.get("md_out")
    if md_out:
        md_path = Path(md_out)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(render_dag_markdown(state, cfg=cfg), encoding="utf-8")

    html_out = state.get("html_out")
    if html_out:
        html_path = Path(html_out)
        html_path.parent.mkdir(parents=True, exist_ok=True)
        html_path.write_text(render_dag_html(state, cfg=cfg), encoding="utf-8")


def cmd_dag(args: argparse.Namespace) -> int:
    """Initialize or update the live Podo Swarm DAG state and artifacts."""
    state_path = Path(args.state_file).resolve()
    cfg = load_config(Path(args.config).resolve() if args.config else DEFAULT_CONFIG_PATH)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if args.dag_action == "init":
        raw_arg = args.phases_json
        existing = safe_existing_file(raw_arg)
        if existing is not None:
            raw_json = json.loads(existing.read_text(encoding="utf-8"))
        else:
            raw_json = json.loads(raw_arg)
        phases = normalize_phases(raw_json)
        md_out = str(Path(args.md_out).resolve()) if args.md_out else str(state_path.with_suffix(".md"))
        html_out = str(Path(args.html_out).resolve()) if args.html_out else str(state_path.with_suffix(".html"))
        state = {
            "title": args.title,
            "updated_at": now,
            "md_out": md_out,
            "html_out": html_out,
            "phases": phases,
        }
        write_dag_outputs(state, state_path, cfg=cfg)
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
    for ph in state.get("phases", []):
        if args.phase and ph.get("id") == args.phase:
            if args.gate_status:
                ph["gate_status"] = args.gate_status
            if args.note is not None:
                ph["gate_note"] = args.note
            updated = True
        for node in ph.get("nodes", []):
            node_id = node.get("id")
            if args.node and node_id == args.node:
                if args.status:
                    node["status"] = args.status
                if args.worker is not None:
                    node["worker"] = args.worker
                if args.model is not None:
                    node["model"] = args.model
                if args.note is not None:
                    node["note"] = args.note
                updated = True
            if node_id in batch_updates:
                node["status"] = batch_updates[node_id]
                matched_batch.add(node_id)
                updated = True

    missing_nodes = sorted(set(batch_updates.keys()) - matched_batch)
    if missing_nodes:
        print(f"Error: target node(s) not found in DAG state: {', '.join(missing_nodes)}", file=sys.stderr)
        return 2

    if not updated:
        print("Error: target --node, --set-nodes, or --phase not found in DAG state", file=sys.stderr)
        return 2

    state["updated_at"] = now
    write_dag_outputs(state, state_path, cfg=cfg)
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


def handle_hook_stop(config_path: Path = DEFAULT_CONFIG_PATH) -> int:
    """Enforce deterministic no-comments, dead-code, and codified checks on Antigravity Stop hook."""
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
    if isinstance(transcript_raw, str) and transcript_raw.strip():
        turn_info = inspect_turn_transcript(Path(transcript_raw.strip()), repo)
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

    sub = parser.add_subparsers(dest="command")

    p_check = sub.add_parser("check", help="Run deterministic comment, dead-code, and structural checks")
    p_check.add_argument("--repo", default=".", help="Repository path to inspect")
    p_check.add_argument("--diff-text", help="Unified diff file path, '-' for stdin, or raw diff string")
    p_check.add_argument("--base", help="Git base ref for inspecting committed + workspace changes")
    p_check.add_argument("--files", help="Comma-separated repo-relative paths to scope checks to")
    p_check.add_argument("--mode", choices=("enforce", "observe"), default="enforce", help="Enforcement mode")
    p_check.add_argument("--run-verify", action="store_true", help="Also execute .podo/verify.sh if present")

    p_codify = sub.add_parser("codify", help="Add a deterministic check to .podo/checks.json")
    p_codify.add_argument("--repo", default=".", help="Repository path")
    p_codify.add_argument("--id", required=True, help="Unique check identifier")
    p_codify.add_argument("--regex", help="Forbidden regex pattern")
    p_codify.add_argument("--glob", default="*", help="File glob for --regex")
    p_codify.add_argument("--cmd", help="Required shell command that must exit 0")
    p_codify.add_argument("--fix", required=True, help="Actionable remediation message")

    p_dag = sub.add_parser("dag", help="Initialize or update live DAG state and artifacts")
    dag_sub = p_dag.add_subparsers(dest="dag_action", required=True)

    p_dag_init = dag_sub.add_parser("init")
    p_dag_init.add_argument("--state-file", required=True)
    p_dag_init.add_argument("--title", required=True)
    p_dag_init.add_argument("--md-out")
    p_dag_init.add_argument("--html-out")
    p_dag_init.add_argument("--phases-json", required=True)

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
    p_dag_upd.add_argument("--md-out")
    p_dag_upd.add_argument("--html-out")

    p_cfg = sub.add_parser("config", help="Inspect or update role models and gate modes")
    p_cfg.add_argument("--show", action="store_true")
    p_cfg.add_argument("--reset", action="store_true")
    p_cfg.add_argument("--preset")
    p_cfg.add_argument("--mode", choices=VALID_STOP_MODES)
    p_cfg.add_argument("--set", action="append")

    return parser


def main() -> int:
    """Entry point for CLI and lifecycle hook invocations."""
    parser = build_parser()
    args = parser.parse_args()

    if args.hook_stop:
        config_path = Path(args.config).resolve() if args.config else DEFAULT_CONFIG_PATH
        return handle_hook_stop(config_path=config_path)
    if args.command == "check":
        return cmd_check(args)
    if args.command == "codify":
        return cmd_codify(args)
    if args.command == "dag":
        return cmd_dag(args)
    if args.command == "config":
        return cmd_config(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
