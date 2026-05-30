---
name: Predecessor — todolist-optimizer
description: This repo's predecessor was github.com/talha131/todolist-optimizer. That repo is being archived (not deleted) and holds the original "AI prioritization brain" design specs and plans not preserved here.
type: project
---

This repo (`ticktick-cli`) is the active continuation of an earlier
project named `todolist-optimizer`. The rename happened on 2026-05-30
when the project's scope narrowed from an "AI prioritization brain"
(MCP server + slash commands + AI metadata layer in-CLI) to a focused
**TickTick Open API wrapper**. Everything AI-flavored — effort
estimation, mode tagging, ranking, snooze, reports — moved out of the
CLI and into a separate "workspace" Claude session.

## Where the predecessor lives

- **GitHub:** `github.com/talha131/todolist-optimizer` — archived (or
  scheduled to be archived). Read-only, public, preserves the full
  commit history.
- **Local (as of 2026-05-30):** `~/Developer/todolist-optimizer/` —
  Talha plans to delete locally after a week of confirmed working
  `ticktick-cli` usage.

## What the predecessor has that this repo deliberately doesn't

- `docs/superpowers/specs/2026-05-25-prioritization-brain-design.md`
  — original MCP-server-based design (v2 after a multi-LLM review).
  Worth reading if you ever wonder why we made a particular
  architectural choice; usually the answer is "because the alternative
  was worse, and the spec explains it."
- `docs/superpowers/plans/2026-05-25-prioritization-brain-plan.md` —
  the 20-task TDD plan that built the data layer that survives in
  this repo (store / sync / candidates / auth / ticktick client).
- `docs/workspace/CLAUDE.md.template` — old workspace agent
  template; superseded by the workspace having its own CLAUDE.md
  outside this repo.
- ~30 commits documenting the brainstorm → spec → plan → build →
  pivot → rename journey.

## When this matters

- **"Why did we pick X?"** — the rationale is often in the original
  spec or its multi-LLM review thread. Clone the archived repo if
  you want to read it without the GitHub UI.
- **"Can we revive the old approach?"** — Default answer: no. The
  pivot to CLI-only was deliberate and the current architecture
  (workspace + thin CLI) is working. If something genuinely broken
  would benefit from the old design, surface that — but don't assume
  the old design was better just because it's documented.
- **"Where's the test cassette from?"** — TickTick API cassette
  fixtures originated in the predecessor's 20-task plan; the schema
  in `tests/fixtures/ticktick_cassette.json` traces back to the
  original design.
