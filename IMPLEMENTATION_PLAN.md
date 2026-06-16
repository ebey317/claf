# 🚀 CLAF MASTER IMPLEMENTATION PLAN: ZERO-LAG AUTOMATION
**Lead Agent:** Gemma 4 (Overseer)
**Status:** ACTIVE / IN-PROGRESS
**Hardware:** Elijah (GTX 1660 Ti 6GB) | Mary (HP Pro)

## 🎯 THE NORTH STAR
Transform CLAF from a "Smart Proxy" into a "Zero-Lag Automation Engine." The goal is an instant, multimodal experience where the user never feels the "seams" between local, free-cloud, and paid-cloud agents.

---

## 🛠️ PHASE 1: THE ROUTER OVERHAUL (HIGH PRIORITY)
**Problem:** "Try $\rightarrow$ Fail $\rightarrow$ Escalate" loop causes perceived lag.
**Goal:** Instant dispatch to the fastest capable brain.

### 1.1 The "Fast-Path" Bypass
- **Logic:** Implement a priority check at the very top of `orchestrator.py`.
- **Trigger:** If the request is a "Deterministic Toolbox" match or a simple "System Check," bypass the needle-scans and hit `local-ollama` immediately.
- **Outcome:** 0ms routing overhead for routine tasks.

### 1.2 Parallel Speculative Dispatch
- **Logic:** For "Medium" complexity tasks, dispatch the request to BOTH `local-ollama` (qwen2.5-coder:7b) and a fast cloud peer (Kimi/Groq) simultaneously.
- **Winning Condition:** The first response to return a valid JSON tool-call or high-confidence text wins. The other is cancelled.
- **Outcome:** Cloud-level reasoning at local-level speeds.

### 1.3 Dynamic Tiering
- **Update:** Finalize the `claf_config.py` tiering to prioritize:
  `Local-7B` $\rightarrow$ `Ollama-Cloud-Free (Gemma4/Nemotron)` $\rightarrow$ `Groq` $\rightarrow$ `Paid Tiers`.

---

## 🎙️ PHASE 2: VOICE BRIDGE HARDENING
**Problem:** `voice_bridge.py` is unauthenticated and lacks robust error handling.
**Goal:** A production-grade voice-to-text pipeline.

### 2.1 Security Layer
- Implement a simple `X-API-KEY` check or local-subnet restriction in the FastAPI middleware to prevent external spam of the Groq endpoint.

### 2.2 Resilience Logic
- Wrap `httpx` calls to Groq in a retry-loop with a strict 5s timeout.
- Implement a "Fallback to Local" STT if the cloud endpoint 429s.

---

## 🏎️ PHASE 3: HARDWARE & OS TUNING
**Problem:** VRAM limits on the 1660 Ti and untapped Windows partition data.
**Goal:** Maximum throughput on Elijah's gaming rig.

### 3.1 VRAM Optimization
- Audit `ollama` load settings. Ensure `qwen2.5-coder:7b` is using the optimal K-Quant to stay 100% GPU-resident.
- Set `renice` priority for `claf.service` to ensure the orchestrator never throttles.

### 3.2 The Windows Bridge (`sda3`)
- Map key directories from the NTFS partition into the Linux environment.
- Identify specific Windows-only AI tools and create "Virtual-Triggers" in the router to let the user know when a task requires a reboot into Windows.

---

## ♾️ PHASE 4: THE PERMANENT MEMORY LOOP
**Goal:** Total immunity to session timeouts.

### 4.1 The Triple-Lock Sync
Every major turn must update:
1. `~/MD/HANDOFF.md` $\rightarrow$ The "Executive Summary" for any agent.
2. `~/projects/claf/IMPLEMENTATION_PLAN.md` $\rightarrow$ The "Technical Roadmap."
3. `~/.claf/current_task.json` $\rightarrow$ The "Immediate Next Step."

### 4.2 Secret Mapping
Maintain `~/MD/SECRET_MAP.md` (private) to map service names to their key locations in the filesystem, ensuring zero-friction auth recovery.

---

## ✅ VERIFICATION GATE
A task is only "DONE" when:
1. It passes the `requesting-code-review` pipeline.
2. It is verified live on **Elijah** and **Mary**.
3. The `HANDOFF.md` is updated.
