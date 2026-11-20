#!/usr/bin/env python3
"""Deterministic guardrails, AST narration-comment gate, DAG renderer, and hook engine for podo-stuff."""

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
import tokenize
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PLUGIN_ROOT / "podo-config.json"

DEFAULT_ALLOW_TAGS = ("WHY:", "INVARIANT:", "SAFETY:", "NOTE(popododo):")

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
    "/**",
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

BRACE_STATUS_LABELS = {
    "pending": ("Pending", "border-[var(--border)] text-[var(--muted-foreground)]"),
    "building": ("Building", "border-[var(--primary)] text-[var(--primary)]"),
    "script_checked": ("Script Checked", "border-[var(--chart-2)] text-[var(--chart-2)]"),
    "arena_running": ("Arena Competing", "border-[var(--warning)] text-[var(--warning)]"),
    "phase_interrogating": ("Opus Interrogating", "border-[var(--warning)] text-[var(--warning)]"),
    "rejected": ("Rejected", "border-[var(--error)] text-[var(--error)]"),
    "verified": ("Verified", "border-[var(--success)] text-[var(--success)]"),
}

MERMAID_STATUS_BADGE = {
    "pending": "⏳ pending",
    "building": "🔨 building",
    "script_checked": "🧪 script-ok",
    "arena_running": "⚔️ arena",
    "phase_interrogating": "🔍 opus-check",
    "rejected": "🔁 rejected",
    "verified": "✅ verified",
}


def load_config(config_path: Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    """Load podo-config.json from the plugin directory or fallback defaults."""
    if config_path.is_file():
        return json.loads(config_path.read_text(encoding="utf-8"))
    return {
        "active_preset": "podo-default",
        "roles": {
            "scout": "gemini-4-hydrogen-medium",
            "builder": "gemini-pro-orchestrator-neon",
            "arena_a": "gemini-pro-orchestrator-neon",
            "arena_b": "claude-opus-5-5-medium",
            "interrogator": "claude-opus-5-5-medium",
            "monitor": "gemini-4-hydrogen-low",
        },
        "presets": {},
        "gates": {
            "per_task_deterministic_script": True,
            "interrogator_cadence": "per_dag_phase",
            "allow_comment_tags": list(DEFAULT_ALLOW_TAGS),
        },
    }


def save_config(cfg: dict[str, Any], config_path: Path = DEFAULT_CONFIG_PATH) -> None:
    """Persist updated configuration to podo-config.json."""
    config_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def get_allow_tags(cfg: dict[str, Any]) -> tuple[str, ...]:
    """Return allowed rationale prefixes from configuration."""
    tags = cfg.get("gates", {}).get("allow_comment_tags")
    if isinstance(tags, list) and tags:
        return tuple(str(t) for t in tags)
    return DEFAULT_ALLOW_TAGS


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
        return text.strip(" *")
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

    for pattern in DEAD_CODE_PATTERNS:
        if pattern.search(body):
            return False, "Commented-out code detected; delete dead code instead of commenting it out", True

    for tag in allow_tags:
        if body.startswith(tag):
            return True, "", False

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
        source_for_tokens = full_source
        line_offset = 0
    else:
        ordered = sorted(added_lines.items())
        source_for_tokens = "\n".join(text for _, text in ordered)
        func_ranges = python_function_ranges(source_for_tokens)
        header_end = python_module_header_end(source_for_tokens) if func_ranges is not None else 0
        line_offset = (ordered[0][0] - 1) if ordered else 0

    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source_for_tokens).readline))
    except (tokenize.TokenError, IndentationError):
        tokens = []

    if tokens:
        line_map = (
            {i + 1: text for i, text in enumerate(source_for_tokens.splitlines())}
            if full_source is not None
            else {idx + 1: text for idx, (_, text) in enumerate(sorted(added_lines.items()))}
        )
        ordered_lines = sorted(added_lines.keys())
        for tok in tokens:
            if tok.type != tokenize.COMMENT:
                continue
            tok_line = tok.start[0]
            actual_line = tok_line if full_source is not None else (ordered_lines[tok_line - 1] if tok_line <= len(ordered_lines) else tok_line + line_offset)
            if actual_line not in added_lines:
                continue

            raw_line = line_map.get(tok_line, "")
            is_indented = (len(raw_line) - len(raw_line.lstrip(" \t"))) > 0
            in_function = (
                any(start <= tok_line <= end for start, end in func_ranges)
                if func_ranges is not None
                else is_indented
            )
            in_header = tok_line <= header_end and not is_indented
            ok, reason, always_block = is_allowed_comment(
                tok.string,
                allow_tags,
                allow_bare_header=(in_header and not in_function),
            )
            if not ok and (in_function or is_indented or always_block):
                violations.append(f"[NO-COMMENTS] {rel_path}:{actual_line}: {tok.string.strip()} -> {reason}")
        return violations

    for line_no, text in sorted(added_lines.items()):
        stripped = text.lstrip(" \t")
        if not stripped.startswith("#"):
            continue
        is_indented = len(text) > len(stripped)
        ok, reason, always_block = is_allowed_comment(stripped, allow_tags, allow_bare_header=not is_indented)
        if not ok and (is_indented or always_block):
            violations.append(f"[NO-COMMENTS] {rel_path}:{line_no}: {stripped} -> {reason}")
    return violations


