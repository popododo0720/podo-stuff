#!/usr/bin/env python3
"""Unit tests for podo-guard deterministic comment, dead-code, DAG, config, and Stop hook gates."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
GUARD_SCRIPT = PLUGIN_ROOT / "scripts" / "podo-guard.py"


def load_guard_module() -> Any:
    """Dynamically load scripts/podo-guard.py as a Python module for direct testing."""
    spec = importlib.util.spec_from_file_location("podo_guard", GUARD_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GUARD = load_guard_module()


def run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Execute a git command in repo with deterministic author identity."""
    env = dict(os.environ)
    env.setdefault("GIT_AUTHOR_NAME", "Test User")
    env.setdefault("GIT_AUTHOR_EMAIL", "test@example.com")
    env.setdefault("GIT_COMMITTER_NAME", "Test User")
    env.setdefault("GIT_COMMITTER_EMAIL", "test@example.com")
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )


def init_git_repo(repo: Path) -> None:
    """Initialize a temporary git repository ready for commits."""
    run_git(repo, "init")
    run_git(repo, "config", "user.name", "Test User")
    run_git(repo, "config", "user.email", "test@example.com")


def run_cli(
    args: list[str],
    stdin_text: str | None = None,
    env_overrides: dict[str, str | None] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Invoke scripts/podo-guard.py in a subprocess with clean environment controls."""
    env = dict(os.environ)
    for var in (
        "PODO_GUARD_MODE",
        "PODO_ROLE",
        "PODO_TELEMETRY_FILE",
        "PODO_PHASE",
        "PODO_WORKER",
        "PODO_CONVERSATION_ID",
    ):
        env.pop(var, None)
    if env_overrides:
        for key, val in env_overrides.items():
            if val is None:
                env.pop(key, None)
            else:
                env[key] = val
    return subprocess.run(
        [sys.executable, str(GUARD_SCRIPT), *args],
        input=stdin_text,
        capture_output=True,
        text=True,
        env=env,
    )


class CommentParserTests(unittest.TestCase):
    """Verify comment gate allow tags, Python tokenization, and brace-language scanners."""

    def test_allow_tags_checked_before_dead_code_patterns(self) -> None:
        tags = GUARD.DEFAULT_ALLOW_TAGS
        ok, reason, always_block = GUARD.is_allowed_comment(
            "// WHY: if cache misses; fall back",
            tags,
        )
        self.assertTrue(ok, msg=reason)
        self.assertFalse(always_block)

        ok_inv, _, _ = GUARD.is_allowed_comment("// INVARIANT: return early;", tags)
        self.assertTrue(ok_inv)

        ok_dead, dead_reason, dead_always = GUARD.is_allowed_comment(
            "// if cache misses; fall back",
            tags,
        )
        self.assertFalse(ok_dead)
        self.assertTrue(dead_always)
        self.assertIn("Commented-out code", dead_reason)

        ok_todo, _, todo_always = GUARD.is_allowed_comment(
            "// WHY: TODO clean up later",
            tags,
        )
        self.assertFalse(ok_todo)
        self.assertTrue(todo_always)

    def test_python_partial_dedent_diff_trailing_comment_and_string_literal(self) -> None:
        diff_text = "\n".join(
            [
                "+++ b/app.py",
                "@@ -10,0 +11,3 @@",
                '+    url = "https://example.com/#fragment"',
                "+    total = compute()  # trailing narration",
                "+    other = compute()  # WHY: needed for protocol;",
            ]
        )
        violations = GUARD.run_comment_gate(
            Path("."),
            GUARD.DEFAULT_ALLOW_TAGS,
            diff_text=diff_text,
        )
        self.assertEqual(len(violations), 1)
        self.assertIn("app.py:12:", violations[0])
        self.assertIn("# trailing narration", violations[0])

    def test_python_full_file_relies_on_func_ranges_not_generic_indentation(self) -> None:
        source = "\n".join(
            [
                "CONFIG = {",
                "    # section label outside function",
                '    "alpha": 1,',
                "}",
                "def run():",
                "    # narration inside function",
                "    return CONFIG",
            ]
        )
        added = {i + 1: line for i, line in enumerate(source.splitlines())}
        violations = GUARD.check_python_comments("cfg.py", added, source, GUARD.DEFAULT_ALLOW_TAGS)
        self.assertEqual(len(violations), 1)
        self.assertIn("cfg.py:6:", violations[0])

    def test_js_regex_literal_does_not_open_comment(self) -> None:
        js_source = "\n".join(
            [
                "function parseUrl(input) {",
                "  const re = /https?:\\/\\//gi;",
                "  const ok = /a[x/y]b/.test(input);",
                "  const ratio = 10 / 2; // narration after division",
                "  return re.test(input) && ok && ratio > 0;",
                "}",
            ]
        )
        added = {i + 1: line for i, line in enumerate(js_source.splitlines())}
        violations = GUARD.check_brace_comments("url.js", added, js_source, GUARD.DEFAULT_ALLOW_TAGS)
        self.assertEqual(len(violations), 1)
        self.assertIn("url.js:4:", violations[0])
        self.assertIn("narration after division", violations[0])

    def test_rust_lifetimes_and_escaped_chars_do_not_open_string_state(self) -> None:
        rs_source = "\n".join(
            [
                "fn parse<'a>(input: &'a str) -> &'static str {",
                "    let quote = '\\'';",
                "    let dbl = '\"';",
                "    // narration after lifetimes",
                "    let _ = (quote, dbl, input);",
                '    "ok"',
                "}",
            ]
        )
        added = {i + 1: line for i, line in enumerate(rs_source.splitlines())}
        violations = GUARD.check_brace_comments("lib.rs", added, rs_source, GUARD.DEFAULT_ALLOW_TAGS)
        self.assertEqual(len(violations), 1)
        self.assertIn("lib.rs:4:", violations[0])
        self.assertIn("narration after lifetimes", violations[0])

    def test_jsdoc_blocked_inside_function_and_multiline_block_emits_all_lines(self) -> None:
        ts_source = "\n".join(
            [
                "/** Top-level doc comment */",
                "export function work(): number {",
                "  /** JSDoc inside function body */",
                "  /*",
                "   * multi-line narration line one",
                "   * multi-line narration line two",
                "   */",
                "  /* WHY:",
                "   * intentional multi-line rationale",
                "   */",
                "  return 42;",
                "}",
            ]
        )
        scanned = GUARD.scan_brace_language_comments(ts_source, suffix=".ts")
        scanned_lines = {entry[0] for entry in scanned}
        self.assertNotIn(1, scanned_lines)
        self.assertIn(3, scanned_lines)
        self.assertTrue({4, 5, 6, 7}.issubset(scanned_lines))

        only_interior_added = {3: ts_source.splitlines()[2], 6: ts_source.splitlines()[5], 9: ts_source.splitlines()[8]}
        violations = GUARD.check_brace_comments("work.ts", only_interior_added, ts_source, GUARD.DEFAULT_ALLOW_TAGS)
        self.assertEqual(len(violations), 2)
        self.assertTrue(any("work.ts:3:" in v for v in violations))
        self.assertTrue(any("work.ts:6:" in v for v in violations))
        self.assertFalse(any("work.ts:9:" in v for v in violations))


class PythonDeadCodeGateTests(unittest.TestCase):
    """Verify AST-based Python dead-code detection and exemptions."""

    def test_detects_unreachable_unused_import_and_unused_private_helper(self) -> None:
        source = "\n".join(
            [
                "import os",
                "import math",
                "",
                "def _unused_helper(x: int) -> int:",
                "    return x + 1",
                "",
                "def _used_helper(x: int) -> int:",
                "    return x * 2",
                "",
                "def __dunder_hook__() -> int:",
                "    return 0",
                "",
                "def compute(val: int) -> int:",
                "    out = _used_helper(val) + int(math.sqrt(4))",
                "    return out",
                "    never_runs = 99",
                "    return never_runs",
            ]
        )
        added = {i + 1: line for i, line in enumerate(source.splitlines())}
        violations = GUARD.check_python_dead_code("module.py", added, source)
        self.assertTrue(any("[DEAD-CODE] module.py:1: Unused import 'os'" in v for v in violations), violations)
        self.assertTrue(
            any("[DEAD-CODE] module.py:4: Unused private helper function '_unused_helper'" in v for v in violations),
            violations,
        )
        self.assertTrue(
            any("[DEAD-CODE] module.py:16: Unreachable statement immediately following 'return'" in v for v in violations),
            violations,
        )
        self.assertFalse(any("_used_helper" in v for v in violations))
        self.assertFalse(any("__dunder_hook__" in v for v in violations))
        self.assertFalse(any("'math'" in v for v in violations))

    def test_unreachable_after_raise_break_continue(self) -> None:
        source = "\n".join(
            [
                "def f(items):",
                "    for x in items:",
                "        if x == 0:",
                "            continue",
                "            a = 1",
                "        if x < 0:",
                "            break",
                "            b = 2",
                "    raise ValueError('bad')",
                "    c = 3",
            ]
        )
        added = {i + 1: line for i, line in enumerate(source.splitlines())}
        violations = GUARD.check_python_dead_code("flow.py", added, source)
        self.assertEqual(len(violations), 3, violations)
        self.assertTrue(any("following 'continue'" in v for v in violations))
        self.assertTrue(any("following 'break'" in v for v in violations))
        self.assertTrue(any("following 'raise'" in v for v in violations))

    def test_unused_import_exemptions(self) -> None:
        source = "\n".join(
            [
                "from __future__ import annotations",
                "from typing import TYPE_CHECKING",
                "import json  # noqa: F401",
                "import pathlib",
                "",
                "if TYPE_CHECKING:",
                "    import decimal",
                "",
                '__all__ = ["pathlib"]',
            ]
        )
        added = {i + 1: line for i, line in enumerate(source.splitlines())}
        violations = GUARD.check_python_dead_code("pkg/mod.py", added, source)
        self.assertEqual(violations, [])

        init_source = "import unused_export\n"
        init_added = {1: "import unused_export"}
        init_violations = GUARD.check_python_dead_code("pkg/__init__.py", init_added, init_source)
        self.assertEqual(init_violations, [])

    def test_diff_text_runs_dead_code_when_valid_python(self) -> None:
        diff_text = "\n".join(
            [
                "+++ b/snippet.py",
                "@@ -0,0 +1,5 @@",
                "+import sys",
                "+def f():",
                "+    return 1",
                "+    x = 2",
            ]
        )
        violations = GUARD.run_comment_and_dead_code_gates(
            Path("."),
            GUARD.DEFAULT_ALLOW_TAGS,
            diff_text=diff_text,
            dead_code_enabled=True,
        )
        self.assertTrue(any("[DEAD-CODE] snippet.py:1: Unused import 'sys'" in v for v in violations))
        self.assertTrue(any("[DEAD-CODE] snippet.py:4: Unreachable" in v for v in violations))


class GitBaseCheckTests(unittest.TestCase):
    """Verify check --base <ref>, --files, and --mode observe on a git repository."""

    def test_check_base_detects_committed_changes_and_supports_files_and_observe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            init_git_repo(repo)
            (repo / "base.py").write_text("def clean() -> int:\n    return 1\n", encoding="utf-8")
            run_git(repo, "add", "base.py")
            run_git(repo, "commit", "-m", "initial")
            base_sha = run_git(repo, "rev-parse", "HEAD").stdout.strip()

            (repo / "feature.py").write_text(
                "import os\n\ndef work() -> int:\n    # narration committed\n    return 2\n",
                encoding="utf-8",
            )
            (repo / "clean_feature.py").write_text(
                "def ok() -> int:\n    return 3\n",
                encoding="utf-8",
            )
            run_git(repo, "add", "feature.py", "clean_feature.py")
            run_git(repo, "commit", "-m", "feature commit")

            no_base = run_cli(["check", "--repo", str(repo)])
            self.assertEqual(no_base.returncode, 0, no_base.stdout + no_base.stderr)

            with_base = run_cli(["check", "--repo", str(repo), "--base", base_sha])
            self.assertEqual(with_base.returncode, 1)
            self.assertIn("[NO-COMMENTS] feature.py:4:", with_base.stdout)
            self.assertIn("[DEAD-CODE] feature.py:1:", with_base.stdout)

            only_clean = run_cli(["check", "--repo", str(repo), "--base", base_sha, "--files", "clean_feature.py"])
            self.assertEqual(only_clean.returncode, 0, only_clean.stdout + only_clean.stderr)

            observe_proc = run_cli(["check", "--repo", str(repo), "--base", base_sha, "--mode", "observe"])
            self.assertEqual(observe_proc.returncode, 0)
            self.assertIn("OBSERVE", observe_proc.stdout)
            self.assertIn("feature.py:4:", observe_proc.stdout)

            bad_base = run_cli(["check", "--repo", str(repo), "--base", "doesnotexist123"])
            self.assertEqual(bad_base.returncode, 1)
            self.assertIn("[GIT-BASE] invalid base ref 'doesnotexist123'", bad_base.stdout)


class StopHookTests(unittest.TestCase):
    """Verify Stop hook turn-scoping, observe/off modes, role skip, malformed JSON, and deadline budget."""

    def _make_dirty_repo(self, repo: Path) -> None:
        init_git_repo(repo)
        (repo / "clean.py").write_text("def clean() -> int:\n    return 1\n", encoding="utf-8")
        (repo / "dirty.py").write_text("def dirty() -> int:\n    return 1\n", encoding="utf-8")
        run_git(repo, "add", "clean.py", "dirty.py")
        run_git(repo, "commit", "-m", "base")
        (repo / "clean.py").write_text("def clean() -> int:\n    return 2\n", encoding="utf-8")
        (repo / "dirty.py").write_text(
            "def dirty() -> int:\n    # bad narration\n    return 2\n",
            encoding="utf-8",
        )

    def test_read_only_turn_skipped_and_edited_turn_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            self._make_dirty_repo(repo)
            cfg_path = root / "podo-config.json"
            GUARD.save_config(GUARD.default_config(), cfg_path)

            ro_transcript = root / "ro.jsonl"
            ro_transcript.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "USER_INPUT", "content": "explain clean.py"}),
                        json.dumps(
                            {
                                "type": "PLANNER_RESPONSE",
                                "tool_calls": [{"name": "read_file", "args": {"Path": json.dumps(str(repo / "clean.py"))}}],
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            ro_payload = json.dumps(
                {
                    "terminationReason": "NO_TOOL_CALL",
                    "cwd": str(repo),
                    "transcriptPath": str(ro_transcript),
                }
            )
            ro_proc = run_cli(["--config", str(cfg_path), "--hook-stop"], stdin_text=ro_payload)
            self.assertEqual(ro_proc.returncode, 0)
            self.assertEqual(json.loads(ro_proc.stdout), {})

            clean_edit_transcript = root / "clean_edit.jsonl"
            clean_edit_transcript.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "USER_INPUT", "content": "edit clean.py"}),
                        json.dumps(
                            {
                                "type": "PLANNER_RESPONSE",
                                "tool_calls": [{"name": "edit_file", "args": {"Path": json.dumps(str(repo / "clean.py"))}}],
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            clean_payload = json.dumps(
                {
                    "terminationReason": "NO_TOOL_CALL",
                    "cwd": str(repo),
                    "transcriptPath": str(clean_edit_transcript),
                }
            )
            clean_proc = run_cli(["--config", str(cfg_path), "--hook-stop"], stdin_text=clean_payload)
            self.assertEqual(clean_proc.returncode, 0)
            self.assertEqual(json.loads(clean_proc.stdout), {})

            dirty_edit_transcript = root / "dirty_edit.jsonl"
            dirty_edit_transcript.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "USER_INPUT", "content": "edit dirty.py"}),
                        json.dumps(
                            {
                                "type": "PLANNER_RESPONSE",
                                "tool_calls": [{"name": "write_file", "args": {"Path": str(repo / "dirty.py")}}],
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            dirty_payload = json.dumps(
                {
                    "terminationReason": "NO_TOOL_CALL",
                    "cwd": str(repo),
                    "transcriptPath": str(dirty_edit_transcript),
                }
            )
            dirty_proc = run_cli(["--config", str(cfg_path), "--hook-stop"], stdin_text=dirty_payload)
            self.assertEqual(dirty_proc.returncode, 0)
            parsed = json.loads(dirty_proc.stdout)
            self.assertEqual(parsed.get("decision"), "continue")
            self.assertIn("dirty.py:2:", parsed.get("reason", ""))

    def test_observe_mode_role_skip_and_malformed_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            self._make_dirty_repo(repo)
            (repo / ".podo").mkdir()
            cfg_path = root / "podo-config.json"
            GUARD.save_config(GUARD.default_config(), cfg_path)

            payload = json.dumps({"terminationReason": "NO_TOOL_CALL", "cwd": str(repo)})

            obs_proc = run_cli(
                ["--config", str(cfg_path), "--hook-stop"],
                stdin_text=payload,
                env_overrides={"PODO_GUARD_MODE": "observe"},
            )
            self.assertEqual(obs_proc.returncode, 0)
            self.assertEqual(json.loads(obs_proc.stdout), {})
            self.assertIn("dirty.py:2:", obs_proc.stderr)
            observe_log = repo / ".podo" / "last-observe.log"
            self.assertTrue(observe_log.is_file())
            self.assertIn("dirty.py:2:", observe_log.read_text(encoding="utf-8"))

            role_proc = run_cli(
                ["--config", str(cfg_path), "--hook-stop"],
                stdin_text=payload,
                env_overrides={"PODO_ROLE": "scout"},
            )
            self.assertEqual(role_proc.returncode, 0)
            self.assertEqual(json.loads(role_proc.stdout), {})

            bad_stdin_proc = run_cli(["--config", str(cfg_path), "--hook-stop"], stdin_text="{not-json")
            self.assertEqual(bad_stdin_proc.returncode, 0)
            self.assertEqual(json.loads(bad_stdin_proc.stdout), {})

            (repo / ".podo" / "checks.json").write_text("{corrupt", encoding="utf-8")
            bad_checks = GUARD.run_codified_checks(repo)
            self.assertEqual(len(bad_checks), 1)
            self.assertIn("[STRUCTURAL-CHECK]", bad_checks[0])

    def test_codified_checks_respect_deadline_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".podo").mkdir()
            checks = {
                "checks": [
                    {
                        "id": "cmd-1",
                        "type": "required_command",
                        "command": f"{sys.executable} -c 'import time; time.sleep(0.05)'",
                        "fix": "optimize",
                    },
                    {
                        "id": "cmd-2",
                        "type": "required_command",
                        "command": f"{sys.executable} -c 'import time; time.sleep(0.05)'",
                        "fix": "optimize",
                    },
                ]
            }
            (repo / ".podo" / "checks.json").write_text(json.dumps(checks), encoding="utf-8")
            violations = GUARD.run_codified_checks(repo, cmd_timeout=5, deadline=time.monotonic() + 0.05)
            self.assertTrue(any("deadline budget" in v for v in violations), violations)


class ConfigAndDagTests(unittest.TestCase):
    """Verify modular config commands, offline role-based DAG rendering, and batch node updates."""

    def test_modular_config_reset_preset_alias_and_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "podo-config.json"
            fallback = GUARD.load_config(cfg_path)
            self.assertEqual(fallback["default_roles"]["builder"], "gemini-pro-orchestrator-neon")
            self.assertEqual(fallback["roles"], fallback["default_roles"])
            self.assertEqual(fallback["gates"]["stop_hook_mode"], "enforce")
            self.assertTrue(fallback["gates"]["dead_code_check"])
            self.assertNotIn("presets", fallback)

            set_proc = run_cli(
                [
                    "--config",
                    str(cfg_path),
                    "config",
                    "--set",
                    "builder=custom-builder-model",
                    "--mode",
                    "observe",
                    "--show",
                ]
            )
            self.assertEqual(set_proc.returncode, 0, set_proc.stderr)
            updated = json.loads(set_proc.stdout)
            self.assertEqual(updated["roles"]["builder"], "custom-builder-model")
            self.assertEqual(updated["gates"]["stop_hook_mode"], "observe")

            reset_proc = run_cli(["--config", str(cfg_path), "config", "--reset", "--show"])
            self.assertEqual(reset_proc.returncode, 0, reset_proc.stderr)
            restored = json.loads(reset_proc.stdout)
            self.assertEqual(restored["roles"]["builder"], "gemini-pro-orchestrator-neon")

            run_cli(["--config", str(cfg_path), "config", "--set", "scout=alt-scout"])
            alias_proc = run_cli(["--config", str(cfg_path), "config", "--preset", "podo-default", "--show"])
            self.assertEqual(alias_proc.returncode, 0, alias_proc.stderr)
            aliased = json.loads(alias_proc.stdout)
            self.assertEqual(aliased["roles"]["scout"], "gemini-4-hydrogen-medium")

            bad_preset = run_cli(["--config", str(cfg_path), "config", "--preset", "nonexistent"])
            self.assertEqual(bad_preset.returncode, 2)

    def test_offline_dag_html_role_based_and_batch_set_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg_path = root / "podo-config.json"
            cfg = GUARD.default_config()
            cfg["roles"]["interrogator"] = "custom-interrogator-v9"
            GUARD.save_config(cfg, cfg_path)

            phases_json = json.dumps(
                {
                    "phases": [
                        {
                            "id": "phase-1",
                            "name": "Phase 1: Core",
                            "nodes": [
                                {"id": "t1", "title": "Engine", "worker": "builder-a", "model": "model-a"},
                                {"id": "t2", "title": "Tests", "worker": "builder-b", "model": "model-b"},
                            ],
                        }
                    ]
                }
            )
            state_path = root / "dag.json"
            md_path = root / "dag.md"
            html_path = root / "dag.html"

            init_proc = run_cli(
                [
                    "--config",
                    str(cfg_path),
                    "dag",
                    "init",
                    "--state-file",
                    str(state_path),
                    "--title",
                    "Guard Upgrade",
                    "--md-out",
                    str(md_path),
                    "--html-out",
                    str(html_path),
                    "--phases-json",
                    phases_json,
                ]
            )
            self.assertEqual(init_proc.returncode, 0, init_proc.stderr)

            upd_proc = run_cli(
                [
                    "--config",
                    str(cfg_path),
                    "dag",
                    "update",
                    "--state-file",
                    str(state_path),
                    "--phase",
                    "phase-1",
                    "--gate-status",
                    "verified",
                    "--set-nodes",
                    "t1=verified,t2=verified",
                ]
            )
            self.assertEqual(upd_proc.returncode, 0, upd_proc.stderr)

            state = json.loads(state_path.read_text(encoding="utf-8"))
            statuses = {n["id"]: n["status"] for n in state["phases"][0]["nodes"]}
            self.assertEqual(statuses, {"t1": "verified", "t2": "verified"})
            self.assertEqual(state["phases"][0]["gate_status"], "verified")

            md_text = md_path.read_text(encoding="utf-8")
            html_text = html_path.read_text(encoding="utf-8")

            self.assertIn("Interrogator Gate", md_text)
            self.assertIn("custom-interrogator-v9", md_text)
            self.assertNotIn("claude-opus-5-5-medium", md_text)
            self.assertNotIn("Opus", md_text)

            self.assertIn("Interrogator Gate", html_text)
            self.assertIn("custom-interrogator-v9", html_text)
            self.assertIn("<style>", html_text)
            self.assertNotIn("<script", html_text)
            self.assertNotIn("https://", html_text)
            self.assertNotIn("claude-opus-5-5-medium", html_text)
            self.assertNotIn("Opus", html_text)

            for token in (
                "--background",
                "--card",
                "--foreground",
                "--muted-foreground",
                "--border",
                "--card-border",
                "--primary",
                "--secondary",
                "--secondary-foreground",
                "--success",
                "--warning",
                "--error",
            ):
                self.assertIn(token, html_text)

            bad_upd = run_cli(
                [
                    "--config",
                    str(cfg_path),
                    "dag",
                    "update",
                    "--state-file",
                    str(state_path),
                    "--set-nodes",
                    "missing-node=verified",
                ]
            )
            self.assertEqual(bad_upd.returncode, 2)

    def test_cli_parser_has_no_removed_post_tool_hook(self) -> None:
        parser = GUARD.build_parser()
        buf_out = io.StringIO()
        buf_err = io.StringIO()
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            ns = parser.parse_args([])
        self.assertEqual(set(vars(ns).keys()), {"config", "hook_stop", "command"})


class ObservabilityTests(unittest.TestCase):
    """Verify all 5 v1.2 observability pillars: telemetry, Gantt DAG, snapshots, anomalies, and trace joining."""

    def test_pillar1_jsonl_telemetry_emitter_and_clean_repo_invariant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_git_repo(repo)
            (repo / "ok.py").write_text("def ok() -> int:\n    return 1\n", encoding="utf-8")
            run_git(repo, "add", "ok.py")
            run_git(repo, "commit", "-m", "base")

            plain_check = run_cli(["check", "--repo", str(repo)])
            self.assertEqual(plain_check.returncode, 0, plain_check.stdout + plain_check.stderr)
            self.assertFalse((repo / ".podo").exists())

            tel_file = root / "custom" / "events.jsonl"
            (repo / "bad.py").write_text("def bad() -> int:\n    # inline narration\n    return 2\n", encoding="utf-8")
            fail_check = run_cli(
                [
                    "check",
                    "--repo",
                    str(repo),
                    "--telemetry-file",
                    str(tel_file),
                    "--phase",
                    "phase-1",
                    "--worker",
                    "builder-guard",
                    "--role",
                    "builder",
                ],
                env_overrides={"PODO_CONVERSATION_ID": "conv-telemetry-1"},
            )
            self.assertEqual(fail_check.returncode, 1)
            self.assertTrue(tel_file.is_file())
            events = GUARD.read_telemetry_events(tel_file)
            self.assertEqual(len(events), 1)
            ev = events[0]
            for field in (
                "ts",
                "iso_ts",
                "event",
                "conversation_id",
                "phase",
                "worker",
                "role",
                "model",
                "gate",
                "result",
                "violation_count",
                "files",
                "duration_ms",
            ):
                self.assertIn(field, ev)
            self.assertEqual(ev["event"], "check")
            self.assertEqual(ev["conversation_id"], "conv-telemetry-1")
            self.assertEqual(ev["phase"], "phase-1")
            self.assertEqual(ev["worker"], "builder-guard")
            self.assertEqual(ev["role"], "builder")
            self.assertEqual(ev["model"], "gemini-pro-orchestrator-neon")
            self.assertEqual(ev["gate"], "no_comments")
            self.assertEqual(ev["result"], "fail")
            self.assertEqual(ev["violation_count"], 1)
            self.assertIn("bad.py", ev["files"])

            (repo / "bad.py").write_text("def bad() -> int:\n    return 2\n", encoding="utf-8")
            env_check = run_cli(
                ["check", "--repo", str(repo)],
                env_overrides={
                    "PODO_TELEMETRY_FILE": str(tel_file),
                    "PODO_PHASE": "phase-1",
                    "PODO_WORKER": "builder-guard",
                    "PODO_ROLE": "builder",
                },
            )
            self.assertEqual(env_check.returncode, 0, env_check.stdout + env_check.stderr)
            events = GUARD.read_telemetry_events(tel_file)
            self.assertEqual(len(events), 2)
            self.assertEqual(events[1]["result"], "pass")

            codify_proc = run_cli(
                [
                    "codify",
                    "--repo",
                    str(repo),
                    "--id",
                    "no-print",
                    "--regex",
                    "FORBIDDEN_TOKEN",
                    "--fix",
                    "Remove forbidden token",
                ]
            )
            self.assertEqual(codify_proc.returncode, 0, codify_proc.stderr)
            podo_tel = repo / ".podo" / "telemetry.jsonl"
            self.assertTrue(podo_tel.is_file())
            podo_events = GUARD.read_telemetry_events(podo_tel)
            self.assertTrue(any(e["event"] == "codify" for e in podo_events))

            disabled_cfg = GUARD.default_config()
            disabled_cfg["telemetry"]["enabled"] = False
            disabled_cfg_path = root / "disabled-config.json"
            GUARD.save_config(disabled_cfg, disabled_cfg_path)
            disabled_tel = root / "should-not-exist.jsonl"
            run_cli(
                [
                    "--config",
                    str(disabled_cfg_path),
                    "check",
                    "--repo",
                    str(repo),
                    "--telemetry-file",
                    str(disabled_tel),
                ]
            )
            self.assertFalse(disabled_tel.exists())

    def test_pillar2_dag_timestamps_interrogator_calls_and_offline_gantt_html(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg_path = root / "podo-config.json"
            GUARD.save_config(GUARD.default_config(), cfg_path)
            state_path = root / "podo-dag.state.json"
            md_path = root / "podo-dag.md"
            html_path = root / "podo-dag.html"

            phases_json = json.dumps(
                {
                    "phases": [
                        {
                            "id": "phase-1",
                            "name": "Phase 1: Build",
                            "nodes": [
                                {"id": "t1", "title": "Core Engine", "worker": "builder-a", "model": "model-a"},
                                {"id": "t2", "title": "Unit Tests", "worker": "builder-b", "model": "model-b"},
                            ],
                        }
                    ]
                }
            )
            init_proc = run_cli(
                [
                    "--config",
                    str(cfg_path),
                    "dag",
                    "init",
                    "--state-file",
                    str(state_path),
                    "--title",
                    "Observability Run",
                    "--md-out",
                    str(md_path),
                    "--html-out",
                    str(html_path),
                    "--phases-json",
                    phases_json,
                ]
            )
            self.assertEqual(init_proc.returncode, 0, init_proc.stderr)
            self.assertTrue((root / "podo-telemetry.jsonl").is_file())

            run_cli(
                [
                    "--config",
                    str(cfg_path),
                    "dag",
                    "update",
                    "--state-file",
                    str(state_path),
                    "--node",
                    "t1",
                    "--status",
                    "building",
                    "--started-at",
                    "2026-11-20T05:00:00Z",
                ]
            )
            run_cli(
                [
                    "--config",
                    str(cfg_path),
                    "dag",
                    "update",
                    "--state-file",
                    str(state_path),
                    "--node",
                    "t1",
                    "--status",
                    "verified",
                    "--finished-at",
                    "2026-11-20T05:00:12.4Z",
                ]
            )
            run_cli(
                [
                    "--config",
                    str(cfg_path),
                    "dag",
                    "update",
                    "--state-file",
                    str(state_path),
                    "--node",
                    "t2",
                    "--status",
                    "verified",
                    "--started-at",
                    "2026-11-20T05:00:05Z",
                    "--finished-at",
                    "2026-11-20T05:00:18Z",
                ]
            )
            run_cli(
                [
                    "--config",
                    str(cfg_path),
                    "dag",
                    "update",
                    "--state-file",
                    str(state_path),
                    "--phase",
                    "phase-1",
                    "--gate-status",
                    "phase_interrogating",
                ]
            )
            run_cli(
                [
                    "--config",
                    str(cfg_path),
                    "dag",
                    "update",
                    "--state-file",
                    str(state_path),
                    "--phase",
                    "phase-1",
                    "--gate-status",
                    "verified",
                    "--finished-at",
                    "2026-11-20T05:00:25Z",
                ]
            )

            state = json.loads(state_path.read_text(encoding="utf-8"))
            ph = state["phases"][0]
            self.assertEqual(ph["started_at"], "2026-11-20T05:00:00Z")
            self.assertEqual(ph["finished_at"], "2026-11-20T05:00:25Z")
            self.assertEqual(ph["duration_s"], 25.0)
            self.assertEqual(ph["interrogator_calls"], 2)

            nodes = {n["id"]: n for n in ph["nodes"]}
            self.assertEqual(nodes["t1"]["duration_s"], 12.4)
            self.assertEqual(nodes["t2"]["duration_s"], 13.0)

            html_text = html_path.read_text(encoding="utf-8")
            md_text = md_path.read_text(encoding="utf-8")

            self.assertIn("Phase &amp; Worker Timeline (Gantt View)", html_text)
            self.assertIn("Observability &amp; Token Estimate", html_text)
            self.assertIn("gantt-bar", html_text)
            self.assertIn("left:", html_text)
            self.assertIn("width:", html_text)
            self.assertIn("12.4s", html_text)
            self.assertIn("25.0s", html_text)
            self.assertIn("HIGH_INTERROGATOR_CHURN", html_text)
            self.assertNotIn("<script", html_text)
            self.assertNotIn("https://", html_text)

            self.assertIn("## Phase & Worker Timeline (Gantt View)", md_text)
            self.assertIn("## Observability & Token Estimate", md_text)
            self.assertIn("12.4s", md_text)
            self.assertIn("HIGH_INTERROGATOR_CHURN", md_text)

    def test_pillar3_stop_hook_and_cli_session_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg_path = root / "podo-config.json"
            GUARD.save_config(GUARD.default_config(), cfg_path)

            repo_artifact_only = root / "repo_art"
            repo_artifact_only.mkdir()
            init_git_repo(repo_artifact_only)
            (repo_artifact_only / "app.py").write_text("def ok() -> int:\n    return 1\n", encoding="utf-8")
            run_git(repo_artifact_only, "add", "app.py")
            run_git(repo_artifact_only, "commit", "-m", "init")
            artifact_dir = root / "artifacts"
            artifact_dir.mkdir()

            payload_art = json.dumps(
                {
                    "terminationReason": "NO_TOOL_CALL",
                    "cwd": str(repo_artifact_only),
                    "conversationId": "conv-art-1",
                    "artifactDirectoryPath": str(artifact_dir),
                }
            )
            hook_art = run_cli(["--config", str(cfg_path), "--hook-stop"], stdin_text=payload_art)
            self.assertEqual(hook_art.returncode, 0)
            self.assertEqual(json.loads(hook_art.stdout), {})
            self.assertFalse((repo_artifact_only / ".podo").exists())
            art_snap_path = artifact_dir / "podo-session-snapshot.json"
            self.assertTrue(art_snap_path.is_file())
            art_snap = json.loads(art_snap_path.read_text(encoding="utf-8"))
            self.assertEqual(art_snap["conversation_id"], "conv-art-1")
            self.assertIn("dag_summary", art_snap)
            self.assertIn("gate_metrics", art_snap)
            self.assertIn("token_estimates", art_snap)
            self.assertIn("trace_link", art_snap)

            repo_podo = root / "repo_podo"
            repo_podo.mkdir()
            init_git_repo(repo_podo)
            (repo_podo / ".podo").mkdir()
            (repo_podo / "ok.py").write_text("def ok() -> int:\n    return 1\n", encoding="utf-8")
            run_git(repo_podo, "add", "ok.py")
            run_git(repo_podo, "commit", "-m", "init")
            (repo_podo / "ok.py").write_text("def ok() -> int:\n    return 2\n", encoding="utf-8")

            payload_podo = json.dumps(
                {
                    "terminationReason": "NO_TOOL_CALL",
                    "cwd": str(repo_podo),
                    "conversationId": "conv-podo-9",
                }
            )
            hook_podo = run_cli(["--config", str(cfg_path), "--hook-stop"], stdin_text=payload_podo)
            self.assertEqual(hook_podo.returncode, 0)
            self.assertEqual(json.loads(hook_podo.stdout), {})

            podo_snap_path = repo_podo / ".podo" / "snapshots" / "conv-podo-9.json"
            self.assertTrue(podo_snap_path.is_file())
            podo_snap = json.loads(podo_snap_path.read_text(encoding="utf-8"))
            self.assertEqual(podo_snap["gate_metrics"]["total"], 1)
            self.assertEqual(podo_snap["gate_metrics"]["pass"], 1)

            rep_json = run_cli(["--config", str(cfg_path), "telemetry", "report", "--repo", str(repo_podo), "--json"])
            self.assertEqual(rep_json.returncode, 0, rep_json.stderr)
            parsed_rep = json.loads(rep_json.stdout)
            self.assertEqual(parsed_rep["gate_metrics"]["pass"], 1)

            rep_txt = run_cli(["--config", str(cfg_path), "telemetry", "report", "--repo", str(repo_podo)])
            self.assertEqual(rep_txt.returncode, 0, rep_txt.stderr)
            self.assertIn("Podo Telemetry Report", rep_txt.stdout)
            self.assertIn("Gates: total=1 pass=1", rep_txt.stdout)

    def test_pillar4_role_token_estimates_and_all_three_anomalies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            snaps_dir = repo / ".podo" / "snapshots"
            snaps_dir.mkdir(parents=True)
            cfg = GUARD.default_config()

            for idx in (1, 2):
                prior_snap = {
                    "conversation_id": f"prior-{idx}",
                    "dag_summary": {"total_interrogator_calls": 1},
                    "token_estimates": {"by_role": {"interrogator": {"calls": 1, "avg_tokens": 25000, "estimated_tokens": 25000}}},
                }
                (snaps_dir / f"prior-{idx}.json").write_text(json.dumps(prior_snap), encoding="utf-8")

            state = {
                "title": "Anomaly Test",
                "updated_at": "2026-11-20T05:00:00Z",
                "phases": [
                    {
                        "id": "phase-1",
                        "name": "Phase 1",
                        "gate_status": "verified",
                        "interrogator_calls": 3,
                        "nodes": [
                            {"id": "t1", "title": "A", "worker": "builder-1", "status": "verified"},
                            {"id": "t2", "title": "B", "worker": "scout-1", "status": "verified"},
                        ],
                    }
                ],
            }
            events = [
                {"event": "check", "phase": "phase-1", "worker": "builder-1", "role": "builder", "gate": "no_comments", "result": "fail"},
                {"event": "check", "phase": "phase-1", "worker": "builder-1", "role": "builder", "gate": "all", "result": "pass"},
            ]

            snap = GUARD.compute_observability_snapshot(
                cfg=cfg,
                state=state,
                events=events,
                repo=repo,
                conversation_id="current-conv",
            )
            tok = snap["token_estimates"]
            self.assertEqual(tok["by_role"]["interrogator"]["calls"], 3)
            self.assertEqual(tok["by_role"]["interrogator"]["estimated_tokens"], 75000)
            self.assertEqual(tok["by_role"]["scout"]["calls"], 1)
            self.assertEqual(tok["by_role"]["scout"]["estimated_tokens"], 12000)
            self.assertEqual(tok["by_role"]["builder"]["calls"], 2)
            self.assertEqual(tok["by_role"]["builder"]["estimated_tokens"], 56000)
            self.assertEqual(tok["total_estimated_tokens"], 143000)
            self.assertEqual(tok["tier1_saved_tokens"], 50000)

            codes = {a["code"] for a in tok["anomalies"]}
            self.assertIn("HIGH_INTERROGATOR_CHURN", codes)
            self.assertIn("HIGH_GATE_FAIL_RATE", codes)
            self.assertIn("INTERROGATOR_SPIKE_VS_HISTORY", codes)
            self.assertEqual(snap["gate_metrics"]["failure_rate"], 0.5)
            self.assertIn("no_comments", snap["gate_metrics"]["by_gate"])
            self.assertIn("phase-1", snap["gate_metrics"]["by_phase"])
            self.assertIn("builder-1", snap["gate_metrics"]["by_worker"])

    def test_pillar5_transcript_trace_joining_and_trace_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            (repo / ".podo").mkdir(parents=True)
            cfg_path = root / "podo-config.json"
            GUARD.save_config(GUARD.default_config(), cfg_path)

            transcript = root / "transcript.jsonl"
            transcript.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "USER_INPUT", "content": "implement feature"}),
                        json.dumps(
                            {
                                "type": "PLANNER_RESPONSE",
                                "tool_calls": [
                                    {"name": "read_file", "args": {"Path": json.dumps(str(repo / "a.py"))}},
                                    {"name": "edit_file", "args": {"Path": json.dumps(str(repo / "a.py"))}},
                                ],
                            }
                        ),
                        "{malformed-line",
                        json.dumps({"type": "USER_INPUT", "content": "run tests"}),
                        json.dumps(
                            {
                                "type": "PLANNER_RESPONSE",
                                "tool_calls": [
                                    {"name": "run_command", "args": {"CommandLine": "pytest"}},
                                    {"name": "read_file", "args": {"Path": str(repo / "b.py")}},
                                ],
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            artifact_dir = root / "art"
            artifact_dir.mkdir()
            state_path = artifact_dir / "podo-dag.state.json"
            state_path.write_text(json.dumps({"title": "Trace Run", "updated_at": "2026-11-20T05:00:00Z", "phases": []}), encoding="utf-8")
            tel_path = repo / ".podo" / "telemetry.jsonl"
            tel_path.write_text(
                json.dumps({"event": "check", "gate": "all", "result": "pass", "phase": "phase-1", "worker": "b1", "role": "builder"}) + "\n",
                encoding="utf-8",
            )

            snap_proc = run_cli(
                [
                    "--config",
                    str(cfg_path),
                    "telemetry",
                    "snapshot",
                    "--repo",
                    str(repo),
                    "--state-file",
                    str(state_path),
                    "--telemetry-file",
                    str(tel_path),
                    "--conversation-id",
                    "conv-trace-77",
                    "--transcript-path",
                    str(transcript),
                    "--artifact-dir",
                    str(artifact_dir),
                ]
            )
            self.assertEqual(snap_proc.returncode, 0, snap_proc.stderr)
            snap = json.loads(snap_proc.stdout)
            trace = snap["trace_link"]
            self.assertEqual(trace["conversation_id"], "conv-trace-77")
            self.assertEqual(trace["transcript_steps_count"], 4)
            self.assertEqual(trace["user_turns_count"], 2)
            self.assertEqual(trace["tool_call_counts"], {"edit_file": 1, "read_file": 2, "run_command": 1})
            self.assertEqual(trace["telemetry_file"], str(tel_path.resolve()))
            self.assertEqual(trace["dag_state_file"], str(state_path.resolve()))
            self.assertEqual(trace["artifact_dir"], str(artifact_dir.resolve()))

            trace_index_path = repo / ".podo" / "trace-index.json"
            self.assertTrue(trace_index_path.is_file())
            index_data = json.loads(trace_index_path.read_text(encoding="utf-8"))
            self.assertIn("conv-trace-77", index_data["sessions"])
            self.assertEqual(index_data["sessions"]["conv-trace-77"]["user_turns_count"], 2)

            repo_no_podo = root / "clean_repo"
            repo_no_podo.mkdir()
            out_snap = root / "exported" / "snap.json"
            out_proc = run_cli(
                [
                    "--config",
                    str(cfg_path),
                    "telemetry",
                    "snapshot",
                    "--repo",
                    str(repo_no_podo),
                    "--conversation-id",
                    "conv-out-1",
                    "--transcript-path",
                    str(transcript),
                    "--out",
                    str(out_snap),
                ]
            )
            self.assertEqual(out_proc.returncode, 0, out_proc.stderr)
            self.assertTrue(out_snap.is_file())
            self.assertFalse((repo_no_podo / ".podo").exists())
            self.assertTrue((out_snap.parent / "trace-index.json").is_file())

    def test_merged_telemetry_across_dag_and_repo_podo_and_node_role_preservation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            init_git_repo(repo)
            (repo / "app.py").write_text("def run() -> int:\n    return 1\n", encoding="utf-8")
            run_git(repo, "add", "app.py")
            run_git(repo, "commit", "-m", "base")

            cfg_path = root / "podo-config.json"
            GUARD.save_config(GUARD.default_config(), cfg_path)
            state_path = repo / ".podo" / "podo-dag.state.json"
            html_path = repo / ".podo" / "podo-dag.html"

            phases_json = json.dumps(
                [
                    {
                        "id": "p1",
                        "title": "Phase 1 Title Fallback",
                        "nodes": [
                            {"id": "n1", "title": "Scout Task", "worker": "worker-x", "role": "scout"},
                        ],
                    }
                ]
            )
            init_proc = run_cli(
                [
                    "--config",
                    str(cfg_path),
                    "dag",
                    "init",
                    "--state-file",
                    str(state_path),
                    "--html-out",
                    str(html_path),
                    "--title",
                    "Merged Telemetry Run",
                    "--phases-json",
                    phases_json,
                ]
            )
            self.assertEqual(init_proc.returncode, 0, init_proc.stderr)

            saved_state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved_state["phases"][0]["name"], "Phase 1 Title Fallback")
            self.assertEqual(saved_state["phases"][0]["nodes"][0]["role"], "scout")

            check_proc = run_cli(
                [
                    "--config",
                    str(cfg_path),
                    "check",
                    "--repo",
                    str(repo),
                    "--phase",
                    "p1",
                    "--worker",
                    "w1",
                    "--role",
                    "builder",
                ]
            )
            self.assertEqual(check_proc.returncode, 0, check_proc.stdout + check_proc.stderr)

            upd_proc = run_cli(
                [
                    "--config",
                    str(cfg_path),
                    "dag",
                    "update",
                    "--state-file",
                    str(state_path),
                    "--node",
                    "n1",
                    "--status",
                    "verified",
                ]
            )
            self.assertEqual(upd_proc.returncode, 0, upd_proc.stderr)

            rep_proc = run_cli(
                [
                    "--config",
                    str(cfg_path),
                    "telemetry",
                    "report",
                    "--repo",
                    str(repo),
                    "--state-file",
                    str(state_path),
                    "--json",
                ]
            )
            self.assertEqual(rep_proc.returncode, 0, rep_proc.stderr)
            rep_data = json.loads(rep_proc.stdout)
            self.assertGreaterEqual(rep_data["gate_metrics"]["total"], 1)
            self.assertEqual(rep_data["gate_metrics"]["pass"], 1)
            self.assertIn("scout", rep_data["token_estimates"]["by_role"])

            snap_proc = run_cli(
                [
                    "--config",
                    str(cfg_path),
                    "telemetry",
                    "snapshot",
                    "--repo",
                    str(repo),
                    "--state-file",
                    str(state_path),
                    "--conversation-id",
                    "c1",
                ]
            )
            self.assertEqual(snap_proc.returncode, 0, snap_proc.stderr)
            snap_data = json.loads(snap_proc.stdout)
            self.assertGreaterEqual(snap_data["gate_metrics"]["total"], 1)
            self.assertEqual(snap_data["gate_metrics"]["pass"], 1)

            html_text = html_path.read_text(encoding="utf-8")
            self.assertIn("1 (1 pass / 0 fail)", html_text)
            self.assertIn("scout", html_text)

    def test_format_duration_and_batch_update_backfills_created_at_with_stacked_gantt(self) -> None:
        self.assertEqual(GUARD.format_duration_s(12.5), "12.5s")
        self.assertEqual(GUARD.format_duration_s(125.0), "2m 05s")
        self.assertEqual(GUARD.format_duration_s(3900.0), "1h 05m")
        self.assertEqual(
            GUARD.format_compact_time_range("2026-11-20T05:45:01Z", "2026-11-20T06:03:36Z"),
            "05:45:01 → 06:03:36 UTC",
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg_path = root / "podo-config.json"
            GUARD.save_config(GUARD.default_config(), cfg_path)
            state_path = root / "podo-dag.state.json"
            html_path = root / "podo-dag.html"

            state = {
                "title": "Backfill Test",
                "created_at": "2026-11-20T05:00:00Z",
                "updated_at": "2026-11-20T05:00:00Z",
                "html_out": str(html_path),
                "phases": [
                    {
                        "id": "phase-1",
                        "name": "Phase 1: Backfill",
                        "gate_status": "pending",
                        "started_at": None,
                        "finished_at": None,
                        "duration_s": None,
                        "interrogator_calls": 0,
                        "nodes": [
                            {
                                "id": "t1",
                                "title": "Pending Task",
                                "worker": "builder-1",
                                "role": "builder",
                                "status": "pending",
                                "started_at": None,
                                "finished_at": None,
                                "duration_s": None,
                            }
                        ],
                    }
                ],
            }
            state_path.write_text(json.dumps(state), encoding="utf-8")

            upd_proc = run_cli(
                [
                    "--config",
                    str(cfg_path),
                    "dag",
                    "update",
                    "--state-file",
                    str(state_path),
                    "--phase",
                    "phase-1",
                    "--gate-status",
                    "verified",
                    "--set-nodes",
                    "t1=verified",
                    "--finished-at",
                    "2026-11-20T05:02:05Z",
                ]
            )
            self.assertEqual(upd_proc.returncode, 0, upd_proc.stderr)

            updated = json.loads(state_path.read_text(encoding="utf-8"))
            ph = updated["phases"][0]
            node = ph["nodes"][0]
            self.assertEqual(node["started_at"], "2026-11-20T05:00:00Z")
            self.assertEqual(node["finished_at"], "2026-11-20T05:02:05Z")
            self.assertEqual(node["duration_s"], 125.0)
            self.assertEqual(ph["started_at"], "2026-11-20T05:00:00Z")
            self.assertEqual(ph["duration_s"], 125.0)

            html_text = html_path.read_text(encoding="utf-8")
            self.assertIn("gantt-item", html_text)
            self.assertIn("gantt-head", html_text)
            self.assertIn("gantt-sub", html_text)
            self.assertIn("gantt-track", html_text)
            self.assertIn("obs-stack", html_text)
            self.assertIn("share-bar", html_text)
            self.assertIn("2m 05s", html_text)
            self.assertIn("width: 100.0%", html_text)
            self.assertNotIn("(@builder-1)", html_text)
            self.assertNotIn("https://", html_text)


if __name__ == "__main__":
    unittest.main()
