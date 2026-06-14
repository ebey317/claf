TASK LOOP (AUTONOMOUS MODE only)
TaskList → claim next unblocked → execute → mark complete → re-pull. Never idle when queue has items.

EMPTY TASK LIST
Backlog is already in injected context under "OPEN TASKS" / "[SESSION SNAPSHOT]" / "[TASK_SEED]". Display those tasks or seed them with TaskCreate. Never call a made-up tool to "find" tasks. Never call switch_protocol.

ACTIVE TASK FILE — persist progress across loop resets
~/.claf/current_task.json tracks the current multi-step goal. It is injected at the top of every turn so you always know where you are.
- START a task: Write a JSON file to ~/.claf/current_task.json with keys: goal (string), items (array of {id, task, status: "pending"})
- UPDATE an item: Edit the file, set status to "done", "failed", or "skip" and add a note if useful
- FINISH a task: When all items are done, delete the file with Bash: rm ~/.claf/current_task.json
Never re-derive task state from scratch — read the file instead.

CONTINUATION RULE (next-step fidelity) — THIS IS WHAT KEEPS AUTOMATION ALIVE
If the [ACTIVE TASK] block above shows ANY pending item (icon ⬜, status not done/failed/skip), you are MID-TASK. A text-only reply is FORBIDDEN — it ends the loop and abandons the work. Every turn while items remain, you MUST emit your NEXT tool call. The only times prose is allowed: (a) every item is ✅ done or ❌ failed AND you deleted the task file, or (b) you are blocked and need ONE specific answer the operator alone can give — then ask exactly one question. Never narrate "I've completed X, next I'll do Y" and stop: do Y in the same turn by calling the tool.

RICH TASK STRUCTURE — for browser / multi-step / failure-prone work
For anything beyond a single file edit, add strategy, success_criteria, and fallback_chain fields. The orchestrator injects them every turn. See ~/projects/claf/TASK_STRUCTURE_RUNBOOK.md for the full pattern and an annotated example.

MEMORY — writing to persistent memory
Memory files live at ~/.claude/projects/-home-elijah/memory/. Use Write or Edit tool to create/update .md files there.
MEMORY.md at that path is the index — add a one-line pointer for every new memory file.

LONG-RUNNING TASK CHECKPOINTS (MODEL CONTEXT RESET)
Small local models lose coherence after ~5 tool calls in one pass.  After every `CLAF_CHECKPOINT_EVERY` completed steps (default 5), the engagement loop pauses, writes a machine-readable checkpoint to `~/.claf/engagement_checkpoint.json`, appends a context-reset summary to `~/MD/notepad.md`, and marks the HANDOFF task `⏸️`.  The operator re-running the loop is just the trigger; the real purpose is to give Mary a fresh context window before she continues, preventing hallucination on long chains.
