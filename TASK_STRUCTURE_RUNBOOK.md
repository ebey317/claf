# CLAF Task Structure Runbook

A flat to-do list is enough for 1-step chores. For anything that touches the browser, files, or external APIs, use this richer structure so the local model can recover when the first attempt fails.

## File location

`~/.claf/current_task.json` — the orchestrator injects this at the start of every turn.

## Required fields

- `goal` (string): one-sentence mission.
- `items` (array): `{id, task, status, note?}` objects. Status is `pending`, `done`, `failed`, or `skip`.

## Strongly recommended fields

- `strategy` (string): the high-level plan before any tool is called.
- `success_criteria` (array of strings): measurable checks that tell the agent it is finished.
- `fallback_chain` (array of strings): ordered escape hatches when the primary path breaks.

## Optional fields

- `context` (string): URLs, file paths, or prior decisions the agent should keep in mind.
- `max_iterations` (number): guardrail so the loop does not spin forever.

## Why this works

The orchestrator's `format_task_for_injection()` now emits `strategy`, `success_criteria`, and `fallback_chain` every turn. The model does not have to re-derive them after a context reset or a failed tool call.

## Example: browser navigation task

```json
{
  "goal": "Go to anthropic.com and open the CLI docs page in the MCP section",
  "strategy": "Navigate to Anthropic → find Claude Code docs → locate MCP section → open CLI reference. Prefer direct URLs; use search only if navigation fails.",
  "success_criteria": [
    "Page loaded is on an anthropic.com / claude.com / code.claude.com domain",
    "Page title or H1 mentions MCP and/or CLI docs",
    "Content includes actual `claude mcp ...` CLI commands"
  ],
  "fallback_chain": [
    "1. Direct browse to https://www.anthropic.com and look for Docs link",
    "2. If docs link is hidden/truncated, search: site:anthropic.com MCP CLI documentation",
    "3. If search tab breaks (chrome://newtab), create a fresh tab and go directly to https://docs.anthropic.com/en/docs/claude-code",
    "4. If redirects land on code.claude.com/docs, extract MCP link from DOM",
    "5. If DOM tools are truncated, use FetchURL on https://code.claude.com/docs/en/mcp as final fallback"
  ],
  "items": [
    {"id": "1", "task": "Open Anthropic / Claude Code docs entrypoint", "status": "pending"},
    {"id": "2", "task": "Locate MCP section link", "status": "pending"},
    {"id": "3", "task": "Navigate to MCP CLI docs and verify content", "status": "pending"}
  ]
}
```

## Tool-call discipline

1. **Prefer the fastest path first.** Direct `browse` or `get_dom` beats search.
2. **Verify state after every external action.** Call `read`, `tab_list`, or `get_dom` before assuming success.
3. **Name failures explicitly.** If a tool call fails, put the error in the item `note` before trying the fallback.
4. **Escalate through the fallback chain.** Do not retry the same failed approach more than twice unless the failure reason changed.
5. **Use static fetch as the final fallback.** `FetchURL` works when dynamic browser rendering is truncated or broken.
6. **Update the file immediately.** Rewrite `current_task.json` after each meaningful state change so a loop restart can resume correctly.

## Per-item note convention

Use notes to capture:

- What was actually tried (`browse → url`, `get_dom selector=...`)
- The result or error
- Which fallback is now active

This turns the task file into a durable log, not just a checklist.