def scan_brace_language_comments(source: str) -> list[tuple[int, str, int, bool]]:
    """Extract (line_no, comment_text, brace_depth, is_header) from C-style brace code."""
    results: list[tuple[int, str, int, bool]] = []
    depth = 0
    seen_code = False
    i = 0
    n = len(source)
    line_no = 1
    in_string: str | None = None
    in_line_comment = False
    in_block_comment = False
    block_is_jsdoc = False
    comment_start_line = 1
    comment_start_depth = 0
    comment_buf: list[str] = []

    while i < n:
        ch = source[i]
        nxt = source[i + 1] if i + 1 < n else ""

        if in_line_comment:
            if ch == "\n":
                results.append((comment_start_line, "".join(comment_buf), comment_start_depth, not seen_code))
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
                if not block_is_jsdoc:
                    results.append((comment_start_line, "".join(comment_buf), comment_start_depth, not seen_code))
                in_block_comment = False
                block_is_jsdoc = False
                comment_buf = []
                i += 2
                continue
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
            elif ch == "\n":
                line_no += 1
            i += 1
            continue

        if ch in ('"', "'", "`"):
            seen_code = True
            in_string = ch
            i += 1
            continue

        if ch == "/" and nxt == "/":
            in_line_comment = True
            comment_start_line = line_no
            comment_start_depth = depth
            comment_buf = ["//"]
            i += 2
            continue

        if ch == "/" and nxt == "*":
            in_block_comment = True
            block_is_jsdoc = (i + 2 < n and source[i + 2] == "*")
            comment_start_line = line_no
            comment_start_depth = depth
            comment_buf = ["/*"]
            i += 2
            continue

        if ch == "{":
            seen_code = True
            depth += 1
        elif ch == "}":
            seen_code = True
            depth = max(0, depth - 1)
        elif ch == "\n":
            line_no += 1
        elif not ch.isspace():
            seen_code = True

        i += 1

    if in_line_comment and comment_buf:
        results.append((comment_start_line, "".join(comment_buf), comment_start_depth, not seen_code))
    return results


