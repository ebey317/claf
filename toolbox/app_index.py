"""Shared app corpus for local app discovery.

Builds a unified index from:
  - the static open_app map
  - executable files found in $PATH
  - .desktop files in /usr/share/applications and ~/.local/share/applications

Used by resolve_app.py and open_app.py so they share the same "memory" of
what apps exist on the system.
"""
from __future__ import annotations

import configparser
import os
import shutil
from difflib import SequenceMatcher
from pathlib import Path


def _static_apps() -> list[dict]:
    """Return the static _APP_MAP entries as corpus items."""
    # Import open_app lazily to avoid circular imports.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "open_app", Path(__file__).resolve().parent / "open_app.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    corpus: list[dict] = []
    for name, cmd in module._APP_MAP.items():
        exe = shutil.which(cmd[0]) if cmd else None
        corpus.append({
            "name": name,
            "aliases": [name],
            "executable": exe or cmd[0] if cmd else None,
            "command": cmd,
            "source": "app_map",
        })
    return corpus


def _path_apps() -> list[dict]:
    """Return executable basenames found in $PATH."""
    seen: set[str] = set()
    corpus: list[dict] = []
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        d = Path(directory)
        if not d.is_dir():
            continue
        try:
            for entry in d.iterdir():
                if not entry.is_file():
                    continue
                try:
                    if os.access(entry, os.X_OK):
                        name = entry.name.lower()
                        if name not in seen:
                            seen.add(name)
                            corpus.append({
                                "name": entry.name,
                                "aliases": [entry.name],
                                "executable": str(entry),
                                "command": [str(entry)],
                                "source": "path",
                            })
                except (OSError, PermissionError):
                    continue
        except (OSError, PermissionError):
            continue
    return corpus


def _desktop_apps() -> list[dict]:
    """Parse .desktop files for Name, GenericName, Keywords, Exec."""
    dirs = [
        Path("/usr/share/applications"),
        Path("/usr/local/share/applications"),
        Path.home() / ".local" / "share" / "applications",
        Path("/var/lib/flatpak/exports/share/applications"),
    ]
    corpus: list[dict] = []
    seen: set[str] = set()
    for d in dirs:
        if not d.is_dir():
            continue
        for desktop in d.glob("*.desktop"):
            try:
                cp = configparser.ConfigParser(interpolation=None)
                cp.read(desktop, encoding="utf-8", errors="ignore")
                if not cp.has_section("Desktop Entry"):
                    continue
                de = cp["Desktop Entry"]
                if de.get("NoDisplay", "false").lower() == "true":
                    continue
                if de.get("Type", "Application").lower() != "application":
                    continue
                name = de.get("Name", desktop.stem)
                generic = de.get("GenericName", "")
                keywords = de.get("Keywords", "")
                exec_line = de.get("Exec", "")
                if not exec_line:
                    continue
                # Strip field codes from Exec.
                exe = exec_line.split()[0]
                exe_path = shutil.which(exe)
                key = name.lower()
                if key in seen:
                    continue
                seen.add(key)
                aliases = [name, desktop.stem]
                if generic:
                    aliases.append(generic)
                for kw in keywords.split(";"):
                    if kw.strip():
                        aliases.append(kw.strip())
                corpus.append({
                    "name": name,
                    "aliases": list(dict.fromkeys(a.lower() for a in aliases)),
                    "executable": exe_path or exe,
                    "command": [exe_path or exe],
                    "source": "desktop",
                    "desktop_file": str(desktop),
                })
            except Exception:
                continue
    return corpus


def build_corpus() -> list[dict]:
    """Build and return the full local app corpus."""
    corpus = _static_apps() + _desktop_apps() + _path_apps()
    # Deduplicate by name, preferring earlier sources.
    seen: set[str] = set()
    out: list[dict] = []
    for item in corpus:
        key = item["name"].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def fuzzy_match(query: str, corpus: list[dict], threshold: float = 0.55, top_n: int = 5) -> list[tuple[dict, float]]:
    """Return corpus items whose names/aliases fuzzy-match the query."""
    query = query.lower().strip()
    if not query:
        return []
    scored: list[tuple[dict, float]] = []
    for item in corpus:
        best = 0.0
        candidates = [item["name"]] + item.get("aliases", [])
        for cand in candidates:
            cand = cand.lower().strip()
            if not cand:
                continue
            # Exact substring match gets a strong boost.
            if query in cand or cand in query:
                score = 0.85 + 0.15 * SequenceMatcher(None, query, cand).ratio()
            else:
                score = SequenceMatcher(None, query, cand).ratio()
            if score > best:
                best = score
        if best >= threshold:
            scored.append((item, best))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_n]


if __name__ == "__main__":
    for item in build_corpus()[:20]:
        print(item["name"], "=>", item.get("executable"), f"({item['source']})")
