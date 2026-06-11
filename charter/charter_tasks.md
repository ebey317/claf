TASK LOOP (AUTONOMOUS MODE only)
TaskList → claim next unblocked → execute → mark complete → re-pull. Never idle when queue has items.

EMPTY TASK LIST
Backlog is already in injected context under "OPEN TASKS" / "[SESSION SNAPSHOT]" / "[TASK_SEED]". Display those tasks or seed them with TaskCreate. Never call a made-up tool to "find" tasks. Never call switch_protocol.

MEMORY — writing to persistent memory
Memory files live at ~/.claude/projects/-home-elijah/memory/. Use Write or Edit tool to create/update .md files there.
MEMORY.md at that path is the index — add a one-line pointer for every new memory file.
