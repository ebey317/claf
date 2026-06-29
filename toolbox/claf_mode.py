#!/usr/bin/env python3
"""Minted tool: get, set, or cycle CLAF permission mode.

Mirrors Claude Code's Shift+Tab mode cycle:
    default → acceptEdits → plan → auto → default

Usage:
    python3 ~/projects/claf/toolbox/run_tool.py claf_mode
    python3 ~/projects/claf/toolbox/run_tool.py claf_mode '{"cycle": true}'
    python3 ~/projects/claf/toolbox/run_tool.py claf_mode '{"set": "auto"}'
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_CLAF_DIR = Path(__file__).resolve().parent.parent
if str(_CLAF_DIR) not in sys.path:
    sys.path.insert(0, str(_CLAF_DIR))

import claf_permissions as cp


def run(args: dict | None = None) -> str:
    args = args or {}

    if args.get("cycle"):
        new_mode = cp.cycle_mode()
        return f"Cycled to {new_mode}"

    if "set" in args:
        mode = str(args["set"])
        new_mode = cp.set_mode(mode)
        return f"Set mode to {new_mode}"

    if args.get("list"):
        return "Modes: " + ", ".join(sorted(cp._VALID_MODES))

    mode = cp.current_mode()
    block = cp.mode_prompt_block()
    return f"Current mode: {mode}\n{block}"


def main() -> int:
    parser = argparse.ArgumentParser(description="CLAF permission mode control")
    parser.add_argument("--cycle", action="store_true", help="Cycle to next mode")
    parser.add_argument("--set", help="Set mode explicitly")
    parser.add_argument("--list", action="store_true", help="List valid modes")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.add_argument(
        "--export", action="store_true", help="Output shell export command for the current mode"
    )
    args = parser.parse_args()

    tool_args = {}
    if args.cycle:
        tool_args["cycle"] = True
    if args.set:
        tool_args["set"] = args.set
    if args.list:
        tool_args["list"] = True

    result = run(tool_args)
    if args.export:
        print(f"export CLAF_PERMISSION_MODE={cp.persisted_mode()}")
    elif args.json:
        print(json.dumps({"result": result, "mode": cp.current_mode()}))
    else:
        print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
