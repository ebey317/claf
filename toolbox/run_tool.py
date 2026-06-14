#!/usr/bin/env python3
"""Generic runner for minted toolbox tools."""
import importlib.util
import json
import sys
from pathlib import Path


TOOLS_DIR = Path(__file__).resolve().parent


def load_tool(name: str):
    module_path = TOOLS_DIR / f"{name}.py"
    if not module_path.exists():
        raise FileNotFoundError(f"Minted tool not found: {name}")
    spec = importlib.util.spec_from_file_location(f"toolbox_{name}", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: run_tool.py <tool_name> [json-args]", file=sys.stderr)
        return 1

    name = sys.argv[1]
    args = {}
    if len(sys.argv) > 2:
        try:
            args = json.loads(sys.argv[2])
        except Exception as e:
            print(f"[tool error] Invalid JSON args: {e}", file=sys.stderr)
            return 1

    try:
        module = load_tool(name)
        output = module.run(args)
        print(output)
        return 0
    except Exception as e:
        print(f"[tool error] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
