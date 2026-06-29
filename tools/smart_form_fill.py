#!/usr/bin/env python3
"""Universal form filler — x-ray any page and fill every field it can.

Works around the loaded Sensei extension bug where BROWSER_FILL ignores the
separate `text`/`value` parameter by baking the value into the target with the
`::` delimiter (e.g. `#firstName :: Alex`).

Usage:
    python3 tools/smart_form_fill.py                     # fill with defaults
    python3 tools/smart_form_fill.py --profile profile.json
    python3 tools/smart_form_fill.py --dry-run           # plan only, no fill
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import date
from pathlib import Path

try:
    from bs4 import BeautifulSoup
except Exception as e:  # pragma: no cover
    print("[SmartFormFill] BeautifulSoup4 is required: pip install beautifulsoup4", file=sys.stderr)
    raise SystemExit(1)

SENSEI_MCP = Path.home() / "projects/master-ai/sensei_mcp_server.py"

DEFAULTS = {
    "first_name": "Alex",
    "last_name": "Tester",
    "full_name": "Alex Tester",
    "email": "alex.tester@example.com",
    "phone": "555-123-4567",
    "address1": "123 Main Street",
    "address2": "Apt 4B",
    "city": "Anytown",
    "state": "California",
    "zip": "90210",
    "country": "United States",
    "company": "Example Corp",
    "job_title": "DevOps Engineer",
    "website": "https://example.com",
    "salary": "150000",
    "experience_years": "2",
    "birth_date": "1990-01-01",
    "start_date": date.today().isoformat(),
    "number": "5",
    "contact_method": "mail",
    "experience_level": "mid",
    "skills": ["Go"],
    "generic_text": "Sample value",
}

# Pattern specs: (regex, default_key). Use keys so overrides in DEFAULTS are picked up.
_TEXT_PATTERN_SPECS = [
    (r"first\s*name|given\s*name", "first_name"),
    (r"last\s*name|surname|family\s*name", "last_name"),
    (r"preferred\s*contact\s*method|contact\s*method", "contact_method"),
    (r"e[-\s]?mail", "email"),
    (r"phone|mobile|cell|telephone|tel\b", "phone"),
    (r"address\s*(line\s*)?2|apartment|suite|unit|apt\b", "address2"),
    (r"address|street", "address1"),
    (r"\bcity\b|town", "city"),
    (r"\bstate\b|province", "state"),
    (r"zip|postal", "zip"),
    (r"\bcountry\b", "country"),
    (r"company|organization|employer|business\s*name", "company"),
    (r"job\s*title|position\s*title|role\b", "job_title"),
    (r"title\b", "job_title"),
    (r"website|url\b|linkedin|portfolio|github", "website"),
    (r"salary|compensation|desired\s*pay|pay\s*rate", "salary"),
    (r"experience\s*level|level\b", "experience_level"),
    (r"years?\s*of\s*experience|experience\s*years?", "experience_years"),
    (r"dob|birth\s*date|date\s*of\s*birth|birthday", "birth_date"),
    (r"start\s*date|date\b", "start_date"),
    (r"full\s*name|your\s*name|applicant\s*name|\bname\b", "full_name"),
    (
        r"comments|summary|description|message|cover\s*letter|notes|additional",
        None,
    ),  # generated later
]


def refresh_patterns():
    """Rebuild TEXT_PATTERNS from the current DEFAULTS so profile overrides work."""
    global TEXT_PATTERNS
    TEXT_PATTERNS = []
    for pat, key in _TEXT_PATTERN_SPECS:
        val = DEFAULTS.get(key) if key else None
        TEXT_PATTERNS.append((re.compile(pat, re.I), val))


refresh_patterns()

SENSITIVE_RE = re.compile(
    r"password|passcode|ssn|social\s*security|credit\s*card|card\s*number|cvv|cvc|"
    r"routing|account\s*number|bank\s*account|secret|token|api\s*key|private\s*key",
    re.I,
)

PLACEHOLDER_OPTION_RE = re.compile(r"^\s*(select|choose|pick|\.\.\.|--|none|n\/a|please)\b", re.I)


def _is_placeholder_option(text: str, value: str) -> bool:
    """Return True for 'Select one...', '-- choose --', empty-value placeholders, etc."""
    text = text.strip()
    value = value.strip()
    if not text and not value:
        return True
    if text.startswith("--"):
        return True
    if PLACEHOLDER_OPTION_RE.match(text):
        return True
    # Empty value + short/non-descriptive text is usually a placeholder.
    if not value and len(text.split()) <= 2:
        return True
    return False


def _match_text_value(hints: str) -> str | None:
    """Return a default string value based on label/id/name hints."""
    low = hints.lower()
    for pat, val in TEXT_PATTERNS:
        if pat.search(low):
            if val is None:
                # textarea / comments: generate a sentence from the label text
                label = re.sub(r"[^a-z0-9 ]", " ", low).strip()
                return f"This is a sample {label}. Please replace it with real information."
            return val
    return None


def _source_lookup(selector: str, soup: BeautifulSoup):
    """Try to find the source element for a read_full selector."""
    if not selector:
        return None
    try:
        el = soup.select_one(selector)
        if el:
            return el
    except Exception:
        pass
    # Fallback: try to extract an id.
    m = re.search(r"#([A-Za-z0-9_\-]+)", selector)
    if m:
        return soup.find(id=m.group(1))
    # Fallback: try name attribute.
    m = re.search(r"\[name=[\"']?([^\"'\]]+)[\"']?\]", selector)
    if m:
        return soup.find(attrs={"name": m.group(1)})
    return None


def _pick_select_option(select_el, preferred: str | None = None) -> str | None:
    """Pick the best option from a <select>."""
    preferred = (preferred or "").strip().lower()
    first_real: str | None = None
    for opt in select_el.find_all("option"):
        text = opt.get_text(strip=True)
        val = opt.get("value", "")
        if _is_placeholder_option(text, val):
            continue
        payload = val if val else text
        # If we have a preferred value from defaults/profile, match by text or value.
        if preferred and (preferred == text.lower() or preferred == val.lower()):
            return payload
        if first_real is None:
            first_real = payload
    return first_real


def _should_check_checkbox(label: str) -> bool:
    """Check positive agreement/consent/eligibility checkboxes; leave opt-outs alone."""
    low = label.lower()
    if re.search(r"opt[-\s]?out|do\s*not|unsubscribe|no\s*email|don.?t\s*send|skip|decline", low):
        return False
    if re.search(
        r"agree|terms|consent|subscribe|newsletter|confirm|accept|acknowledge|"
        r"i\s*am\s*not\s*a\s*robot|certify|certification|license|licensed|authorized|"
        r"eligible|willing|available|full[-\s]?time|part[-\s]?time|remote|"
        r"background\s*check|information\s*is\s*true|job\s*alerts",
        low,
    ):
        return True
    return False


def _radio_pick_value(
    selector: str, soup: BeautifulSoup, preferred: str | None = None
) -> str | None:
    """For a radio input, return the best option in its group."""
    src = _source_lookup(selector, soup)
    if not src or src.name != "input" or src.get("type", "").lower() != "radio":
        return None
    name = src.get("name")
    if not name:
        return src.get("value") or src.get_text(strip=True)
    group = soup.find_all("input", {"type": "radio", "name": name})
    if not group:
        return None
    preferred = (preferred or "").strip().lower()
    if preferred:
        for opt in group:
            val = (opt.get("value") or "").strip().lower()
            label = opt.get_text(strip=True).lower()
            if val == preferred or label == preferred:
                return opt.get("value") or opt.get_text(strip=True) or "on"
    pick = group[0]
    return pick.get("value") or pick.get_text(strip=True) or "on"


def _fetch_page_source(url: str) -> BeautifulSoup | None:
    """Fetch the raw HTML for a page so we can read select/radio options."""
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                "Accept": "text/html",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        return BeautifulSoup(html, "html.parser")
    except Exception as e:
        print(f"[SmartFormFill] Could not fetch page source ({e}); select/radio fallback disabled.")
        return None


class SenseiMCP:
    def __init__(self):
        self.proc = subprocess.Popen(
            [sys.executable, str(SENSEI_MCP)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "SENSEI_HEADLESS": "0", "SENSEI_READ_FULL_MAX_CHARS": "100000"},
        )
        self._req = 0
        self.call(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "smart-form-fill", "version": "1.0"},
            },
            1,
        )

    def call(self, method, params=None, req_id=None):
        self._req += 1
        msg = {"jsonrpc": "2.0", "id": req_id or int(time.time() * 1000) % 100000, "method": method}
        if params:
            msg["params"] = params
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline().strip())

    def read_full(self) -> dict:
        r = self.call("tools/call", {"name": "read_full"}, self._req)
        text = r["result"]["content"][0]["text"]
        return json.loads(text)

    def _result_from_text(self, txt: str) -> dict:
        # Tool handlers truncate the JSON to ~400 chars, so we can't always parse it.
        # We just need the boolean ok and any explicit error/reason.
        ok = bool(re.search(r'"ok"\s*:\s*true', txt))
        err = ""
        for key in ("error", "reason"):
            m = re.search(rf'"{key}"\s*:\s*"([^"]*)"', txt)
            if m:
                err = m.group(1)
                break
        return {"ok": ok, "error": err, "raw": txt}

    def fill(self, selector: str, value: str) -> dict:
        target = f"{selector} :: {value}"
        r = self.call("tools/call", {"name": "fill", "arguments": {"where": target}}, self._req)
        txt = r["result"]["content"][0]["text"]
        return self._result_from_text(txt)

    def click(self, target: str) -> dict:
        r = self.call(
            "tools/call",
            {"name": "click", "arguments": {"what": target, "intercept_popup": True}},
            self._req,
        )
        txt = r["result"]["content"][0]["text"]
        return self._result_from_text(txt)

    def close(self):
        self.proc.terminate()


def discover_fields(page: dict) -> list[dict]:
    """Build a flat list of fillable fields from read_full output."""
    fields = []
    seen_selectors = set()

    for el in page.get("result", {}).get("elements", []):
        role = el.get("role", "").lower()
        if role not in ("input", "textarea", "select"):
            continue
        sel = el.get("selector")
        if not sel or sel in seen_selectors:
            continue
        seen_selectors.add(sel)
        fields.append(
            {
                "selector": sel,
                "type": "text" if role == "input" else role,
                "label": el.get("name", ""),
                "role": role,
                "value_present": bool(el.get("value", "").strip()),
            }
        )

    return fields


def _is_checked_in_source(selector: str, soup: BeautifulSoup | None) -> bool:
    if not soup:
        return False
    src = _source_lookup(selector, soup)
    return bool(src and src.get("checked"))


def _radio_group_state(selector: str, soup: BeautifulSoup | None) -> tuple[bool, str | None]:
    """Return (any_checked, first_option_value) for the radio group."""
    if not soup:
        return False, None
    src = _source_lookup(selector, soup)
    if not src or src.name != "input" or src.get("type", "").lower() != "radio":
        return False, None
    name = src.get("name")
    group = soup.find_all("input", {"type": "radio", "name": name}) if name else [src]
    if not group:
        return False, None
    any_checked = any(el.get("checked") for el in group)
    first = group[0]
    return any_checked, (first.get("value") or first.get_text(strip=True) or "on")


def decide_value(field: dict, soup: BeautifulSoup | None) -> tuple[str | None, str]:
    """Return (value_to_send, reason) for a field. value=None means skip."""
    selector = field["selector"]
    label = field.get("label", "")
    ftype = field.get("type", "text").lower()
    role = field.get("role", "").lower()

    # Look up the source element when possible so we know the real type.
    src = _source_lookup(selector, soup) if soup else None
    if src:
        ftype = src.get("type", ftype).lower()
        role = src.name if src.name else role

    hints = f"{label} {selector} {ftype}".lower()

    # Sensitive gate.
    if SENSITIVE_RE.search(hints):
        return None, "sensitive field — skipped"

    # Checkboxes: value_present is not a reliable checked signal, inspect source.
    if ftype == "checkbox" or role == "checkbox":
        if _is_checked_in_source(selector, soup):
            return None, "checkbox already checked"
        if _should_check_checkbox(label):
            # Send the input's own value attribute (e.g. "yes") to avoid conflict checks.
            cb_value = src.get("value") if src else ""
            return cb_value or "on", "checkbox checked (consent-style)"
        return None, "checkbox skipped (not an agreement/consent field)"

    # Radio buttons: prefer an option that matches the field's default value.
    if ftype == "radio" or role == "radio":
        checked, _ = _radio_group_state(selector, soup)
        if checked:
            return None, "radio group already has a selection"
        # Use the radio group name to pick the right default, since individual
        # radio labels (e.g. "Email contact") can trigger unrelated patterns.
        src = _source_lookup(selector, soup) if soup else None
        radio_name = src.get("name") if src else ""
        radio_default_map = {
            "contact": "contact_method",
            "level": "experience_level",
        }
        default_key = radio_default_map.get(radio_name)
        preferred = DEFAULTS.get(default_key) if default_key else None
        if not preferred:
            preferred = _match_text_value(hints)
        val = _radio_pick_value(selector, soup, preferred=preferred)
        if val:
            reason = (
                "radio: matched default option" if preferred else "radio: first option selected"
            )
            return val, reason
        return None, "radio: could not determine options"

    # Select menus: prefer an option that matches the field's default value,
    # otherwise fall back to the first real option.
    if role == "select" or ftype == "select" or (src and src.name == "select"):
        sel_src = (
            src
            if (src and src.name == "select")
            else (_source_lookup(selector, soup) if soup else None)
        )
        if sel_src and sel_src.name == "select":
            preferred = _match_text_value(hints)
            opt = _pick_select_option(sel_src, preferred=preferred)
            if opt:
                reason = (
                    "select: matched default option"
                    if preferred
                    else "select: first real option chosen"
                )
                return opt, reason
        return None, "select: no usable options found"

    # For plain text-ish inputs, skip if the user already typed something.
    if field.get("value_present"):
        return None, "already has a value"

    # Textareas and large text blocks.
    if role == "textarea":
        val = _match_text_value(hints)
        if val:
            return val, "textarea matched by label"
        label_clean = re.sub(r"[^a-z0-9 ]", " ", label.lower()).strip()
        return (
            f"This is a sample {label_clean or 'response'}. Replace with real details.",
            "textarea generic sample",
        )

    # Label/name-based defaults take precedence over generic type defaults
    # so that fields like "Salary" or "Date of Birth" get meaningful values.
    val = _match_text_value(hints)
    if val:
        return val, "matched by label/name"

    # Type-based defaults.
    if ftype == "email":
        return DEFAULTS["email"], "email type default"
    if ftype in ("tel", "telephone"):
        return DEFAULTS["phone"], "tel type default"
    if ftype == "url":
        return DEFAULTS["website"], "url type default"
    if ftype == "number":
        return DEFAULTS["number"], "number type default"
    if ftype == "date":
        return DEFAULTS["start_date"], "date type default"

    # Fallback.
    if ftype in ("text", "search"):
        return DEFAULTS["generic_text"], "generic text default"

    return None, f"unhandled type/role: {ftype}/{role}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Universal form filler")
    parser.add_argument("--dry-run", action="store_true", help="Plan fills but do not execute")
    parser.add_argument(
        "--profile", type=Path, help="JSON file mapping field hints to custom values"
    )
    args = parser.parse_args()

    # Load custom profile if provided.
    if args.profile:
        with open(args.profile) as f:
            overrides = json.load(f)
        DEFAULTS.update(overrides)
        refresh_patterns()

    mcp = SenseiMCP()
    try:
        print("[SmartFormFill] X-raying current page...")
        page = mcp.read_full()
        url = page.get("result", {}).get("url", "")
        title = page.get("result", {}).get("title", "")
        print(f"[SmartFormFill] Page: {title} ({url})")

        print("[SmartFormFill] Fetching page source for select/radio options...")
        soup = _fetch_page_source(url) if url else None

        fields = discover_fields(page)
        print(f"[SmartFormFill] Discovered {len(fields)} fillable field(s).\n")

        plan = []
        radio_groups: set[str] = set()
        for field in fields:
            value, reason = decide_value(field, soup)
            # Only fill the first radio button in each name group; skip duplicates.
            if value is not None:
                src = _source_lookup(field["selector"], soup) if soup else None
                if src and src.name == "input" and src.get("type", "").lower() == "radio":
                    name = src.get("name") or field["selector"]
                    if name in radio_groups:
                        value, reason = None, "radio: duplicate group member"
                    else:
                        radio_groups.add(name)
            plan.append({**field, "value": value, "reason": reason})

        # Print plan.
        for p in plan:
            status = p["value"] if p["value"] is not None else "SKIP"
            print(f"  {p['selector']:30} {p['type']:10} -> {status!r} ({p['reason']})")

        if args.dry_run:
            print("\n[SmartFormFill] Dry run — no fields were modified.")
            return 0

        print("\n[SmartFormFill] Filling fields...")
        filled = skipped = failed = 0
        for p in plan:
            if p["value"] is None:
                skipped += 1
                continue
            res = mcp.fill(p["selector"], str(p["value"]))
            ok = bool(res.get("ok"))
            if ok:
                filled += 1
                print(f"  OK   {p['selector']}")
            else:
                failed += 1
                err = res.get("error", res.get("reason", res.get("raw", "unknown")))[:120]
                print(f"  FAIL {p['selector']}: {err}")

        print("\n[SmartFormFill] Verifying...")
        page2 = mcp.read_full()
        fields2 = {f["selector"]: f for f in discover_fields(page2)}
        for p in plan:
            if p["value"] is None:
                continue
            f2 = fields2.get(p["selector"])
            observed = f2.get("value_present", False) if f2 else False
            status = "verified" if observed else "not verified"
            print(f"  {p['selector']:30} -> {status}")

        print(f"\n[SmartFormFill] Done — filled: {filled}, skipped: {skipped}, failed: {failed}")
        return 0 if failed == 0 else 1
    finally:
        mcp.close()


if __name__ == "__main__":
    raise SystemExit(main())
