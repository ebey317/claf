SELF-DEBUG — read the data before retrying anything
Bridge access log: ~/scripts/sensei_bridge.log (every HTTP hit + status; 404 on action path = endpoint mismatch)
Bridge audit: ~/.sensei_bridge_audit.jsonl (queue_push → queue_pop → action_result flow, timestamped)
Router log: ~/projects/claf/orchestrator.log (route_decision, trims, tool caps, giveup_detected events)
Docs: ~/scripts/ARCHITECTURE.md, ~/scripts/howwework.txt, ~/projects/claf/CLAUDE.md
Use Read/Bash/Grep on these paths directly. Diagnose at the data layer — never guess from the symptom.
