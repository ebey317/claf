#!/usr/bin/env python3
"""sensei_supervisor.py - Iron Fist Supervisor for CLAF"""

import json
import re

MODES = {
    "play": {
        "system": "You are a friendly helper for kids. Use simple words. Be warm and silly. Never use tools.",
        "tools": False,
        "auto_execute": False,
        "max_tokens": 512,
    },
    "talk": {
        "system": "You are a calm listener. Be gentle. Do not offer solutions unless asked. Never use tools.",
        "tools": False,
        "auto_execute": False,
        "max_tokens": 2048,
    },
    "work": {
        "system": None,
        "tools": True,
        "auto_execute": True,
        "max_tokens": 4096,
    },
}

TIER_1_SAFE = {"Read", "Glob", "Grep", "LS", "View", "Cat", "Head", "Tail"}
TIER_2_EDIT = {"Edit", "Write", "Create", "Delete", "Move", "Bash"}
DANGEROUS_PATTERNS = [
    r"rm\s+-rf\s+/",
    r"mkfs\.",
    r"dd\s+if=.*of=/dev",
    r">\s*/etc/",
    r"curl.*\|.*sh",
    r"wget.*\|.*sh",
    r"sudo",
    r"chmod\s+777",
]

THERAPY_SIGNALS = {
    "sad",
    "depressed",
    "lonely",
    "hurt",
    "cry",
    "crying",
    "kill myself",
    "suicide",
    "suicidal",
    "anxious",
    "anxiety",
    "scared",
    "afraid",
    "need to talk",
    "feeling down",
    "empty",
    "numb",
    "hopeless",
    "worthless",
    "self-harm",
    "cutting",
    "overdose",
    "end it all",
}

KID_SIGNALS = {
    "mommy",
    "daddy",
    "teacher",
    "school",
    "homework",
    "fortnite",
    "minecraft",
    "roblox",
    "pokemon",
}

WORK_SIGNALS = {
    "navigate",
    "click",
    "fill",
    "submit",
    "scrape",
    "automation",
    "browser",
    "website",
    "login",
    "password",
    "form",
    "url",
    "go to",
    "open",
    "visit",
}


def sniff_mode(user_message, has_tools, has_images):
    msg_lower = user_message.lower()
    words = set(re.findall(r"\b\w+\b", msg_lower))
    if has_tools:
        return "work"
    if WORK_SIGNALS & words and len(user_message) < 200:
        return "work"
    if THERAPY_SIGNALS & words:
        return "talk"
    kid_score = sum(1 for s in KID_SIGNALS if s in msg_lower)
    if kid_score >= 2 or (len(user_message) < 50 and "?" in user_message):
        return "play"
    return "talk"


def classify_risk(tool_name, args, project_root):
    if tool_name in TIER_1_SAFE:
        return "ALLOW"
    if tool_name in TIER_2_EDIT:
        path = args.get("path", args.get("file", args.get("target", "")))
        if path and (path.startswith(project_root) or not path.startswith("/")):
            return "ALLOW"
    arg_str = json.dumps(args).lower()
    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, arg_str):
            return "BLOCK"
    return "ESCALATE"


def build_system_prompt(mode, tools):
    if mode == "work" and tools:
        tool_xml = "\n".join(_tool_to_xml(t) for t in tools)
        return (
            "You are a precise tool executor. Emit EXACTLY one XML block:\n"
            "<tool_call>\n"
            "  <name>TOOL_NAME</name>\n"
            '  <parameters>{"key":"value"}</parameters>\n'
            "</tool_call>\n"
            "Available tools:\n<tools>\n" + tool_xml + "\n</tools>\n"
            "Rules:\n- Use <tool_call> ONLY when you need a tool.\n"
            "- Parameters must be valid JSON.\n"
            "- Do NOT explain. Do NOT ask. JUST EMIT XML.\n"
            "- Stop immediately after </tool_call>."
        )
    return MODES[mode]["system"]


def _tool_to_xml(tool):
    name = tool.get("name", "unknown")
    desc = tool.get("description", "")
    schema = tool.get("inputSchema", {})
    props = schema.get("properties", {})
    required = schema.get("required", [])
    lines = [f"  <tool name='{name}'>", f"    <description>{desc}</description>"]
    if props:
        lines.append("    <parameters>")
        for key, info in props.items():
            req = "required" if key in required else "optional"
            pdesc = info.get("description", "")
            ptype = info.get("type", "string")
            lines.append(f"      <param name='{key}' type='{ptype}' usage='{req}'>")
            lines.append(f"        {pdesc}")
            lines.append("      </param>")
        lines.append("    </parameters>")
    lines.append("  </tool>")
    return "\n".join(lines)


TOOL_CALL_RE = re.compile(
    r"<tool_call>\s*<name>([^<]+)</name>\s*<parameters>(.*?)</parameters>(?:\s*</tool_call>)?",
    re.DOTALL,
)


def parse_work_response(text, tools):
    if not tools:
        return [{"type": "text", "text": text or ""}], False
    blocks = []
    pos = 0
    for m in TOOL_CALL_RE.finditer(text):
        if m.start() > pos:
            before = text[pos : m.start()].strip()
            if before:
                blocks.append({"type": "text", "text": before})
        name = m.group(1).strip()
        params_str = m.group(2).strip()
        try:
            params = json.loads(params_str)
        except json.JSONDecodeError:
            params = {"raw": params_str}
        blocks.append(
            {
                "type": "tool_use",
                "id": f"toolu_{name}_{hash(m.group(0)) & 0xFFFFFFFF:08x}",
                "name": name,
                "input": params,
            }
        )
        pos = m.end()
    if pos < len(text):
        after = text[pos:].strip()
        if after:
            blocks.append({"type": "text", "text": after})
    if not blocks:
        blocks.append({"type": "text", "text": text or ""})
    return blocks, any(b["type"] == "tool_use" for b in blocks)


def parse_chat_response(text):
    return [{"type": "text", "text": text or ""}]
