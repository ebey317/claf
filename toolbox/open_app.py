#!/usr/bin/env python3
"""Minted tool: launch a local desktop application by name.

Known apps get their exact command from _APP_MAP. Any other name is treated
as a CLI command looked up in PATH — so "open ollama" works the same way
typing `ollama` in a terminal does.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

# Make claf_permissions importable from the toolbox/ subdirectory.
_CLAF_DIR = Path(__file__).resolve().parent.parent
if str(_CLAF_DIR) not in sys.path:
    sys.path.insert(0, str(_CLAF_DIR))
import claf_permissions

_APP_MAP = {
    # Media / entertainment
    "hypnotix": ["hypnotix"],
    "iptv": ["hypnotix"],
    "vlc": ["vlc"],
    "mpv": ["mpv"],
    "totem": ["totem"],
    "rhythmbox": ["rhythmbox"],
    "spotify": ["spotify"],
    # Communication
    "thunderbird": ["thunderbird"],
    "email": ["thunderbird"],
    "evolution": ["evolution"],
    "discord": ["discord"],
    "slack": ["slack"],
    # Files
    "files": ["nemo"],
    "nemo": ["nemo"],
    "nautilus": ["nautilus"],
    "dolphin": ["dolphin"],
    # Browsers
    "chrome": ["google-chrome"],
    "browser": ["google-chrome"],
    "firefox": ["firefox"],
    "chromium": ["chromium-browser"],
    "brave": ["brave"],
    # Terminals
    "terminal": ["x-terminal-emulator"],
    "gnome-terminal": ["gnome-terminal"],
    "console": ["kgx"],
    "kgx": ["kgx"],
    "tilix": ["tilix"],
    "konsole": ["konsole"],
    # Text editors / IDEs
    "gedit": ["gedit"],
    "text": ["gedit"],
    "gnome-text-editor": ["gnome-text-editor"],
    "mousepad": ["mousepad"],
    "geany": ["geany"],
    "code": ["code"],
    "vscode": ["code"],
    "vim": ["x-terminal-emulator", "-e", "vim"],
    "nano": ["x-terminal-emulator", "-e", "nano"],
    # Office
    "writer": ["libreoffice", "--writer"],
    "calc": ["libreoffice", "--calc"],
    "impress": ["libreoffice", "--impress"],
    "libreoffice": ["libreoffice"],
    # Settings / system
    "settings": ["cinnamon-settings"],
    "cinnamon-settings": ["cinnamon-settings"],
    "gnome-settings": ["gnome-control-center"],
    "gnome-control-center": ["gnome-control-center"],
    "system-monitor": ["gnome-system-monitor"],
    "calculator": ["gnome-calculator"],
    "screenshot": ["gnome-screenshot"],
    "image-viewer": ["eog"],
    "eog": ["eog"],
    "help": ["yelp"],
}

# Commands we should never launch, even if they are in PATH.
_BLOCKLIST = {
    "sudo",
    "su",
    "rm",
    "mv",
    "cp",
    "dd",
    "mkfs",
    "fdisk",
    "parted",
    "shutdown",
    "reboot",
    "poweroff",
    "halt",
    "init",
}


def run(args: dict | None = None) -> str:
    args = args or {}
    app = (args.get("app") or "").strip().lower()

    if not app:
        # Parse from raw command text: "open hypnotix" → "hypnotix"
        raw = (args.get("_raw_command") or "").strip().lower()
        for prefix in ("open ", "launch ", "start "):
            if raw.startswith(prefix):
                app = raw[len(prefix) :].strip().split()[0] if raw[len(prefix) :].strip() else ""
                break

    if not app:
        return "[tool error] No app name provided."
    if app in _BLOCKLIST:
        return f"[tool error] Refusing to launch blocked command: {app}"

    # Permission mode gate
    verdict = claf_permissions.is_action_allowed("launch", app)
    if verdict == "plan":
        return f"[plan] Would launch: {app}"
    if verdict == "ask":
        return f"[ask] Approve launching {app}?"
    if verdict == "deny":
        return f"[tool error] Permission mode ({claf_permissions.MODE}) denies launching {app}"

    cmd = _APP_MAP.get(app)
    if cmd is None:
        # Treat unknown names like a terminal command: look them up in PATH.
        exe = shutil.which(app)
        if not exe:
            return f"[tool error] App not found in PATH: {app}"
        cmd = [exe]

    try:
        subprocess.Popen(
            cmd, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        return f"Launched {cmd[0]}."
    except FileNotFoundError:
        return f"[tool error] App not found: {cmd[0]}"
    except Exception as e:
        return f"[tool error] {e}"


if __name__ == "__main__":
    raw_args = {}
    if len(sys.argv) > 1:
        try:
            raw_args = json.loads(sys.argv[1])
        except Exception:
            raw_args = {}
    print(run(raw_args))
