#!/usr/bin/env python3
import json, re
from typing import Any

TOOL_CALL_RE = re.compile(
    r"<tool_call>\s*<name>([^<]+)</name>\s*<parameters>(.*?)</parameters>(?:\s*</tool_call>)?",
    re.DOTALL,
)


def _tool_to_xml(tool: dict) -> str:
    name = tool.get("name", "unknown")
    desc = tool.get("description", "")
    schema = tool.get("inputSchema", {})
    props = schema.get("properties", {})
    required = schema.get("required", [])
    lines = [f'  <tool name="{name}">', f"    <description>{desc}</description>"]
    if props:
        lines.append("    <parameters>")
        for key, info in props.items():
            req = "required" if key in required else "optional"
            pdesc = info.get("description", "")
            ptype = info.get("type", "string")
            lines.append(f'      <param name="{key}" type="{ptype}" usage="{req}">')
            lines.append(f"        {pdesc}")
            lines.append("      </param>")
        lines.append("    </parameters>")
    lines.append("  </tool>")
    return "\n".join(lines)


def prepare_ollama_request(messages: list[dict], tools: list[dict] | None, model: str) -> tuple:
    if not tools:
        return messages, None, False
    tool_xml = "\n".join(_tool_to_xml(t) for t in tools)
    system_msg = (
        "You are a helpful assistant with access to tools.\n"
        "When you need to use a tool, emit EXACTLY one XML block like this:\n"
        "<tool_call>\n"
        "  <name>TOOL_NAME</name>\n"
        '  <parameters>{"key":"value"}</parameters>\n'
        "</tool_call>\n"
        "Available tools:\n"
        f"<tools>\n{tool_xml}\n</tools>\n"
        "Rules:\n"
        "- Use <tool_call> ONLY when you need a tool.\n"
        "- The parameters value must be valid JSON.\n"
        "- After the tool call, wait for the result before continuing.\n"
        "- Do NOT explain that you are using a tool. Just emit the XML."
    )
    out_msgs = []
    for m in messages:
        if m.get("role") == "system":
            out_msgs.append(
                {"role": "system", "content": system_msg + "\n\n" + str(m.get("content", ""))}
            )
        else:
            out_msgs.append(m)
    if not any(m.get("role") == "system" for m in out_msgs):
        out_msgs.insert(0, {"role": "system", "content": system_msg})
    return out_msgs, tools, True


def parse_ollama_response_to_anthropic_blocks(text: str, tools: list[dict] | None) -> tuple:
    if not tools:
        return [{"type": "text", "text": text or ""}], False
    blocks: list[dict] = []
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
    return blocks, len(blocks) > 1 or any(b["type"] == "tool_use" for b in blocks)
