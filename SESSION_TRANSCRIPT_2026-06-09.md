# Session Transcript — 2026-06-09
## Agent: Kimi (CLAF Hybrid Mode Fix + Research Tools)

---

### 🔍 DIAGNOSIS: Why Hybrid Mode Was Broken

**Root Cause 1: Local model had ZERO tools**
- `CLAF_LOCAL_MAX_TOOLS=0` → all 74 tools stripped
- Local model couldn't call anything → no tool_use blocks → no escalation trigger

**Root Cause 2: Escalation thresholds were sky-high**
- System prompt > 40k chars (CLAUDE.md is 10k, never triggered)
- Messages > 60 (normal convos never hit it)
- Only trigger that worked was `[ESCALATE]` marker

**Root Cause 3: Cloud peers ALL dead (no credits)**
- Anthropic Console key: VALID but ZERO credits
- OpenRouter: key valid, 0 models, no credits (402)
- Groq: free tier rate limited (429)
- Cerebras: rate limited (429)
- Ollama Cloud: subscription required (429)
- Result: EVERY hard task fell back to local 3B model

**Root Cause 4: Local model had amnesia**
- `CLAF_LOCAL_MAX_MSGS=4` → forgot conversation after 4 messages
- `CLAF_LOCAL_SYS_MAX_CHARS=1500` → 10KB system prompt gutted to 1.5KB
- Model didn't know its own identity or that it should chain tools

---

### ✅ FIXES APPLIED

#### 1. `projects/claf/.env`
```diff
- CLAF_LOCAL_MAX_TOOLS=0
+ CLAF_LOCAL_MAX_TOOLS=20

- CLAF_LOCAL_MAX_MSGS=4
+ CLAF_LOCAL_MAX_MSGS=10
```

#### 2. `projects/claf/claf_config.py`
```diff
- system prompt > 40_000 chars → escalate
+ system prompt > 12_000 chars → escalate

- messages > 60 → escalate
+ messages > 20 → escalate

- tool loop only triggers if MAX_TOOLS=0
+ tool loop triggers if MAX_TOOLS ≤ 3
```

#### 3. `projects/claf/orchestrator.py`
- Added `_LOCAL_CHARTER` — prepended to every local request
- Charter tells model: "chain tools, be proactive, zero preamble"
- Protected charter survives system prompt trimming

#### 4. `scripts/mcp_web_search.py` (NEW)
MCP server with 3 research tools:
- `web_search(query, num_results=5)` — Google via Serper
- `wikipedia_search(query, sentences=3)` — free knowledge
- `scrape_page(url, max_chars=8000)` — full page reading via Firecrawl

#### 5. `~/.config/master_ai/mcp_servers.json`
```json
{
  "servers": {
    "filesystem": { ... },
    "web_search": {
      "command": "python3",
      "args": ["/home/elijah/scripts/mcp_web_search.py"]
    }
  }
}
```

---

### 📊 BEFORE vs AFTER

| Metric | Before | After |
|---|---|---|
| Local tools | 0 | 20 |
| Message memory | 4 | 10 |
| System prompt | 1,500 chars | 1,500 chars + charter |
| Escalation trigger | never | 12k chars / 20 msgs |
| Web search | ❌ | ✅ Google + Wikipedia |
| Page scraping | ❌ | ✅ Firecrawl |
| Tool chaining | ❌ (amnesia) | ✅ (charter + memory) |

---

### 💸 BILLING STATUS (Blocks Cloud Escalation)

| Provider | Key Valid? | Has Credits? | Action Needed |
|---|---|---|---|
| Anthropic Console | ✅ | ❌ NO | Add $5-20 at console.anthropic.com |
| OpenRouter | ✅ | ❌ NO | Add credits at openrouter.ai |
| Groq | ✅ | ⚠️ Free tier | Rate limits reset; or upgrade |
| Cerebras | ✅ | ⚠️ Rate limited | Wait or check plan |
| Fireworks | ❌ | ❌ | Account suspended |
| Serper | ✅ | ✅ | 10K searches/mo |
| Firecrawl | ✅ | ✅ | Active |

---

### 🚀 TO RESTART CLAF

```bash
# Stop existing CLAF
pkill -f orchestrator.py

# Start fresh
bash ~/projects/claf/launch.sh
```

---

### 🧠 WHAT THE USER LEARNED

- Keychain "probe" only checks if a key is VALID, not if it has CREDITS
- A 3B local model is a smart intern — handles simple tasks, struggles with complex chains
- Hybrid mode is only as good as the cloud peers' billing status
- Tool chaining requires: enough message history + identity instructions + model capability
- Firecrawl + Serper + Wikipedia = research stack for job hunting, forms, docs

---

### 📝 NEXT STEPS (Pending User Decision)

1. **Add cloud credits** ($5-20) to get hybrid escalation working
2. **Test tool chaining** with a multi-step request
3. **Add more MCP servers** (email, calendar, etc.)
4. **Create MEMORY.md** from daily notes so agent has long-term memory
5. **Add DuckDuckGo** as search fallback (if Serper runs out)

---
*Session end: CLAF config updated, research tools live, cloud billing exposed.*