def check_brace_comments(
    rel_path: str,
    added_lines: dict[int, str],
    full_source: str | None,
    allow_tags: tuple[str, ...],
) -> list[str]:
    """Check added brace-language lines for forbidden inline narration or placeholder comments."""
    violations: list[str] = []
    if full_source is not None:
        scanned = scan_brace_language_comments(full_source)
        for line_no, comment_text, depth, is_header in scanned:
            if line_no not in added_lines:
                continue
            ok, reason, always_block = is_allowed_comment(
                comment_text,
                allow_tags,
                allow_bare_header=(is_header and depth == 0),
            )
            if not ok and (depth >= 1 or always_block):
                violations.append(f"[NO-COMMENTS] {rel_path}:{line_no}: {comment_text.strip()} -> {reason}")
        return violations

    depth = 0
    for line_no, text in sorted(added_lines.items()):
        stripped = text.strip()
        is_indented = len(text) > len(text.lstrip(" \t"))
        if stripped.startswith("//") or stripped.startswith("/*"):
            in_body = depth >= 1 or is_indented
            ok, reason, always_block = is_allowed_comment(stripped, allow_tags, allow_bare_header=not in_body)
            if not ok and (in_body or always_block):
                violations.append(f"[NO-COMMENTS] {rel_path}:{line_no}: {stripped} -> {reason}")
        depth = max(0, depth + text.count("{") - text.count("}"))
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


def collect_git_added_lines(repo: Path) -> dict[str, dict[int, str]]:
    """Collect added lines from tracked git diff plus untracked source files."""
    if not (repo / ".git").exists():
        return {}

    has_head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "HEAD"],
        capture_output=True,
        text=True,
    ).returncode == 0

    diff_cmd = (
        ["git", "-C", str(repo), "diff", "HEAD", "--unified=0", "--no-color"]
        if has_head
        else ["git", "-C", str(repo), "diff", "--cached", "--unified=0", "--no-color"]
    )
    diff_proc = subprocess.run(diff_cmd, capture_output=True, text=True)
    added_by_file = parse_unified_diff(diff_proc.stdout) if diff_proc.returncode == 0 else {}

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
            if p.suffix not in SOURCE_EXTENSIONS or not p.is_file() or p.stat().st_size > 512_000:
                continue
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            added_by_file[rel] = {idx + 1: text for idx, text in enumerate(lines)}

    return added_by_file


def run_comment_gate(repo: Path, allow_tags: tuple[str, ...], diff_text: str | None = None) -> list[str]:
    """Run deterministic narration-comment checks across diff or workspace changes."""
    if diff_text is not None:
        added_by_file = parse_unified_diff(diff_text)
        use_disk = False
    else:
        added_by_file = collect_git_added_lines(repo)
        use_disk = True

    violations: list[str] = []
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
        elif suffix in SHELL_EXTENSIONS:
            violations.extend(check_shell_comments(rel_path, added_lines, allow_tags))
        elif suffix in BRACE_EXTENSIONS:
            violations.extend(check_brace_comments(rel_path, added_lines, full_source, allow_tags))
    return violations


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


