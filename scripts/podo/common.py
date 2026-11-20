#!/usr/bin/env python3
"""Shared utilities, persistent user-config resolution, and opt-in checks.json hash trust for podo-stuff."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_CONFIG_PATH = PLUGIN_ROOT / "podo-config.json"

SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")


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


def deep_merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge override onto a copy of base without mutating inputs."""
    merged: dict[str, Any] = dict(base)
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge_dicts(merged[key], val)
        else:
            merged[key] = val
    return merged


def is_telemetry_enabled(cfg: dict[str, Any]) -> bool:
    """Return whether JSONL telemetry emission is enabled in configuration."""
    telemetry = cfg.get("telemetry", {})
    if isinstance(telemetry, dict) and "enabled" in telemetry:
        return bool(telemetry["enabled"])
    return True


def resolve_user_data_dir(home_dir: Path | None = None) -> Path:
    """Resolve persistent user data directory outside the plugin install folder."""
    plugin_data = os.environ.get("PLUGIN_DATA", "").strip()
    if plugin_data:
        return Path(plugin_data).resolve()
    base_home = home_dir if home_dir is not None else Path.home()
    return base_home / ".config" / "podo-stuff"


def resolve_user_config_path(home_dir: Path | None = None) -> Path:
    """Return the persistent user override config path that survives plugin reinstalls."""
    return resolve_user_data_dir(home_dir=home_dir) / "podo-config.json"


def resolve_trusted_checks_path(home_dir: Path | None = None) -> Path:
    """Return the JSON registry storing opt-in approved .podo/checks.json SHA-256 hashes."""
    return resolve_user_data_dir(home_dir=home_dir) / "trusted-checks.json"


def load_effective_config(
    config_path: Path = DEFAULT_CONFIG_PATH,
    *,
    explicit_path: bool = False,
    home_dir: Path | None = None,
) -> dict[str, Any]:
    """Load bundled config merged with persistent user overrides when using the default path."""
    base = load_json_dict(config_path)
    if not explicit_path and config_path.resolve() == DEFAULT_CONFIG_PATH.resolve():
        user_cfg = load_json_dict(resolve_user_config_path(home_dir=home_dir))
        if user_cfg:
            base = deep_merge_dicts(base, user_cfg)
    default_roles = base.get("default_roles")
    if isinstance(default_roles, dict):
        active_roles = base.get("roles")
        if not isinstance(active_roles, dict) or not active_roles:
            base["roles"] = dict(default_roles)
        else:
            merged_roles = dict(default_roles)
            merged_roles.update(active_roles)
            base["roles"] = merged_roles
    return base


def compute_file_sha256(path: Path) -> str:
    """Return hex SHA-256 digest for a file, or empty string if unreadable."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def is_checks_file_trusted(checks_path: Path, home_dir: Path | None = None) -> bool:
    """Return True if .podo/checks.json hash is explicitly opted-in via trust store or PODO_TRUST_CHECKS=1."""
    if os.environ.get("PODO_TRUST_CHECKS", "").strip() in {"1", "true", "yes"}:
        return True
    digest = compute_file_sha256(checks_path)
    if not digest:
        return False
    store = load_json_dict(resolve_trusted_checks_path(home_dir=home_dir))
    repos = store.get("approved", {})
    if not isinstance(repos, dict):
        return False
    repo_key = str(checks_path.resolve().parent.parent)
    return repos.get(repo_key) == digest


def trust_checks_file(checks_path: Path, home_dir: Path | None = None) -> str:
    """Record the current SHA-256 hash of .podo/checks.json in the opt-in trust store."""
    digest = compute_file_sha256(checks_path)
    if not digest:
        return ""
    trust_path = resolve_trusted_checks_path(home_dir=home_dir)
    store = load_json_dict(trust_path)
    approved = store.get("approved")
    if not isinstance(approved, dict):
        approved = {}
    repo_key = str(checks_path.resolve().parent.parent)
    approved[repo_key] = digest
    store["approved"] = approved
    store["updated_at"] = utc_now_iso()
    trust_path.parent.mkdir(parents=True, exist_ok=True)
    trust_path.write_text(json.dumps(store, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return digest
