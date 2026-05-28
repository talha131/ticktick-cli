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

## Subcommand surface

| Command | Purpose | Endpoint |
|---|---|---|
| `setup` | One-time OAuth via browser callback | `POST /oauth/token` |
| `sync` | Pull TickTick projects + tasks into local SQLite | `GET /open/v1/project`, `GET /open/v1/project/{id}/data` |
| `candidates [--limit N]` | JSON of active tasks from local mirror | local SQLite query |
| `recent [--limit N]` | JSON of recently completed tasks | local SQLite (currently empty — see Known quirks) |
| `add <title> --project P [--due ...] [--remind ...]` | Create a task | `POST /open/v1/task` |
| `complete <task_id>` | Mark task complete | `POST /open/v1/project/{p}/task/{t}/complete` |
| `remind <task_id> [durations...] [--clear]` | Set reminders | `POST /open/v1/task/{taskId}` |
| `move <task_id> --to <project>` | Move task to another project | `POST /open/v1/task/move` |

Reminder duration syntax: `15m`, `1h`, `2d`, `at-due`, bare integer
(minutes). All translate to iCal TRIGGER strings sent to TickTick.

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
└── docs/
    └── ticktick-openapi.md    ← TickTick Open API reference (verbatim)
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
  see the new state.
- Each subcommand is `cmd_<name>(args)` returning an exit code (0 OK).
- Add new subcommands: write the function, then add a
  `sub.add_parser(...)` block in `_build_parser()`.

## Known quirks

**TickTick does not issue refresh tokens.** Their `/oauth/token`
response gives only `access_token` + `expires_in` (~180 days).
Recovery when the token expires: re-run `setup`. Don't reintroduce
the assumption that `d["refresh_token"]` exists.

**TickTick's `/open/v1/project/{id}/data` returns only active tasks.**
The mirror's `tasks WHERE status=2` table is empty after a normal
sync, and `recent` returns `[]`. The fix exists in the API but isn't
implemented yet: **`POST /open/v1/task/completed`** (documented in
`docs/ticktick-openapi.md` under "List Completed Tasks") returns
completed tasks for a date range. Wire this in when `recent` becomes
genuinely useful to you.

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
- `POST /open/v1/task/{taskId}` (update — used for reminders)
- `POST /open/v1/task/move` (move task between projects)
- `POST /open/v1/project/{projectId}/task/{taskId}/complete`

Documented but not yet wrapped (good follow-ups):
- `GET /open/v1/project/{projectId}/task/{taskId}` — fetch a single task
- `POST /open/v1/task/completed` — list completed tasks by date range
  (fixes the empty `recent` issue)
- `POST /open/v1/task/filter` — advanced filtering server-side
- `DELETE /open/v1/project/{projectId}/task/{taskId}` — delete a task
- Habit + Focus APIs — entirely separate domain; ignore unless asked

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
