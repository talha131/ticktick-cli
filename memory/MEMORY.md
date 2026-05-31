Persistent context for this repo. Lighter than the workspace pattern — sessions
here are episodic, not daily, so there's no "read at session start" rule.
**Check these files when working on related context** (the file names should
make relevance obvious).

- [Predecessor — todolist-optimizer](project_predecessor_todolist_optimizer.md) — this repo's ancestor. Archived at github.com/talha131/todolist-optimizer; holds the original "AI prioritization brain" spec + plan + design history not preserved here.
- [Feature request — edit / punt / bump-priority subcommand](feature_request_triage_edit_subcommand.md) — shipped 2026-05-30 in commits `9c3bbce..bb56493`. Kept as historical context: it captures the why behind the three new verbs and the workspace-agent triage flow that requested them.
- [Debate — scoping the retry-with-backoff fix (2026-05-31)](debate_2026-05-31_retry_scoping.md) — three-voice debate (Gemini, Sonnet, Opus) on the post-incident hardening plan. Conclusion: ship #2 retry alone first; design answers for `_request` helper, POST pre-send-only retry, 0.5/2/8s backoff, stderr logging.