def run_codified_checks(repo: Path, cmd_timeout: int = 30) -> list[str]:
    """Execute structural checks stored in <repo>/.podo/checks.json."""
    checks_file = repo / ".podo" / "checks.json"
    if not checks_file.is_file():
        return []

    data = json.loads(checks_file.read_text(encoding="utf-8"))
    checks = data.get("checks", [])
    violations: list[str] = []
    repo_files: list[Path] | None = None

    for item in checks:
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
            try:
                proc = subprocess.run(
                    command,
                    shell=True,
                    cwd=str(repo),
                    capture_output=True,
                    text=True,
                    timeout=cmd_timeout,
                )
            except subprocess.TimeoutExpired:
                violations.append(
                    f"[STRUCTURAL-CHECK:{check_id}] Command timed out ({cmd_timeout}s): {command} -> {fix_msg}"
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
    """Execute deterministic comment, structural check, and optional verify.sh gates."""
    repo = Path(args.repo).resolve()
    cfg = load_config(Path(args.config).resolve() if args.config else DEFAULT_CONFIG_PATH)
    allow_tags = get_allow_tags(cfg)
    diff_text = resolve_diff_text_arg(args.diff_text)

    violations: list[str] = []
    violations.extend(run_comment_gate(repo, allow_tags, diff_text=diff_text))
    violations.extend(run_codified_checks(repo))
    if args.run_verify:
        violations.extend(run_verify_script(repo))

    if violations:
        for line in violations:
            print(line)
        return 1

    print("[podo-guard] OK: 0 violations (no-comments & structural checks passed).")
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
        data = json.loads(checks_file.read_text(encoding="utf-8"))
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


def render_dag_markdown(state: dict[str, Any]) -> str:
    """Render DAG state into a Markdown summary with Mermaid flowchart and phase gate table."""
    lines = [
        f"# Podo Swarm DAG — {state['title']}",
        "",
        f"- **Updated**: `{state['updated_at']}`",
        "- **Tier 1 (Per-Task)**: `podo-guard.py check` (0 tokens)",
        "- **Tier 2 (Per-Phase Batch)**: `Claude Opus 5.5 (Medium)` Interrogator",
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
        lines.append(f"    {gate_node}{{\"Opus Phase Gate<br/>{gate_badge}\"}}")
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
            f"| {ph['name']} | **Phase Gate (Interrogator)** | `interrogator` | `claude-opus-5-5-medium` | {gate_badge} | {ph.get('gate_note') or '-'} |"
        )

    lines.append("")
    return "\n".join(lines)


def render_dag_html(state: dict[str, Any]) -> str:
    """Render DAG state into a standalone Tailwind HTML dashboard with Antigravity theme tokens."""
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
        gate_label, gate_cls = BRACE_STATUS_LABELS.get(
            ph.get("gate_status", "pending"),
            (ph.get("gate_status", "pending"), "border-[var(--border)] text-[var(--muted-foreground)]"),
        )
        node_cards: list[str] = []
        for node in ph.get("nodes", []):
            st_label, st_cls = BRACE_STATUS_LABELS.get(
                node.get("status", "pending"),
                (node.get("status", "pending"), "border-[var(--border)] text-[var(--muted-foreground)]"),
            )
            deps_html = "".join(
                f'<span class="px-1.5 py-0.5 rounded text-xs bg-[var(--secondary)] text-[var(--secondary-foreground)] font-mono">← {html.escape(d)}</span>'
                for d in node.get("depends_on", [])
            )
            files_html = "".join(
                f'<span class="px-1.5 py-0.5 rounded text-xs border border-[var(--border)] text-[var(--muted-foreground)] font-mono">{html.escape(f)}</span>'
                for f in node.get("files", [])
            )
            note_html = (
                f'<div class="mt-2 text-xs text-[var(--muted-foreground)] break-words">{html.escape(node["note"])}</div>'
                if node.get("note")
                else ""
            )
            node_cards.append(
                f"""
                <div class="rounded-lg bg-[var(--background)] border border-[var(--card-border)] p-3 flex flex-col justify-between gap-2">
                  <div class="flex items-start justify-between gap-2">
                    <div>
                      <div class="text-xs font-mono text-[var(--muted-foreground)]">{html.escape(node["id"])}</div>
                      <div class="text-sm font-medium text-[var(--foreground)] mt-0.5">{html.escape(node["title"])}</div>
                    </div>
                    <span class="shrink-0 px-2 py-0.5 rounded-full border text-xs font-medium {st_cls}">{html.escape(st_label)}</span>
                  </div>
                  <div class="flex flex-wrap items-center gap-1.5 text-xs text-[var(--muted-foreground)]">
                    <span class="font-mono text-[var(--foreground)]">@{html.escape(node.get("worker") or "unassigned")}</span>
                    <span>·</span>
                    <span class="font-mono">{html.escape(node.get("model") or "inherit")}</span>
                  </div>
                  {f'<div class="flex flex-wrap gap-1">{deps_html}{files_html}</div>' if (deps_html or files_html) else ''}
                  {note_html}
                </div>
                """
            )

        phase_cards_html.append(
            f"""
            <section class="rounded-xl bg-[var(--card)] border border-[var(--border)] p-4 space-y-3">
              <div class="flex flex-wrap items-center justify-between gap-2 pb-2 border-b border-[var(--card-border)]">
                <div class="flex items-center gap-2">
                  <span class="px-2 py-0.5 rounded bg-[var(--secondary)] text-[var(--secondary-foreground)] text-xs font-mono">{html.escape(ph["id"])}</span>
                  <h2 class="text-sm font-semibold text-[var(--foreground)]">{html.escape(ph["name"])}</h2>
                </div>
                <div class="flex items-center gap-2 text-xs">
                  <span class="text-[var(--muted-foreground)]">Opus 5.5 Phase Gate:</span>
                  <span class="px-2 py-0.5 rounded-full border font-medium {gate_cls}">{html.escape(gate_label)}</span>
                </div>
              </div>
              <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
                {"".join(node_cards)}
              </div>
              {f'<div class="text-xs text-[var(--muted-foreground)] pt-1 border-t border-[var(--card-border)]">Gate Note: {html.escape(ph["gate_note"])}</div>' if ph.get("gate_note") else ''}
            </section>
            """
        )

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Podo Swarm DAG — {html.escape(state["title"])}</title>
  <script src="https://www.gstatic.com/antigravity/web/dev/tailwindcss.min.js"></script>
</head>
<body class="bg-[var(--background)] text-[var(--foreground)] antialiased p-4 md:p-6 space-y-4">
  <header class="rounded-xl bg-[var(--card)] border border-[var(--border)] p-4 flex flex-wrap items-center justify-between gap-4">
    <div>
      <div class="text-xs font-mono text-[var(--primary)]">PODO-STUFF · PHASED DAG ORCHESTRATOR</div>
      <h1 class="text-lg font-semibold text-[var(--foreground)] mt-0.5">{html.escape(state["title"])}</h1>
      <div class="text-xs text-[var(--muted-foreground)] mt-1">
        Tier 1: Per-Task Deterministic Script (0 tokens) · Tier 2: Per-Phase Claude Opus 5.5 Batch Interrogator
      </div>
    </div>
    <div class="flex flex-wrap items-center gap-3 text-xs">
      <div class="px-3 py-1.5 rounded-lg bg-[var(--background)] border border-[var(--border)]">
        <span class="text-[var(--muted-foreground)]">Tasks:</span>
        <span class="font-mono font-semibold text-[var(--foreground)] ml-1">{verified_nodes}/{total_nodes}</span>
      </div>
      <div class="px-3 py-1.5 rounded-lg bg-[var(--background)] border border-[var(--border)]">
        <span class="text-[var(--muted-foreground)]">Active:</span>
        <span class="font-mono font-semibold text-[var(--primary)] ml-1">{active_nodes}</span>
      </div>
      <div class="px-3 py-1.5 rounded-lg bg-[var(--background)] border border-[var(--border)]">
        <span class="text-[var(--muted-foreground)]">Rejected:</span>
        <span class="font-mono font-semibold text-[var(--error)] ml-1">{rejected_nodes}</span>
      </div>
      <div class="text-[var(--muted-foreground)] font-mono">Updated: {html.escape(state["updated_at"])}</div>
    </div>
  </header>
  <main class="space-y-4">
    {"".join(phase_cards_html)}
  </main>
</body>
</html>
"""


def write_dag_outputs(state: dict[str, Any], state_path: Path) -> None:
    """Persist DAG JSON state and re-render configured Markdown and HTML views."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    md_out = state.get("md_out")
    if md_out:
        md_path = Path(md_out)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(render_dag_markdown(state), encoding="utf-8")

    html_out = state.get("html_out")
    if html_out:
        html_path = Path(html_out)
        html_path.parent.mkdir(parents=True, exist_ok=True)
        html_path.write_text(render_dag_html(state), encoding="utf-8")


def cmd_dag(args: argparse.Namespace) -> int:
    """Initialize or update the live Podo Swarm DAG state and artifacts."""
    state_path = Path(args.state_file).resolve()
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
        write_dag_outputs(state, state_path)
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

    updated = False
    for ph in state.get("phases", []):
        if args.phase and ph.get("id") == args.phase:
            if args.gate_status:
                ph["gate_status"] = args.gate_status
            if args.note is not None:
                ph["gate_note"] = args.note
            updated = True
        if args.node:
            for node in ph.get("nodes", []):
                if node.get("id") == args.node:
                    if args.status:
                        node["status"] = args.status
                    if args.worker is not None:
                        node["worker"] = args.worker
                    if args.model is not None:
                        node["model"] = args.model
                    if args.note is not None:
                        node["note"] = args.note
                    updated = True

    if not updated:
        print("Error: target --node or --phase not found in DAG state", file=sys.stderr)
        return 2

    state["updated_at"] = now
    write_dag_outputs(state, state_path)
    print(f"[podo-guard] Updated DAG state={state_path}")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    """Display or update podo-config.json presets and role model mappings."""
    config_path = Path(args.config).resolve() if args.config else DEFAULT_CONFIG_PATH
    cfg = load_config(config_path)
    changed = False

    if args.preset:
        presets = cfg.get("presets", {})
        if args.preset not in presets:
            print(f"Error: unknown preset '{args.preset}'. Available: {', '.join(presets.keys())}", file=sys.stderr)
            return 2
        cfg["active_preset"] = args.preset
        cfg["roles"] = dict(presets[args.preset]["roles"])
        changed = True

    for assignment in args.set or []:
        if "=" not in assignment:
            print(f"Error: --set expects role=model, got '{assignment}'", file=sys.stderr)
            return 2
        role, model = assignment.split("=", 1)
        role = role.strip()
        model = model.strip()
        cfg.setdefault("roles", {})[role] = model
        cfg["active_preset"] = "custom"
        changed = True

    if changed:
        save_config(cfg, config_path)

    if args.show or not changed:
        print(json.dumps(cfg, indent=2, ensure_ascii=False))
    return 0


def handle_hook_post_tool() -> int:
    """Consume PostToolUse hook stdin payload and return empty JSON."""
    sys.stdin.read()
    print("{}")
    return 0


def handle_hook_stop() -> int:
    """Enforce deterministic no-comments and codified checks on Antigravity Stop hook."""
    raw = sys.stdin.read().strip()
    payload = json.loads(raw) if raw else {}

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
    cfg = load_config(DEFAULT_CONFIG_PATH)
    allow_tags = get_allow_tags(cfg)

    violations: list[str] = []
    violations.extend(run_comment_gate(repo, allow_tags))
    violations.extend(run_codified_checks(repo, cmd_timeout=8))

    if violations:
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
    parser.add_argument("--hook-post-tool", action="store_true", help="Run as Antigravity PostToolUse hook")

    sub = parser.add_subparsers(dest="command")

    p_check = sub.add_parser("check", help="Run deterministic comment and structural checks")
    p_check.add_argument("--repo", default=".", help="Repository path to inspect")
    p_check.add_argument("--diff-text", help="Unified diff file path, '-' for stdin, or raw diff string")
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
    p_dag_upd.add_argument("--phase")
    p_dag_upd.add_argument("--gate-status")
    p_dag_upd.add_argument("--worker")
    p_dag_upd.add_argument("--model")
    p_dag_upd.add_argument("--note")
    p_dag_upd.add_argument("--md-out")
    p_dag_upd.add_argument("--html-out")

    p_cfg = sub.add_parser("config", help="Inspect or update role model presets")
    p_cfg.add_argument("--show", action="store_true")
    p_cfg.add_argument("--preset")
    p_cfg.add_argument("--set", action="append")

    return parser


def main() -> int:
    """Entry point for CLI and lifecycle hook invocations."""
    parser = build_parser()
    args = parser.parse_args()

    if args.hook_stop:
        return handle_hook_stop()
    if args.hook_post_tool:
        return handle_hook_post_tool()
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
