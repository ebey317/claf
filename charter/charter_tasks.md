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

MEMORY — writing to persistent memory
Memory files live at ~/.claude/projects/-home-elijah/memory/. Use Write or Edit tool to create/update .md files there.
MEMORY.md at that path is the index — add a one-line pointer for every new memory file.
