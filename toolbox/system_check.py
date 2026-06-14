#!/usr/bin/env python3
"""Minted tool: system_check.

Inspect local machine state (USB devices, disks, mounts, processes, failed services). No network access.
"""
import subprocess
import sys


ALLOWED_TARGETS = {
    "usb": [
        ["bash", "-c", "ls -la /dev/disk/by-id /dev/disk/by-path /dev/input/js* 2>/dev/null || echo 'no matching block/input devices'"],
        ["lsusb"],
    ],
    "disks": [
        ["lsblk", "-f"],
        ["df", "-h"],
    ],
    "mounts": [
        ["findmnt"],
    ],
    "processes": [
        ["ps", "aux"],
    ],
    "services": [
        ["systemctl", "--user", "list-units", "--state=failed", "--no-pager"],
        ["systemctl", "list-units", "--state=failed", "--no-pager"],
    ],
}


def _run(cmd: list[str]) -> str:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return f"[timeout] {' '.join(cmd)}"
    except FileNotFoundError as e:
        return f"[not found] {' '.join(cmd)}: {e}"
    except Exception as e:
        return f"[error] {' '.join(cmd)}: {e}"

    out = result.stdout.strip()
    err = result.stderr.strip()
    if result.returncode != 0:
        return f"[exit {result.returncode}] {' '.join(cmd)}\n{err or out}".strip()
    return out or "(no output)"


def run(args: dict | None = None) -> str:
    args = args or {}
    target = (args.get("target") or "").strip().lower()
    if not target:
        return f"[tool error] target is required. Allowed: {', '.join(ALLOWED_TARGETS)}"
    if target not in ALLOWED_TARGETS:
        return f"[tool error] unknown target '{target}'. Allowed: {', '.join(ALLOWED_TARGETS)}"

    outputs = []
    for cmd in ALLOWED_TARGETS[target]:
        outputs.append(f"$ {' '.join(cmd)}\n{_run(cmd)}")

    return "\n\n".join(outputs)


if __name__ == "__main__":
    raw_args = {}
    if len(sys.argv) > 1:
        try:
            import json
            raw_args = json.loads(sys.argv[1])
        except Exception:
            raw_args = {"target": sys.argv[1]}
    print(run(raw_args))
