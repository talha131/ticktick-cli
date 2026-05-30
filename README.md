# ticktick-cli

A command-line tool for [TickTick](https://ticktick.com/), built on
their [Open API](https://developer.ticktick.com/). Maintains a local
SQLite mirror of your projects and tasks for fast, scriptable access.

```
       ticktick-cli sync ──────► TickTick Open API
                                       │
                                       ▼
                        ~/.config/ticktick-cli/cache/tasks.db

       Read     candidates  recent
       Write    add  complete  delete  edit  punt  bump  remind  move  repeat  tag
                       (reads + mutates the API and mirror)
```

The output of every read subcommand is JSON, designed to be piped into
other tools — including AI assistants that want to reason about your
tasks in natural language.

## Setup

### macOS / Linux (~5 min)

1. **Clone and install**

   ```bash
   git clone <repo-url> ~/Developer/ticktick-cli
   cd ~/Developer/ticktick-cli
   uv sync
   ```

2. **Register a TickTick OAuth app** at https://developer.ticktick.com.
   Redirect URI: `http://localhost:8181/callback`.

3. **Save your credentials** to a local-only secrets file (NOT shell rc,
   which is often version-controlled):

   ```bash
   mkdir -p ~/.config/ticktick-cli
   cat > ~/.config/ticktick-cli/secrets.env <<'EOF'
   TICKTICK_CLIENT_ID=your-client-id
   TICKTICK_CLIENT_SECRET=your-client-secret
   EOF
   chmod 0600 ~/.config/ticktick-cli/secrets.env
   ```

   The CLI loads this file on every invocation. Shell env vars take
   precedence, so a one-off `TICKTICK_CLIENT_ID=foo uv run ...` works
   as override.

4. **One-time OAuth flow** (opens your browser):

   ```bash
   uv run ticktick-cli setup
   ```

   The access token lands in `~/.config/ticktick-cli/.ticktick-auth`
   (chmod 0600, per-machine). TickTick access tokens last ~180 days —
   re-run `setup` when it expires.

5. **First sync**:

   ```bash
   uv run ticktick-cli sync
   ```

6. **(Optional) Make the CLI globally available** so you don't need
   `uv run`:

   ```bash
   uv tool install .
   ticktick-cli --help
   ```

   This creates a snapshot install — `git pull` updates to the source
   won't be reflected until you re-run `uv tool install --force .`.
   If you're tracking the repo and want changes to flow through
   automatically, install it editable instead:

   ```bash
   uv tool install --force --editable .
   ```

### Windows (~5 min)

Run in **PowerShell**.

1. **Install Python 3.12+** from https://python.org or the Microsoft Store.

2. **Install uv**:

   ```powershell
   irm https://astral.sh/uv/install.ps1 | iex
   ```

3. **Clone and install**:

   ```powershell
   git clone <repo-url> "$HOME\Developer\ticktick-cli"
   cd "$HOME\Developer\ticktick-cli"
   uv sync
   ```

4. **Register a TickTick OAuth app** at https://developer.ticktick.com.
   Redirect URI: `http://localhost:8181/callback`.

5. **Save your credentials**:

   ```powershell
   $configDir = "$HOME\.config\ticktick-cli"
   New-Item -ItemType Directory -Force -Path $configDir | Out-Null
   @"
   TICKTICK_CLIENT_ID=your-client-id
   TICKTICK_CLIENT_SECRET=your-client-secret
   "@ | Set-Content -Path "$configDir\secrets.env" -Encoding ASCII
   ```

   On Windows the default config directory is
   `$HOME\.config\ticktick-cli\` (Path.home() resolves to
   `C:\Users\<you>`). If you'd rather use the Windows-native
   `%APPDATA%`, set XDG_CONFIG_HOME permanently:

   ```powershell
   setx XDG_CONFIG_HOME "$env:APPDATA"
   # ...then move the secrets.env you just created to %APPDATA%\ticktick-cli\.
   ```

6. **OAuth + first sync**:

   ```powershell
   uv run ticktick-cli setup
   uv run ticktick-cli sync
   ```

7. **(Optional) Install globally**:

   ```powershell
   uv tool install .
   ticktick-cli --help
   ```

   This is a snapshot install; re-run `uv tool install --force .` to
   pick up code changes. For live source updates as you `git pull`,
   use `uv tool install --force --editable .` instead.

## Subcommand reference

Run `ticktick-cli <subcommand> --help` for full options.

### Authentication & sync

| Subcommand | Purpose |
|---|---|
| `setup` | One-time OAuth flow via browser. Saves access token. Re-run when the ~180-day token expires. |
| `sync` | Pull TickTick projects + tasks into the local SQLite mirror. Wrapped in a transaction; partial failure rolls back. |

### Read

| Subcommand | Purpose |
|---|---|
| `candidates [--limit N]` | JSON of active tasks: `status=0`, project not archived, project not in `excluded_projects_by_name`, `start_date` ≤ now. Ordered by priority DESC, due-date ASC with NULLs last. Default limit 60. |
| `recent [--limit N]` | JSON of last N completed tasks, populated by `sync` via `POST /open/v1/task/completed`. The window is bounded by `sync.completions_lookback_days` (30 days by default); completions older than the lookback don't enter the mirror. Default limit 10. |

### Write — per-task, fires immediately

| Subcommand | Purpose |
|---|---|
| `add <title> --project <name>` | Create a task. `--project` accepts a name (case-insensitive) or a TickTick project id. Optional: `--content`, `--priority {0,1,3,5}`, `--due <ISO>`, `--remind <duration>` (repeatable), `--repeat <RRULE>`, `--tag <name>` (repeatable). Re-syncs after. |
| `complete <task_id>` | Mark complete via TickTick's API. Re-syncs. |
| `remind <task_id> [durations...] [--clear]` | Set reminders on an existing task. Replaces any existing reminders. |
| `edit <task_id> [--title T] [--content C] [--due W \| --clear-due] [--start W \| --clear-start] [--priority {none,low,medium,high}] [--dry-run]` | Edit fields on an existing task. At least one flag required. Date inputs accept ISO 8601, relative (`+7d`, `3h`), weekday names (`monday`), or `today`/`tomorrow` — see [`dates.py`](src/ticktick_cli/dates.py) for the full grammar. Priority accepts names or numeric (0/1/3/5). `--dry-run` prints the PATCH body as JSON and exits without calling the API or re-syncing. Re-syncs after a real write. |
| `punt <task_id> <when> [--dry-run]` | Sugar over `edit --start <when>`. Sets the start date so the task disappears from default views until then. Doesn't touch the due date. `--dry-run` previews the PATCH body. |
| `bump <task_id> {none,low,medium,high} [--dry-run]` | Sugar over `edit --priority`. Sets task priority by name (no numeric form — name only for triage clarity). `--dry-run` previews the PATCH body. |
| `move <task_id> --to <project>` | Move a task to a different project. `--to` accepts a name (case-insensitive) or project id. Errors if the task is already in that project. Re-syncs. |
| `repeat <task_id> [RRULE] [--clear]` | Set or clear an iCal RRULE recurrence on a task. Pass through verbatim — see RFC 5545 for syntax. |
| `tag add <task_id> <tag>...` | Add one or more tags to a task. Merges with existing tags (auto-pre-syncs to avoid losing tags added on another device); duplicates are skipped. |
| `tag remove <task_id> <tag>... [--ignore-case]` | Remove one or more tags from a task. Same pre-sync as `tag add`. No-op if the task didn't carry any of them. |

### Write — destructive / global, dry-run by default

These commands print what they *would* do and exit without touching
the API. Pass `--apply` to actually perform the operation.

| Subcommand | Purpose |
|---|---|
| `delete <task_id> [--apply]` | Delete a task. TickTick's API doesn't expose trash vs hard-delete behavior; treat as irreversible. |
| `tag rename <old> <new> [--apply] [--ignore-case]` | Rename a tag across the local mirror. Auto-pre-syncs to avoid stale-data overwrites. Not a true global rename — excluded projects and historical completions are silently missed. |
| `tag delete <tag> [--apply] [--ignore-case]` | Remove a tag across the local mirror. Same auto-pre-sync + scope caveat as `tag rename`. |

### Reminder durations

| Form | Meaning | iCal TRIGGER produced |
|---|---|---|
| `15m` | 15 minutes before due | `TRIGGER:-PT15M` |
| `1h` | 1 hour before due | `TRIGGER:-PT60M` |
| `2d` | 2 days before due | `TRIGGER:-PT2880M` |
| `at-due` | at the due time | `TRIGGER:PT0S` |
| `30` | 30 minutes before (bare integer) | `TRIGGER:-PT30M` |

Reminders fire through TickTick's existing push infrastructure (the
same notifications you already get from the TickTick mobile and
desktop apps). **Tasks without a `dueDate` cannot have reminders** —
TickTick anchors all reminder triggers to the due time.

## Examples

```bash
# Add a task with a reminder 15 min before the due time:
ticktick-cli add "Call mom" --project Personal \
       --due "2026-05-30T15:00:00+0000" --remind 15m

# Add a task with multiple reminders:
ticktick-cli add "Submit grant proposal" --project Research \
       --priority 5 --due "2026-06-15T17:00:00+0000" \
       --remind 1d --remind 1h --remind 15m

# Set reminders on an existing task (replaces existing reminders):
ticktick-cli remind 6549abcdef0123456789 30m 1h

# Clear all reminders:
ticktick-cli remind 6549abcdef0123456789 --clear

# Move a task to a different project:
ticktick-cli move 6549abcdef0123456789 --to Personal

# Create a recurring task:
ticktick-cli add "Daily standup" --project Work \
       --due "2026-06-01T09:00:00+0000" \
       --repeat "RRULE:FREQ=DAILY;INTERVAL=1"

# Change an existing task's recurrence:
ticktick-cli repeat 6549abcdef0123456789 "RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR"

# Remove recurrence (make it a one-shot task):
ticktick-cli repeat 6549abcdef0123456789 --clear

# Push a task's due date out 7 days from now:
ticktick-cli edit 6549abcdef0123456789 --due +7d

# Raise priority via edit:
ticktick-cli edit 6549abcdef0123456789 --priority high

# Rename and clear the due date:
ticktick-cli edit 6549abcdef0123456789 --title "New title" --clear-due

# Set the start date to next Monday (00:00 local):
ticktick-cli edit 6549abcdef0123456789 --start monday

# Hide a task for a week (start date in the future):
ticktick-cli punt 6549abcdef0123456789 7d

# Punt until next Monday:
ticktick-cli punt 6549abcdef0123456789 monday

# Bump priority to high:
ticktick-cli bump 6549abcdef0123456789 high

# Drop priority back to none:
ticktick-cli bump 6549abcdef0123456789 none

# Create a tagged task:
ticktick-cli add "Buy milk" --project Personal --tag errand --tag shopping

# Add/remove tags on an existing task:
ticktick-cli tag add 6549abcdef0123456789 urgent waiting
ticktick-cli tag remove 6549abcdef0123456789 waiting

# Rename a tag across the local mirror (dry-run, then apply):
# Auto-pre-syncs; excluded projects and historical completions are still missed.
ticktick-cli tag rename old-name new-name           # prints affected tasks
ticktick-cli tag rename old-name new-name --apply   # performs the rename

# Delete a tag across the local mirror (dry-run, then apply):
ticktick-cli tag delete obsolete-tag                # prints affected tasks
ticktick-cli tag delete obsolete-tag --apply        # performs the removal

# Delete a task (dry-run, then apply):
ticktick-cli delete 6549abcdef0123456789            # prints task title
ticktick-cli delete 6549abcdef0123456789 --apply    # performs the deletion

# Read paths:
ticktick-cli sync
ticktick-cli candidates --limit 30
ticktick-cli complete 6549abcdef0123456789
```

## Configuration

`ticktick-cli` reads optional settings from
`<config>/settings.yml` (where `<config>` is described below). All
fields are optional; the defaults below apply when a field is missing.

```yaml
sync:
  # How long the local SQLite mirror is considered fresh before a
  # read triggers an inline re-sync.
  ttl_minutes: 5

  # Lookback window for the completed-tasks fetch during sync. Every
  # `sync` pulls completions in [now - N days, now] via
  # POST /open/v1/task/completed, which is what populates `recent`.
  # Widen this if you want deeper history in `recent`; set to 0 to
  # skip the call entirely.
  completions_lookback_days: 30

filters:
  # Project names (case-insensitive) whose tasks should NOT appear in
  # `candidates`. Resolved to TickTick project IDs at every sync;
  # renames in TickTick log a warning rather than silently
  # re-including the project.
  excluded_projects_by_name:
    - Someday
    - Archive

database:
  # Path to the SQLite mirror. When unset (the default), the CLI
  # uses `<config>/cache/tasks.db`. Override to relocate the mirror
  # (e.g. inside a directory you sync with git, or onto another
  # volume). `~` is expanded.
  path: ~/.config/ticktick-cli/cache/tasks.db
```

### Where everything lives

The CLI keeps all of its state under one directory, **`<config>`**,
resolved with this precedence:

| Precedence | Value | Resolves to (default) |
|---|---|---|
| 1 | `TICKTICK_CLI_HOME` env var | as-set |
| 2 | `$XDG_CONFIG_HOME/ticktick-cli` (if `XDG_CONFIG_HOME` is set) | follows XDG |
| 3 | `~/.config/ticktick-cli` | Linux/macOS default; also works on Windows because `Path.home()` resolves to `C:\Users\<you>` |

That directory contains:

```
<config>/
├── secrets.env              # TickTick OAuth creds, chmod 0600 (you create)
├── settings.yml             # optional, user-edited
├── .ticktick-auth           # OAuth access token, chmod 0600 (setup writes)
└── cache/tasks.db           # SQLite mirror (sync writes, rebuildable)
```

**None of these files belong in version control** — they're either
credentials, machine-specific tokens, or a cache that regenerates
from the TickTick API.

### Environment variables at a glance

| Variable | Effect |
|---|---|
| `TICKTICK_CLIENT_ID`, `TICKTICK_CLIENT_SECRET` | OAuth credentials. Read from `secrets.env`; shell env wins. |
| `TICKTICK_CLI_HOME` | Override the config + state directory. |
| `XDG_CONFIG_HOME` | Standard XDG override; affects `TICKTICK_CLI_HOME` default. |

## TickTick API reference

The full TickTick Open API documentation (endpoints, request and
response schemas, the Task object's field list) is included in this
repo at [`docs/ticktick-openapi.md`](docs/ticktick-openapi.md). It's
the canonical source for anything not covered in this README.

## Development

```bash
uv sync
uv run pytest -v
```

Tests use `pytest-httpx` for TickTick API mocking; no live API calls
in CI.

## License

[MIT](LICENSE)
