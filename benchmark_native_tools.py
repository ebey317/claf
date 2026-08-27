#!/usr/bin/env python3
"""Benchmark native tool-calling vs. constrained decoding on CLAF.

Three lanes:
1. Local constrained (CLAF_LOCAL_CONSTRAINED=1, format grammar)
2. Local native (CLAF_LOCAL_CONSTRAINED=0, native tool_calls)
3. Cloud OpenRouter (anthropic/claude-sonnet-4.6)

Each sends 5 identical Sensei tool schemas and a simple user request,
measures wall-clock time and tool-call correctness.
"""

import json
import os
import sys
import time
from pathlib import Path

# Add CLAF to path for imports
sys.path.insert(0, str(Path(__file__).parent))

import httpx


# A representative Sensei tool set (5 tools) for benchmarking.
SENSEI_TOOLS = [
    {
        "name": "mcp__sensei__tab_create",
        "description": "Create a new browser tab with a URL",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "URL to open in the new tab",
                }
            },
            "required": ["url"],
        },
    },
    {
        "name": "mcp__sensei__click",
        "description": "Click on an element in the browser",
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": "CSS selector for the element to click",
                }
            },
            "required": ["selector"],
        },
    },
    {
        "name": "mcp__sensei__fill",
        "description": "Fill a form field with text",
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": "CSS selector for the input field",
                },
                "text": {
                    "type": "string",
                    "description": "Text to fill",
                },
            },
            "required": ["selector", "text"],
        },
    },
    {
        "name": "mcp__sensei__read",
        "description": "Read the current page content",
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": "Optional CSS selector to read a specific element",
                }
            },
            "required": [],
        },
    },
    {
        "name": "mcp__sensei__screenshot",
        "description": "Take a screenshot of the current page",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]

# Simple Anthropic-format request with tools
REQUEST_BODY = {
    "model": "gpt-4o",  # Will be overridden by CLAF routing
    "messages": [
        {
            "role": "user",
            "content": "Open google.com, search for 'benchmarking tool calling', and show me the results.",
        }
    ],
    "tools": SENSEI_TOOLS,
    "max_tokens": 1024,
}


def benchmark_local_constrained():
    """Benchmark local with constrained decoding (CLAF_LOCAL_CONSTRAINED=1)."""
    print("\n=== LOCAL CONSTRAINED (format grammar) ===")
    os.environ["CLAF_LOCAL_CONSTRAINED"] = "1"
    return _benchmark_endpoint("local_constrained", "http://localhost:8000/v1/messages")


def benchmark_local_native():
    """Benchmark local with native tool_calls (CLAF_LOCAL_CONSTRAINED=0)."""
    print("\n=== LOCAL NATIVE (native tool_calls) ===")
    os.environ["CLAF_LOCAL_CONSTRAINED"] = "0"
    # Need to restart CLAF for env change to take effect
    # For now, we'll just note that this requires a service restart
    return _benchmark_endpoint("local_native", "http://localhost:8000/v1/messages")


def benchmark_cloud_openrouter():
    """Benchmark cloud via OpenRouter (anthropic/claude-sonnet-4.6)."""
    print("\n=== CLOUD OPENROUTER (anthropic/claude-sonnet-4.6) ===")

    # Read API key from ~/.master_ai_keys
    keys_file = Path.home() / ".master_ai_keys"
    openrouter_key = None

    if keys_file.exists():
        try:
            content = keys_file.read_text()
            # Try JSON format first
            try:
                data = json.loads(content)
                openrouter_key = data.get("openrouter") or data.get("OPENROUTER_API_KEY")
            except json.JSONDecodeError:
                # Try KEY=VALUE format
                for line in content.splitlines():
                    if line.startswith("openrouter=") or line.startswith("OPENROUTER_API_KEY="):
                        openrouter_key = line.split("=", 1)[1].strip()
                        break
        except Exception as e:
            print(f"Error reading keys file: {e}")

    if not openrouter_key:
        print("SKIPPED: OPENROUTER_API_KEY not found in ~/.master_ai_keys")
        return None

    # Prepare request for OpenRouter
    or_body = {
        "model": "anthropic/claude-sonnet-4.6",
        "messages": REQUEST_BODY["messages"],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["input_schema"],
                },
            }
            for t in SENSEI_TOOLS
        ],
        "max_tokens": 1024,
    }

    headers = {
        "Authorization": f"Bearer {openrouter_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/ebey317/claf",
        "X-Title": "CLAF Native Tool-Calling Benchmark",
    }

    try:
        start = time.time()
        with httpx.Client(timeout=300.0) as client:
            r = client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                json=or_body,
                headers=headers,
            )
        elapsed = time.time() - start

        if r.status_code != 200:
            print(f"HTTP {r.status_code}: {r.text[:200]}")
            return None

        data = r.json()
        tool_calls = []
        if "choices" in data and data["choices"]:
            msg = data["choices"][0].get("message", {})
            tool_calls = msg.get("tool_calls", [])

        valid = all(
            isinstance(tc, dict) and "function" in tc
            for tc in tool_calls
        )

        return {
            "elapsed_s": elapsed,
            "tool_calls": len(tool_calls),
            "valid": valid,
            "status": "OK" if valid else "INVALID",
        }
    except Exception as e:
        print(f"Error: {e}")
        return None


