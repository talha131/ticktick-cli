# Development context for Claude — ticktick-cli

This file loads when Claude Code is opened in this repository. **You
are here to maintain a Python CLI that integrates with TickTick.**
Nothing else.

## What this project is

`ticktick-cli` is a thin wrapper around [TickTick's Open API](https://developer.ticktick.com/),
plus a local SQLite mirror so reads are fast and offline-tolerant. The
canonical TickTick API reference, copied verbatim from their developer
portal, lives at [`docs/ticktick-openapi.md`](docs/ticktick-openapi.md) — that's the
authoritative source for endpoint shapes, request/response schemas, and
the Task object's field list. Consult it before assuming.

This repo deliberately does NOT contain ranking, snooze, mode, effort
estimation, or report generation logic. Those concerns live in a
separate **task workspace** at `~/Documents/Tasks/` (the caller). When
a user opens Claude Code there, they get a different CLAUDE.md geared
toward task management; that session calls `ticktick-cli` as a tool.
Feature requests bubble up from there — see
[`memory/feature_request_triage_edit_subcommand.md`](memory/feature_request_triage_edit_subcommand.md)
for the current one.

## Persistent context — check `memory/` when relevant

This repo has a `memory/` directory for context that outlives any
single conversation: design decisions, predecessor history, pending
feature requests with full specs. Lighter than the workspace's
memory pattern — no "read at session start" rule, since dev work
here is episodic. **Check `memory/MEMORY.md` when you're working on
something the file titles suggest is relevant.**

Current entries:

- `project_predecessor_todolist_optimizer.md` — this repo replaced
  `todolist-optimizer` on 2026-05-30. The predecessor is archived
  at `github.com/talha131/todolist-optimizer` and holds the
  original spec + plan + design history. Useful if you're asked
  "why did we pick X?".
- `feature_request_triage_edit_subcommand.md` — full spec for the
  highest-priority follow-up: extending `update_task` to accept
  `startDate` and `priority`, adding `ticktick-cli edit` and
  optionally `ticktick-cli punt`. The workspace agent is waiting
  on this to make triage verbs ("punt X for 5d", "bump X to
  high") executable from conversation.

## Subcommand surface

| Command | Purpose | Endpoint |
|---|---|---|
| `setup` | One-time OAuth via browser callback | `POST /oauth/token` |
| `sync` | Pull TickTick projects + tasks (active + recent completions) into local SQLite | `GET /open/v1/project`, `GET /open/v1/project/{id}/data`, `POST /open/v1/task/completed` |
| `candidates [--limit N]` | JSON of active tasks from local mirror | local SQLite query |
| `recent [--limit N]` | JSON of recently completed tasks, bounded by `sync.completions_lookback_days` (default 30) | local SQLite query |
| `add <title> --project P [--due ...] [--remind ...] [--repeat RRULE] [--tag ...]` | Create a task | `POST /open/v1/task` |
| `complete <task_id>` | Mark task complete | `POST /open/v1/project/{p}/task/{t}/complete` |
| `delete <task_id> [--apply]` | Delete task (dry-run unless --apply) | `DELETE /open/v1/project/{p}/task/{t}` |
| `remind <task_id> [durations...] [--clear]` | Set reminders | `POST /open/v1/task/{taskId}` |
| `edit <task_id> [--title ...] [--due ... \| --clear-due] [--start ... \| --clear-start] [--priority ...]` | Edit mutable task fields (title, content, due/start dates, priority) | `POST /open/v1/task/{taskId}` |
| `punt <task_id> <when>` | Sugar over `edit --start` — push start date forward | `POST /open/v1/task/{taskId}` |
| `bump <task_id> {none,low,medium,high}` | Sugar over `edit --priority` — set priority by name | `POST /open/v1/task/{taskId}` |
| `move <task_id> --to <project>` | Move task to another project | `POST /open/v1/task/move` |
| `repeat <task_id> [RRULE] [--clear]` | Set/clear task recurrence | `POST /open/v1/task/{taskId}` |
| `tag add <task_id> <tag>...` | Add tags to a task (merges with existing) | `POST /open/v1/task/{taskId}` |
| `tag remove <task_id> <tag>... [--ignore-case]` | Remove tags from a task | `POST /open/v1/task/{taskId}` |
| `tag rename <old> <new> [--apply] [--ignore-case]` | Rename tag across the local mirror (dry-run unless --apply) | `POST /open/v1/task/{taskId}` × N |
| `tag delete <tag> [--apply] [--ignore-case]` | Remove tag across the local mirror (dry-run unless --apply) | `POST /open/v1/task/{taskId}` × N |

Reminder duration syntax: `15m`, `1h`, `2d`, `at-due`, bare integer
(minutes). All translate to iCal TRIGGER strings sent to TickTick.

Recurrence (`--repeat` on `add`, or `repeat` subcommand) is a raw iCal
RRULE string per RFC 5545 — passed through verbatim, no client-side
parsing. Examples: `RRULE:FREQ=DAILY;INTERVAL=1`,
`RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR`.

Tag operations: `tag add`/`tag remove` are per-task and case-sensitive
by default (mirrors TickTick — "Work" and "work" are distinct).
`tag rename` and `tag delete` sweep the **local SQLite mirror** — they
print affected tasks and exit without changes unless `--apply` is
passed. This is NOT a true global rename: the mirror's coverage is
the operation's coverage, so **excluded projects** (filtered during
sync via `excluded_projects_by_name`) and **historical completions**
(which `/project/{id}/data` doesn't return at all) are permanently
invisible regardless of how many syncs you run. There is no TickTick
API endpoint for tag rename/delete; both are emulated by iterating
`update_task` over each row the mirror knows about. *Mirror staleness*
is handled automatically — every tag op pre-syncs (see below) — so
manual `sync` before tag ops is unnecessary.

**Sweep failures are partial, not atomic.** The N `update_task` calls
are independent; there's no server-side transaction. If one fails
mid-loop, earlier tasks have already been mutated on TickTick. Both
sweep commands run the mirror re-sync in a `finally` block so the
local view reflects whatever partial state the server actually holds,
then re-raise. Callers should treat any non-zero exit from `tag rename
--apply` or `tag delete --apply` as "partial application possible —
inspect the mirror." Don't remove this `finally`.

**All tag mutations sync the mirror twice — once before, once after.**
`update_task` replaces the server's tag list wholesale; if our read-
modify-write reads stale local data, tags added on another device
since the last sync get silently dropped. So `cmd_tag_add` /
`cmd_tag_remove` / `cmd_tag_rename` / `cmd_tag_delete` all call
`_resync_mirror()` *before* reading from the mirror, then again
*after* the write to capture our own mutation. Two `Syncer.run()`
calls per tag op is the cost of correctness; don't optimize one away
without a different correctness story.

## File layout

```
ticktick-cli/
├── CLAUDE.md                  ← this file
├── README.md                  ← user-facing setup + reference
├── LICENSE                    ← MIT
├── pyproject.toml             ← uv project, console script entry
├── src/ticktick_cli/
│   ├── __main__.py            ← entry → cli.main()
│   ├── cli.py                 ← argparse subcommands + _home()
│   ├── store.py               ← SQLite schema, pragmas, connection
│   ├── auth.py                ← TickTick OAuth (no refresh tokens)
│   ├── ticktick.py            ← REST client + iCal TRIGGER helpers
│   ├── sync.py                ← transactional cloud → mirror sync
│   ├── candidates.py          ← parameterized active-task query
│   └── config.py              ← settings.yml pydantic models
├── tests/
│   ├── test_*.py              ← per-module unit tests
│   └── fixtures/              ← pytest-httpx cassette
├── docs/
│   └── ticktick-openapi.md    ← TickTick Open API reference (verbatim)
└── memory/
    ├── MEMORY.md              ← index of persistent context
    └── *.md                   ← design decisions, feature requests, lineage
```

## Tech stack

- Python 3.12+ managed via `uv`
- `httpx` — TickTick API client
- `pydantic` — settings.yml schema validation
- stdlib `sqlite3` (WAL pragma + FK cascades)
- `ruamel.yaml` — settings.yml loading
- `pytest` + `pytest-httpx` + `hypothesis`

## Conventions

**Commits (from the user's global CLAUDE.md):**
- One logical change per commit. Refactor + behavior change = two commits.
- Plan splits *before* editing.
- GPG signing is enabled — never pass `--no-gpg-sign`.
- Push after committing.

**Tests:**
- Every new module gets a `tests/test_<module>.py`.
- `pytest`'s `tmp_path` for filesystem isolation.
- `pytest-httpx`'s `httpx_mock` for TickTick API mocks. **No live API
  calls in CI.** Use the recorded cassette in `tests/fixtures/` for
  realistic shapes.
- `monkeypatch.setenv("TICKTICK_CLI_HOME", str(tmp_path))` to isolate
  the config directory.
- For tests of `cmd_*` handlers, copy the pattern in
  `tests/test_cli_commands.py`: a `cli_env` fixture writes a faux
  OAuth token + sets `TICKTICK_CLIENT_ID/SECRET`, a `no_sync` fixture
  monkeypatches `Syncer.run` to a no-op so tests can pre-populate the
  mirror directly. Tests that need to verify a sync ran (e.g.
  finally-block tests) install a counting wrapper instead.

**Code:**
- Pure functions where possible. I/O at module boundaries.
- One responsibility per file. If it grows past ~200 lines, consider
  splitting.
- Type hints throughout. Pydantic for external-data schemas.
- Parameterized SQL only — TickTick project names and task IDs are
  user-controlled.

**Subcommands (`cli.py`):**
- Read subcommands print JSON. The caller (a script, another tool, a
  Claude session in some other directory) consumes them.
- Write subcommands re-run `sync` after the write so subsequent reads
  see the new state. Tag mutations also re-sync *before* the write to
  prevent stale-mirror overwrites — see the tag operations note below.
- Each subcommand is `cmd_<name>(args)` returning an exit code (0 OK).
- Add new subcommands: write the function, then add a
  `sub.add_parser(...)` block in `_build_parser()`.
- **Destructive or global ops are dry-run by default; `--apply` performs.**
  `delete`, `tag rename`, `tag delete` all follow this. Dry-run prints
  the planned action to stderr and makes zero API calls. Pattern fits
  the scripted-consumer model (CLAUDE.md says callers are scripts /
  other tools / Claude sessions); a wrong-target invocation is a
  question, not an outage. Apply this to any new destructive verb.

## Known quirks

**`delete` is dry-run by default.** Mirrors the `tag rename`/`tag delete`
pattern. The Open API doesn't document whether `DELETE
/open/v1/project/.../task/...` moves to Trash or hard-deletes, and
there's no flag to control it. Treat as irreversible from the CLI's
point of view; pass `--apply` to perform.

**TickTick has no "Won't Do" API.** The Open API only documents status
values `0` (Normal) and `2` (Completed) — see the Task schema in
`docs/ticktick-openapi.md`. Don't add an `update_task(status=...)`
path with guesswork values; if a caller needs the distinction, they
can tag the task and complete it (`tag add <id> wont-do && complete <id>`).

**TickTick does not issue refresh tokens.** Their `/oauth/token`
response gives only `access_token` + `expires_in` (~180 days).
Recovery when the token expires: re-run `setup`. Don't reintroduce
the assumption that `d["refresh_token"]` exists.

**Completed tasks come from two endpoints, not one.**
`/open/v1/project/{id}/data` returns only active (status=0) tasks —
historical completions are served exclusively by
`POST /open/v1/task/completed`. `Syncer.run()` calls both on every
sync: actives first, then completions in `[now − N days, now]` where
N is `sync.completions_lookback_days` (default 30). Completions whose
project is no longer in `list_projects()` are skipped to avoid FK
violations on the upsert. The window is configurable but not
unbounded — completions older than the lookback never enter the
mirror, and `recent` is bounded by the same window.

**Reminders are iCal TRIGGER strings, relative to the task's due
time.** Negative duration = before, zero = at, positive = after. Tasks
must have a `dueDate` for reminders to fire. See `format_trigger()`
and `parse_duration()` in `ticktick.py`.

**Sync sweep preserves completed tasks.** `sync.py` step 5 marks
status=0 tasks as archived when they disappear from cloud — but
NEVER touches status=2 (completed). Don't change this without
updating the candidate flow.

**Empty TickTick project list raises rather than archiving everything.**
A transient empty `list_projects()` response would, naively, cause
the sweep to archive every pending task. `sync.py` raises early if
the project list is empty, triggering ROLLBACK. Keep this guard.

## Testing locally

```bash
uv sync                                 # install/refresh deps
uv run pytest -v                        # full suite
uv run pytest tests/test_foo.py -v      # one file
uv run pytest -k remind                 # by name pattern
uv run ticktick-cli --help              # CLI smoke (after uv sync)
```

## Endpoints worth knowing about (in `docs/ticktick-openapi.md`)

Currently wrapped:
- `GET /open/v1/project`
- `GET /open/v1/project/{id}/data`
- `POST /open/v1/task` (create)
- `POST /open/v1/task/{taskId}` (update — used for reminders, repeat, tags)
- `POST /open/v1/task/move` (move task between projects)
- `POST /open/v1/task/completed` (list completed tasks for a date range — populates `recent`)
- `POST /open/v1/project/{projectId}/task/{taskId}/complete`
- `DELETE /open/v1/project/{projectId}/task/{taskId}`

Documented but not yet wrapped:

- (None currently — the `update_task` extension shipped on 2026-05-30
  unblocking `edit`/`punt`/`bump`. See `memory/feature_request_triage_edit_subcommand.md`
  for the historical request.)
- `GET /open/v1/project/{projectId}/task/{taskId}` — fetch a single
  task. Low priority; the mirror has most of what we'd need.
- `POST /open/v1/task/filter` — advanced filtering server-side.
  Closest the Open API gets to "smart lists" — but note it's ad-hoc
  filtering, NOT a way to enumerate the user's saved Smart Lists or
  named Filters; those have no API surface.
- Habit + Focus APIs — entirely separate domain; ignore unless asked.

## What NOT to do

- Don't add the Anthropic SDK as a dependency. This is a TickTick CLI;
  if a caller wants AI, they wrap us, not the other way around.
- Don't store secrets in the repo or in shell rc files. The pattern is
  `$XDG_CONFIG_HOME/ticktick-cli/secrets.env` (chmod 0600), loaded by
  `cli._load_secrets_file()`.
- Don't add subcommands that aren't TickTick-flavored. Anything
  AI-flavored (effort estimation, mode tagging, ranking, snoozing,
  reports) belongs in the caller's tooling, not here.
- Don't bypass `ticktick.py` and call the API directly from `cli.py`.
  Keep the HTTP boundary in one module.
- Don't change `_user_locked` semantics or any other concept that
  doesn't exist in this codebase anymore — refer only to what's
  actually here.
