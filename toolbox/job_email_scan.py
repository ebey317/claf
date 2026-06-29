#!/usr/bin/env python3
"""Scan all configured IMAP email accounts for job-related messages.

Uses the same credentials as the email-bridge MCP server
(~/.config/email_mcp/credentials.json) and returns a concise summary of recent
messages that look job-related.

The filter is config-driven, not hard-coded:
  ~/.config/claf/job_email_scan.json
If that file is missing, a generic default config is written there. Edit it to
add your own sender domains, keywords, and negative filters.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

# email_mcp lives under ~/scripts
_SCRIPTS_DIR = Path.home() / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import email_mcp.server as bridge

_CONFIG_DIR = Path.home() / ".config" / "claf"
_CONFIG_PATH = _CONFIG_DIR / "job_email_scan.json"

# Generic defaults only — no user-specific companies or phrases.
# The operator owns the config file and can tune it without touching code.
_DEFAULT_CONFIG = {
    "sender_markers": [
        "jobalert.indeed.com",
        "indeed.com",
        "ziprecruiter.com",
        "aerotek.com",
        "linkedin.com",
        "monster.com",
        "careerbuilder.com",
        "glassdoor.com",
        "simplyhired.com",
        "snagajob.com",
        "workday.com",
        "myworkday.com",
        "greenhouse.io",
        "lever.co",
        "smartrecruiters.com",
        "adp.com",
        "talentbrew.com",
    ],
    "subject_words": [
        "job",
        "jobs",
        "hiring",
        "position",
        "positions",
        "recruiter",
        "recruiting",
        "interview",
        "offer",
        "offered",
        "application",
        "applied",
        "requisition",
        "technician",
        "mechanic",
        "installer",
        "operator",
        "hvac",
        "maintenance",
        "apprenticeship",
        "internship",
        "unemployment",
        "workforce",
    ],
    "subject_phrases": [],
    "negative_subject": [
        "course",
        "coursera",
        "webinar",
        "sale",
        "discount",
        "trip",
        "travel",
        "free!",
        "gift",
        "newsletter",
    ],
    "scan_limit": 30,
}


def _load_config() -> dict:
    """Load filter config from disk, writing generic defaults if missing."""
    if _CONFIG_PATH.exists():
        try:
            with _CONFIG_PATH.open("r", encoding="utf-8") as f:
                cfg = json.load(f)
            # Merge any missing keys from defaults so updates stay backward-compatible.
            for key, value in _DEFAULT_CONFIG.items():
                cfg.setdefault(key, value)
            return cfg
        except Exception:
            # If the config is broken, fall back to defaults but don't overwrite.
            return dict(_DEFAULT_CONFIG)

    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with _CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(_DEFAULT_CONFIG, f, indent=2)
    return dict(_DEFAULT_CONFIG)


def _compile_subject_re(words: list[str]) -> re.Pattern | None:
    clean = [w for w in words if w and isinstance(w, str)]
    if not clean:
        return None
    return re.compile(
        r"\b(?:" + "|".join(re.escape(w) for w in clean) + r")\b",
        re.IGNORECASE,
    )


def _is_job_related(parsed: dict, cfg: dict, subject_re: re.Pattern | None) -> bool:
    sender = parsed["from"].lower()
    subject = parsed["subject"].lower()

    negative = [n.lower() for n in cfg.get("negative_subject", []) if isinstance(n, str)]
    if any(neg in subject or neg in sender for neg in negative):
        return False

    for marker in cfg.get("sender_markers", []):
        if isinstance(marker, str) and marker.lower() in sender:
            return True

    if subject_re is not None and subject_re.search(subject):
        return True

    for phrase in cfg.get("subject_phrases", []):
        if isinstance(phrase, str) and phrase.lower() in subject:
            return True

    return False


def _snippet(text: str, max_len: int = 180) -> str:
    text = text.replace("\r", " ").replace("\n", " ")
    while "  " in text:
        text = text.replace("  ", " ")
    if len(text) <= max_len:
        return text.strip()
    return text[:max_len].rstrip() + "..."


def _scan_account(name: str, limit: int) -> dict:
    result = {"matches": [], "error": None, "scanned": 0}
    try:
        acct = bridge.get_account(name)
        conn = bridge.imap_connect(acct)
        try:
            conn.select("INBOX", readonly=True)
            _, data = conn.search(None, "ALL")
            uids = data[0].split()
            uids = uids[-limit:][::-1]
            result["scanned"] = len(uids)
            for uid in uids:
                _, msg_data = conn.fetch(uid, "(RFC822)")
                raw = msg_data[0][1]
                parsed = bridge.parse_email(raw)
                result["matches"].append(
                    {
                        "uid": uid.decode(),
                        "date": parsed["date"],
                        "from": parsed["from"],
                        "subject": parsed["subject"],
                        "snippet": _snippet(parsed["body"]),
                        "_is_job": False,
                    }
                )
        finally:
            try:
                conn.logout()
            except Exception:
                pass
    except Exception as e:
        result["error"] = str(e)
    return result


def _format_date(date_str: str) -> str:
    if not date_str:
        return ""
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%d %b %Y %H:%M:%S %z"):
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            return dt.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            continue
    return date_str.strip()[:30]


def run(args: dict | None = None) -> str:
    args = args or {}
    cfg = _load_config()
    try:
        limit = max(1, min(int(args.get("limit", cfg.get("scan_limit", 30))), 200))
    except Exception:
        limit = 30

    creds = bridge.load_credentials()
    accounts = creds.get("accounts", {})
    if not accounts:
        return "No email accounts configured."

    wanted = args.get("accounts")
    if isinstance(wanted, str):
        wanted = None
    if wanted:
        names = [n for n in wanted if n in accounts]
    else:
        names = list(accounts.keys())

    subject_re = _compile_subject_re(cfg.get("subject_words", []))

    lines = ["Job-related emails across accounts (most recent first):\n"]
    total_matches = 0
    for name in names:
        data = _scan_account(name, limit)
        if data["error"]:
            lines.append(f"{name}: could not read ({data['error']})")
            continue

        matches = [m for m in data["matches"] if _is_job_related(m, cfg, subject_re)]
        total_matches += len(matches)
        lines.append(f"{name} — {len(matches)} job-related message(s) (scanned {data['scanned']}):")
        if not matches:
            lines.append("  (none)")
        else:
            for m in matches[:10]:
                date = _format_date(m["date"])
                sender = m["from"].replace("\n", " ")[:45]
                subject = m["subject"].replace("\n", " ")[:70]
                lines.append(f"  • {date}  {sender}")
                lines.append(f"    Subject: {subject}")
                if m["snippet"]:
                    lines.append(f"    Snippet: {m['snippet']}")
            if len(matches) > 10:
                lines.append(f"    ... and {len(matches) - 10} more")
        lines.append("")

    lines.append(f"Total job-related messages found: {total_matches}")
    return "\n".join(lines)


if __name__ == "__main__":
    _args = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    print(run(_args))
