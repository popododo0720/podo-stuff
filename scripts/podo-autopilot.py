#!/usr/bin/env python3
"""Lightweight PreInvocation hook entrypoint for podo-stuff Autopilot Router."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from podo.autopilot import DEFAULT_CONFIG_PATH, handle_hook_pre_invocation


def main() -> int:
    """Run the PreInvocation autopilot router hook."""
    parser = argparse.ArgumentParser(prog="podo-autopilot.py")
    parser.add_argument("--config", help="Optional path to podo-config.json")
    args = parser.parse_args()
    config_path = Path(args.config).resolve() if args.config else DEFAULT_CONFIG_PATH
    return handle_hook_pre_invocation(config_path=config_path)


if __name__ == "__main__":
    sys.exit(main())