def _benchmark_endpoint(name: str, url: str) -> dict | None:
    """Send request to endpoint and measure time + tool-call validity."""
    try:
        start = time.time()
        with httpx.Client(timeout=300.0) as client:
            r = client.post(url, json=REQUEST_BODY)
        elapsed = time.time() - start

        if r.status_code != 200:
            print(f"HTTP {r.status_code}: {r.text[:200]}")
            return None

        data = r.json()

        # Extract tool_calls from response (Anthropic format)
        tool_calls = []
        content = data.get("content", [])
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_calls.append(block)

        valid = len(tool_calls) > 0 and all(
            isinstance(tc, dict) and tc.get("type") == "tool_use"
            for tc in tool_calls
        )

        result = {
            "elapsed_s": elapsed,
            "tool_calls": len(tool_calls),
            "valid": valid,
            "status": "OK" if valid else "TIMEOUT/INVALID",
        }

        print(f"⏱  {elapsed:.1f}s | Tools: {len(tool_calls)} | Valid: {valid}")
        return result

    except httpx.TimeoutException:
        print("⏱  TIMEOUT (300s+)")
        return {
            "elapsed_s": 300.0,
            "tool_calls": 0,
            "valid": False,
            "status": "TIMEOUT",
        }
    except Exception as e:
        print(f"Error: {e}")
        return None


def main():
    """Run all three benchmarks."""
    print("=" * 70)
    print("CLAF Native Tool-Calling Benchmark")
    print("=" * 70)

    results = {}

    # Lane 1: Local constrained (known to timeout at 300s+)
    print("\n[1/3] Local constrained decoding (CLAF_LOCAL_CONSTRAINED=1)")
    print("(Using known 300s+ timeout result from earlier test)")
    results["local_constrained"] = {
        "elapsed_s": 300.0,
        "tool_calls": 0,
        "valid": False,
        "status": "TIMEOUT (from prior benchmark)",
    }
    print(f"⏱  TIMEOUT | Status: {results['local_constrained']['status']}")

    # Lane 2: Local native (should be ~29.5s based on prior test)
    print("\n[2/3] Local native tool_calls (CLAF_LOCAL_CONSTRAINED=0)")
    print("(Service restart required; will test after restart)")
    results["local_native"] = {
        "elapsed_s": None,
        "tool_calls": None,
        "valid": None,
        "status": "PENDING (needs service restart)",
    }

    # Lane 3: Cloud OpenRouter
    print("\n[3/3] Cloud OpenRouter")
    or_result = benchmark_cloud_openrouter()
    if or_result:
        results["openrouter"] = or_result
    else:
        results["openrouter"] = {
            "elapsed_s": None,
            "tool_calls": None,
            "valid": None,
            "status": "SKIPPED/ERROR",
        }

    # Summary table
    print("\n" + "=" * 70)
    print("BENCHMARK RESULTS SUMMARY")
    print("=" * 70)
    print(f"{'Lane':<30} {'Time (s)':<15} {'Valid':<10} {'Status'}")
    print("-" * 70)
    for name, res in results.items():
        elapsed = f"{res['elapsed_s']:.1f}" if res["elapsed_s"] is not None else "N/A"
        valid = str(res["valid"]) if res["valid"] is not None else "N/A"
        status = res["status"]
        print(f"{name:<30} {elapsed:<15} {valid:<10} {status}")

    print("\nNext steps:")
    print("1. Flip CLAF_LOCAL_CONSTRAINED default to 0 in .env")
    print("2. Restart claf.service: systemctl --user restart claf")
    print("3. Re-run this benchmark to measure local_native performance")


if __name__ == "__main__":
    main()
