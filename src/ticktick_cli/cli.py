"""CLI entry point.

A thin wrapper around TickTick's Open API plus a local SQLite mirror.

Subcommands: setup, sync, candidates, recent, add, complete, delete, remind,
move, repeat, tag.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from .auth import TickTickAuth, TokenStore
from .candidates import list_candidates
from .config import load_settings
from .dates import parse_when
from .store import Store
from .sync import Syncer
from .tags import find_tasks_with_tag, get_task_tags
from .ticktick import TickTickClient, format_trigger, parse_duration


def _home() -> Path:
    """Resolve the app's config + state directory.

    Holds everything ticktick-cli needs to persist: secrets.env (creds),
    .ticktick-auth (OAuth tokens), settings.yml (preferences), and
    cache/tasks.db (SQLite mirror).

    Precedence:
      1. TICKTICK_CLI_HOME              (explicit override; tests use this)
      2. $XDG_CONFIG_HOME/ticktick-cli  (XDG Base Directory Spec)
      3. ~/.config/ticktick-cli         (XDG default location; also
                                         works on Windows where
                                         Path.home() → C:\\Users\\<user>)"""
    explicit = os.environ.get("TICKTICK_CLI_HOME")
    if explicit:
        return Path(explicit)
    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg_config) if xdg_config else Path.home() / ".config"
    return base / "ticktick-cli"


def _load_secrets_file() -> None:
    """Load secrets from $XDG_CONFIG_HOME/ticktick-cli/secrets.env into
    os.environ.

    Format: one KEY=VALUE per line. Comments (#…) and blank lines are skipped.
    Values may be optionally quoted. Existing environment variables take
    precedence (so a one-off shell override still works). The file is
    deliberately outside the repo so it never reaches a versioned shell rc."""
    secrets_path = _home() / "secrets.env"
    if not secrets_path.exists():
        return
    for raw_line in secrets_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, val = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key:
            continue
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


def _require_secrets() -> tuple[str, str]:
    cid = os.environ.get("TICKTICK_CLIENT_ID")
    csec = os.environ.get("TICKTICK_CLIENT_SECRET")
    if not cid or not csec:
        secrets_path = _home() / "secrets.env"
        sys.stderr.write(
            "TICKTICK_CLIENT_ID and TICKTICK_CLIENT_SECRET must be set.\n\n"
            f"Recommended: create {secrets_path} with:\n"
            "  TICKTICK_CLIENT_ID=your-client-id\n"
            "  TICKTICK_CLIENT_SECRET=your-client-secret\n"
            f"and `chmod 0600 {secrets_path}`.\n\n"
            "Register your TickTick OAuth app at "
            "https://developer.ticktick.com first.\n"
        )
        sys.exit(2)
    return cid, csec


def _load_settings_from_home():
    return load_settings(_home() / "settings.yml")


def _resolve_db_path(settings) -> Path:
    """Resolve the SQLite mirror path from settings, expanding `~`.

    If the setting is unset (None), default to `<_home()>/cache/tasks.db`
    — this keeps the cache co-located with the rest of the local-only
    state regardless of platform or XDG configuration."""
    explicit = settings.database.path
    if explicit is None:
        return _home() / "cache" / "tasks.db"
    return Path(explicit).expanduser()


def _open_store(settings=None) -> Store:
    """Open (and migrate) the SQLite mirror. Settings is loaded if not passed."""
    if settings is None:
        settings = _load_settings_from_home()
    s = Store(_resolve_db_path(settings))
    s.init_schema()
    return s


def _build_client() -> TickTickClient:
    home = _home()
    cid, csec = _require_secrets()
    auth = TickTickAuth(
        token_store=TokenStore(home / ".ticktick-auth"),
        client_id=cid, client_secret=csec,
    )
    return TickTickClient(auth=auth)


# ---- subcommands -----------------------------------------------------------


def cmd_setup(_args: argparse.Namespace) -> int:
    home = _home()
    cid, csec = _require_secrets()
    auth = TickTickAuth(
        token_store=TokenStore(home / ".ticktick-auth"),
        client_id=cid, client_secret=csec,
    )
    tok = auth.run_initial_auth_flow()
    print(f"OAuth complete; refresh token saved to {home / '.ticktick-auth'}")
    print(f"Access token expires at: {tok.expires_at}")
    return 0


def cmd_sync(_args: argparse.Namespace) -> int:
    settings = _load_settings_from_home()
    store = _open_store(settings)
    client = _build_client()
    Syncer(
        store=store,
        client=client,
        excluded_names=settings.filters.excluded_projects_by_name,
        completions_lookback_days=settings.sync.completions_lookback_days,
    ).run()
    row = store.conn.execute(
        "SELECT v FROM sync_state WHERE k='last_full_sync'"
    ).fetchone()
    print(f"Sync complete. last_full_sync={row['v'] if row else 'unknown'}")
    return 0


def cmd_candidates(args: argparse.Namespace) -> int:
    """Print filtered candidate tasks as JSON for Claude (or human) to read."""
    store = _open_store()
    excluded_row = store.conn.execute(
        "SELECT v FROM sync_state WHERE k='excluded_project_ids'"
    ).fetchone()
    excluded_ids = json.loads(excluded_row["v"]) if excluded_row else []
    now_iso = datetime.now().isoformat()
    rows = list_candidates(store, excluded_ids, now_iso, limit=args.limit)

    # Join with project names for readability.
    proj_names = {
        r["id"]: r["name"]
        for r in store.conn.execute("SELECT id, name FROM projects")
    }
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "title": r["title"],
            "project": proj_names.get(r["project_id"], r["project_id"]),
            "priority": r["priority"],
            "due_date": r["due_date"],
            "start_date": r["start_date"],
            "tags": json.loads(r["tags"]) if r["tags"] else [],
            "repeat": r["repeat_flag"],
        })
    print(json.dumps(out, indent=2))
    return 0


def cmd_recent(args: argparse.Namespace) -> int:
    """Print last N completed tasks (for context on what the user just finished)."""
    store = _open_store()
    rows = store.conn.execute(
        "SELECT t.id, t.title, t.project_id, t.completed_at, p.name AS project_name "
        "FROM tasks t LEFT JOIN projects p ON t.project_id = p.id "
        "WHERE t.status = 2 AND t.completed_at IS NOT NULL "
        "ORDER BY t.completed_at DESC LIMIT ?",
        (args.limit,),
    )
    out = [
        {
            "id": r["id"],
            "title": r["title"],
            "project": r["project_name"] or r["project_id"],
            "completed_at": r["completed_at"],
        }
        for r in rows
    ]
    print(json.dumps(out, indent=2))
    return 0


_PRIORITY_NAMES = {"none": 0, "low": 1, "medium": 3, "high": 5}


def _parse_priority(s: str) -> int:
    """Accept either a name (`high`/`medium`/`low`/`none`) or a numeric
    TickTick priority (`0`/`1`/`3`/`5`). Used as argparse `type=`."""
    s = s.strip().lower()
    if s in _PRIORITY_NAMES:
        return _PRIORITY_NAMES[s]
    try:
        n = int(s)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"priority must be one of {sorted(_PRIORITY_NAMES)} or 0/1/3/5"
        )
    if n not in (0, 1, 3, 5):
        raise argparse.ArgumentTypeError(
            f"numeric priority must be 0/1/3/5 (got {n})"
        )
    return n


def _resolve_project_id(store: Store, name_or_id: str) -> str:
    """Accept either a project id (returned verbatim if present in DB) or a
    case-insensitive project name match. Errors out if neither matches."""
    row = store.conn.execute(
        "SELECT id FROM projects WHERE id = ?", (name_or_id,)
    ).fetchone()
    if row:
        return row["id"]
    row = store.conn.execute(
        "SELECT id FROM projects WHERE LOWER(name) = LOWER(?)", (name_or_id,)
    ).fetchone()
    if row:
        return row["id"]
    sys.stderr.write(
        f"No project matches {name_or_id!r}. Run `sync` first, or check "
        f"`candidates` for the exact project names you have.\n"
    )
    sys.exit(2)


def _lookup_project_id(store: Store, task_id: str) -> str:
    """Return the project_id for a given task. Exits 2 if not in cache."""
    row = store.conn.execute(
        "SELECT project_id FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if not row:
        sys.stderr.write(
            f"Task {task_id!r} not in local cache. Run `sync` first if you "
            f"just created it on another device.\n"
        )
        sys.exit(2)
    return row["project_id"]


def _reminders_from_args(remind_specs: list[str]) -> list[str]:
    """Turn `--remind 15m --remind 1h` into iCal TRIGGER strings."""
    triggers = []
    for spec in remind_specs:
        minutes = parse_duration(spec)
        triggers.append(format_trigger(minutes))
    return triggers


def cmd_add(args: argparse.Namespace) -> int:
    """Create a task in TickTick and refresh the local mirror."""
    settings = _load_settings_from_home()
    store = _open_store(settings)
    project_id = _resolve_project_id(store, args.project)
    client = _build_client()
    reminders = _reminders_from_args(args.remind) if args.remind else None
    created = client.create_task(
        project_id=project_id,
        title=args.title,
        content=args.content,
        priority=args.priority,
        due_date=args.due,
        reminders=reminders,
        repeat_flag=args.repeat,
        tags=args.tag if args.tag else None,
    )
    # Refresh mirror so the new task is visible to `candidates` immediately.
    Syncer(store=store, client=client,
           excluded_names=settings.filters.excluded_projects_by_name,
           completions_lookback_days=settings.sync.completions_lookback_days).run()
    print(json.dumps({"id": created.get("id"), "title": created.get("title"),
                      "project_id": project_id,
                      "reminders": created.get("reminders", []),
                      "repeat": created.get("repeatFlag"),
                      "tags": created.get("tags", [])}, indent=2))
    return 0


def cmd_remind(args: argparse.Namespace) -> int:
    """Set reminders on an existing TickTick task.

    Each duration argument becomes an iCal TRIGGER. Passing --clear with
    no durations removes all reminders. Passing durations REPLACES the
    existing reminder list (TickTick's update endpoint has replace
    semantics for arrays)."""
    settings = _load_settings_from_home()
    store = _open_store(settings)
    project_id = _lookup_project_id(store, args.task_id)
    client = _build_client()

    if args.clear:
        triggers: list[str] = []
    else:
        if not args.durations:
            sys.stderr.write(
                "Pass one or more durations (e.g. '15m 1h') or --clear "
                "to remove existing reminders.\n"
            )
            sys.exit(2)
        triggers = _reminders_from_args(args.durations)

    updated = client.update_task(
        args.task_id, project_id=project_id, reminders=triggers
    )
    # Re-sync so the mirror reflects the new reminder array.
    Syncer(store=store, client=client,
           excluded_names=settings.filters.excluded_projects_by_name,
           completions_lookback_days=settings.sync.completions_lookback_days).run()
    print(json.dumps({"id": args.task_id,
                      "reminders": updated.get("reminders", triggers)},
                     indent=2))
    return 0


def cmd_edit(args: argparse.Namespace) -> int:
    """Edit one or more mutable fields on an existing task.

    Flags are mutually optional but at least one must be present.
    `--clear-due` / `--clear-start` send empty-string to TickTick;
    they cannot combine with `--due` / `--start` respectively.

    Date inputs are parsed by `dates.parse_when` which accepts ISO
    8601 verbatim, relative durations (`+7d`, `3h`), weekday names,
    and `today`/`tomorrow`. See dates.py for the full grammar."""
    if args.due and args.clear_due:
        sys.stderr.write("Pass either --due or --clear-due, not both.\n")
        return 2
    if args.start and args.clear_start:
        sys.stderr.write("Pass either --start or --clear-start, not both.\n")
        return 2

    title = args.title
    content = args.content
    due_date = "" if args.clear_due else (
        parse_when(args.due) if args.due else None
    )
    start_date = "" if args.clear_start else (
        parse_when(args.start) if args.start else None
    )
    priority = args.priority

    if all(v is None for v in (title, content, due_date, start_date, priority)):
        sys.stderr.write(
            "Pass at least one of --title, --content, --due/--clear-due, "
            "--start/--clear-start, --priority.\n"
        )
        return 2

    settings = _load_settings_from_home()
    store = _open_store(settings)
    project_id = _lookup_project_id(store, args.task_id)
    client = _build_client()
    updated = client.update_task(
        args.task_id,
        project_id=project_id,
        title=title,
        content=content,
        due_date=due_date,
        start_date=start_date,
        priority=priority,
    )
    Syncer(store=store, client=client,
           excluded_names=settings.filters.excluded_projects_by_name,
           completions_lookback_days=settings.sync.completions_lookback_days).run()
    print(json.dumps({
        "id": args.task_id,
        "title": updated.get("title"),
        "due_date": updated.get("dueDate"),
        "start_date": updated.get("startDate"),
        "priority": updated.get("priority"),
    }, indent=2))
    return 0


def cmd_punt(args: argparse.Namespace) -> int:
    """Sugar over `edit --start WHEN`.

    Sets the task's start date so it disappears from default views
    until that date. Deliberately does NOT touch dueDate — the intent
    is "hide for now", not "miss the deadline".

    The WHEN argument is the same grammar as `edit --start` — see
    dates.parse_when."""
    try:
        start_iso = parse_when(args.duration)
    except ValueError as e:
        sys.stderr.write(f"{e}\n")
        return 2

    settings = _load_settings_from_home()
    store = _open_store(settings)
    project_id = _lookup_project_id(store, args.task_id)
    client = _build_client()
    client.update_task(
        args.task_id, project_id=project_id, start_date=start_iso,
    )
    Syncer(store=store, client=client,
           excluded_names=settings.filters.excluded_projects_by_name,
           completions_lookback_days=settings.sync.completions_lookback_days).run()
    print(json.dumps({"id": args.task_id, "start_date": start_iso}, indent=2))
    return 0


def cmd_move(args: argparse.Namespace) -> int:
    """Move a task to a different project via TickTick API and re-sync."""
    settings = _load_settings_from_home()
    store = _open_store(settings)
    from_project_id = _lookup_project_id(store, args.task_id)
    to_project_id = _resolve_project_id(store, args.to)
    if from_project_id == to_project_id:
        sys.stderr.write(
            f"Task {args.task_id} is already in project {args.to!r}; "
            f"nothing to do.\n"
        )
        return 2
    client = _build_client()
    client.move_task(
        args.task_id,
        from_project_id=from_project_id,
        to_project_id=to_project_id,
    )
    Syncer(store=store, client=client,
           excluded_names=settings.filters.excluded_projects_by_name,
           completions_lookback_days=settings.sync.completions_lookback_days).run()
    print(json.dumps({"id": args.task_id,
                      "from_project_id": from_project_id,
                      "to_project_id": to_project_id}, indent=2))
    return 0


def cmd_repeat(args: argparse.Namespace) -> int:
    """Set or clear an iCal RRULE recurrence on an existing task.

    Pass an RRULE string (e.g. 'RRULE:FREQ=DAILY;INTERVAL=1') to set
    recurrence, or --clear to remove it. The rule is passed through to
    TickTick verbatim — see RFC 5545 for the full syntax."""
    settings = _load_settings_from_home()
    store = _open_store(settings)
    project_id = _lookup_project_id(store, args.task_id)
    client = _build_client()

    if args.clear:
        if args.rrule:
            sys.stderr.write("Pass either an RRULE or --clear, not both.\n")
            return 2
        repeat_flag = ""
    else:
        if not args.rrule:
            sys.stderr.write(
                "Pass an RRULE (e.g. 'RRULE:FREQ=DAILY;INTERVAL=1') or "
                "--clear to remove the existing recurrence.\n"
            )
            return 2
        repeat_flag = args.rrule

    updated = client.update_task(
        args.task_id, project_id=project_id, repeat_flag=repeat_flag
    )
    Syncer(store=store, client=client,
           excluded_names=settings.filters.excluded_projects_by_name,
           completions_lookback_days=settings.sync.completions_lookback_days).run()
    print(json.dumps({"id": args.task_id,
                      "repeat": updated.get("repeatFlag", repeat_flag)},
                     indent=2))
    return 0


def _merge_tags(existing: list[str], new: list[str]) -> list[str]:
    """Union preserving order: existing first, then unseen newcomers."""
    seen = set(existing)
    out = list(existing)
    for t in new:
        if t not in seen:
            out.append(t)
            seen.add(t)
    return out


def _resync_mirror(store: Store, settings, client: TickTickClient) -> None:
    """Run a full Syncer.run() with the project's exclusion settings.

    Pulled out because tag mutations need to sync both before (to avoid
    overwriting tags added elsewhere) and after (to reflect our write).
    Keep this trivial — anything more elaborate belongs in sync.py."""
    Syncer(
        store=store, client=client,
        excluded_names=settings.filters.excluded_projects_by_name,
        completions_lookback_days=settings.sync.completions_lookback_days,
    ).run()


def cmd_tag_add(args: argparse.Namespace) -> int:
    """Add one or more tags to a task, merging with the task's existing tags.

    Syncs the mirror first so we don't overwrite tags added on another
    device since the last sync — the API's `update_task` replaces the
    full tag list, so a stale read here would silently drop newer tags."""
    settings = _load_settings_from_home()
    store = _open_store(settings)
    project_id = _lookup_project_id(store, args.task_id)
    client = _build_client()
    _resync_mirror(store, settings, client)
    current = get_task_tags(store, args.task_id)
    new_tags = _merge_tags(current, args.tag)
    if new_tags == current:
        # All requested tags were already present — no API call needed.
        print(json.dumps({"id": args.task_id, "tags": current,
                          "unchanged": True}, indent=2))
        return 0
    client.update_task(args.task_id, project_id=project_id, tags=new_tags)
    _resync_mirror(store, settings, client)
    print(json.dumps({"id": args.task_id, "tags": new_tags}, indent=2))
    return 0


def cmd_tag_remove(args: argparse.Namespace) -> int:
    """Remove one or more tags from a task. No-op (exit 0) if none of the
    requested tags were on the task.

    Same pre-sync as cmd_tag_add: prevents the read-modify-write from
    silently dropping tags added on another device."""
    settings = _load_settings_from_home()
    store = _open_store(settings)
    project_id = _lookup_project_id(store, args.task_id)
    client = _build_client()
    _resync_mirror(store, settings, client)
    current = get_task_tags(store, args.task_id)
    if args.ignore_case:
        targets = {t.casefold() for t in args.tag}
        new_tags = [t for t in current if t.casefold() not in targets]
    else:
        targets = set(args.tag)
        new_tags = [t for t in current if t not in targets]
    if new_tags == current:
        print(json.dumps({"id": args.task_id, "tags": current,
                          "unchanged": True}, indent=2))
        return 0
    client.update_task(args.task_id, project_id=project_id, tags=new_tags)
    _resync_mirror(store, settings, client)
    print(json.dumps({"id": args.task_id, "tags": new_tags}, indent=2))
    return 0


def _dedup_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        if x not in seen:
            out.append(x)
            seen.add(x)
    return out


def cmd_tag_rename(args: argparse.Namespace) -> int:
    """Rename a tag across every task in the local mirror that carries it.

    Dry-run by default — prints the affected tasks and exits 0 without
    touching anything. Pass --apply to actually perform the rename.

    Scope is the local SQLite mirror, NOT a true global rename. The
    command pre-syncs before reading the mirror, so plain staleness is
    handled automatically — but excluded_projects_by_name (a read-side
    filter applied during sync) and historical completions (which
    /project/{id}/data never returns) are permanently invisible.

    Sweep is N independent HTTP calls. On mid-loop failure, the tasks
    iterated so far are already mutated on TickTick; the local mirror is
    re-synced in a finally block so subsequent reads see the partial
    state. The exception still propagates — the caller should treat any
    raise from this command as "partial application possible."""
    if args.old == args.new:
        sys.stderr.write("old and new tag names are identical; nothing to do.\n")
        return 2
    settings = _load_settings_from_home()
    store = _open_store(settings)
    client = _build_client()
    # Sync first so both the dry-run preview and the actual sweep operate
    # on the freshest view of the mirror. Without this, a tag could be
    # renamed off of (or onto) tasks the user wasn't expecting.
    _resync_mirror(store, settings, client)
    affected = find_tasks_with_tag(store, args.old, ignore_case=args.ignore_case)
    if not affected:
        sys.stderr.write(f"No tasks carry tag {args.old!r}.\n")
        return 0
    if not args.apply:
        sys.stderr.write(
            f"Would rename {args.old!r} → {args.new!r} on "
            f"{len(affected)} task(s):\n"
        )
        for t in affected:
            sys.stderr.write(f"  {t['id']}  {t['title']!r}\n")
        sys.stderr.write("\nRe-run with --apply to perform the rename.\n")
        return 0
    updated_ids: list[str] = []
    # Sweep is N independent HTTP calls — there's no server-side
    # transaction available. If one fails mid-loop, earlier tasks have
    # already been mutated on TickTick; the `finally` block ensures the
    # local mirror still reflects whatever partial state the server now
    # holds, so subsequent reads aren't lying about it.
    try:
        for t in affected:
            if args.ignore_case:
                target = args.old.casefold()
                new_tags = [args.new if x.casefold() == target else x for x in t["tags"]]
            else:
                new_tags = [args.new if x == args.old else x for x in t["tags"]]
            # If the new name already coexisted with the old one on this task,
            # the substitution produces a duplicate — collapse it.
            new_tags = _dedup_preserve_order(new_tags)
            client.update_task(t["id"], project_id=t["project_id"], tags=new_tags)
            updated_ids.append(t["id"])
    finally:
        _resync_mirror(store, settings, client)
    print(json.dumps({"renamed_from": args.old, "renamed_to": args.new,
                      "updated_tasks": updated_ids}, indent=2))
    return 0


def cmd_tag_delete(args: argparse.Namespace) -> int:
    """Remove a tag from every task in the local mirror that carries it.

    Same dry-run / --apply discipline as `tag rename`. Scope is the local
    mirror — see cmd_tag_rename for the full list of what that misses
    (excluded projects, unsynced tasks, historical completions). Same
    partial-application semantics: mid-loop failure leaves earlier tasks
    mutated and re-syncs the mirror before raising."""
    settings = _load_settings_from_home()
    store = _open_store(settings)
    client = _build_client()
    # See cmd_tag_rename for why we sync before reading from the mirror.
    _resync_mirror(store, settings, client)
    affected = find_tasks_with_tag(store, args.tag, ignore_case=args.ignore_case)
    if not affected:
        sys.stderr.write(f"No tasks carry tag {args.tag!r}.\n")
        return 0
    if not args.apply:
        sys.stderr.write(
            f"Would remove {args.tag!r} from {len(affected)} task(s):\n"
        )
        for t in affected:
            sys.stderr.write(f"  {t['id']}  {t['title']!r}\n")
        sys.stderr.write("\nRe-run with --apply to perform the removal.\n")
        return 0
    updated_ids: list[str] = []
    # See cmd_tag_rename — sweep is non-atomic, finally-block guarantees
    # the mirror gets re-synced even if a mid-loop update_task raises.
    try:
        for t in affected:
            if args.ignore_case:
                target = args.tag.casefold()
                new_tags = [x for x in t["tags"] if x.casefold() != target]
            else:
                new_tags = [x for x in t["tags"] if x != args.tag]
            client.update_task(t["id"], project_id=t["project_id"], tags=new_tags)
            updated_ids.append(t["id"])
    finally:
        _resync_mirror(store, settings, client)
    print(json.dumps({"deleted_tag": args.tag,
                      "updated_tasks": updated_ids}, indent=2))
    return 0


def cmd_complete(args: argparse.Namespace) -> int:
    """Mark a task complete via TickTick API and re-sync."""
    settings = _load_settings_from_home()
    store = _open_store(settings)
    project_id = _lookup_project_id(store, args.task_id)
    client = _build_client()
    client.complete_task(project_id, args.task_id)
    Syncer(store=store, client=client,
           excluded_names=settings.filters.excluded_projects_by_name,
           completions_lookback_days=settings.sync.completions_lookback_days).run()
    print(f"Completed {args.task_id}")
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    """Delete a task via TickTick API.

    Dry-run by default: reads the task's project and title from the local
    mirror, prints a one-line preview to stderr, and exits 0 without
    contacting the API. Pass --apply to actually delete.

    TickTick most likely moves API-deleted tasks into its Trash folder
    (30-day retention in the UI), but the Open API docs don't guarantee
    soft-delete semantics — treat as irreversible from this CLI's POV."""
    settings = _load_settings_from_home()
    store = _open_store(settings)
    project_id = _lookup_project_id(store, args.task_id)
    row = store.conn.execute(
        "SELECT title FROM tasks WHERE id = ?", (args.task_id,)
    ).fetchone()
    title = row["title"] if row else "<unknown>"
    if not args.apply:
        sys.stderr.write(
            f"Would delete {args.task_id} ({title!r}) from project {project_id}.\n"
            "Re-run with --apply to perform the deletion.\n"
            "Note: trash/hard-delete behavior is TickTick's call; the "
            "Open API has no flag for it.\n"
        )
        return 0
    client = _build_client()
    client.delete_task(project_id, args.task_id)
    Syncer(store=store, client=client,
           excluded_names=settings.filters.excluded_projects_by_name,
           completions_lookback_days=settings.sync.completions_lookback_days).run()
    print(json.dumps({"deleted": args.task_id, "title": title,
                      "project_id": project_id}, indent=2))
    return 0


# ---- entrypoint ------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ticktick-cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("setup", help="One-time TickTick OAuth flow.").set_defaults(
        func=cmd_setup)

    sub.add_parser("sync", help="Pull TickTick → local mirror.").set_defaults(
        func=cmd_sync)

    p_cands = sub.add_parser("candidates",
        help="Print filtered candidate tasks as JSON.")
    p_cands.add_argument("--limit", type=int, default=60)
    p_cands.set_defaults(func=cmd_candidates)

    p_recent = sub.add_parser("recent",
        help="Print last N completed tasks as JSON.")
    p_recent.add_argument("--limit", type=int, default=10)
    p_recent.set_defaults(func=cmd_recent)

    p_add = sub.add_parser("add", help="Create a task in TickTick.")
    p_add.add_argument("title")
    p_add.add_argument("--project", required=True,
        help="Project name (case-insensitive) or project id.")
    p_add.add_argument("--content", default=None, help="Optional task notes.")
    p_add.add_argument("--priority", type=int, default=None,
        choices=[0, 1, 3, 5], help="TickTick priority scale.")
    p_add.add_argument("--due", default=None,
        help='ISO 8601 due date, e.g. "2026-05-30T00:00:00+0000".')
    p_add.add_argument("--remind", action="append", default=[],
        metavar="DURATION",
        help="Reminder N minutes/hours/days BEFORE the due time. "
             "Examples: '15m', '1h', '2d', 'at-due'. May be passed "
             "multiple times for multiple reminders.")
    p_add.add_argument("--repeat", default=None, metavar="RRULE",
        help="Recurrence rule in iCal RRULE format, e.g. "
             "'RRULE:FREQ=DAILY;INTERVAL=1' or "
             "'RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR'.")
    p_add.add_argument("--tag", action="append", default=[], metavar="TAG",
        help="Tag to attach to the task. Pass multiple times for "
             "multiple tags. Tags are case-sensitive on TickTick's side.")
    p_add.set_defaults(func=cmd_add)

    p_done = sub.add_parser("complete", help="Mark a task complete.")
    p_done.add_argument("task_id")
    p_done.set_defaults(func=cmd_complete)

    p_del = sub.add_parser("delete",
        help="Delete a task. Dry-run by default; pass --apply to perform.")
    p_del.add_argument("task_id")
    p_del.add_argument("--apply", action="store_true",
        help="Actually perform the deletion. Without --apply this is a "
             "dry run that prints the task title.")
    p_del.set_defaults(func=cmd_delete)

    p_move = sub.add_parser("move",
        help="Move a task to a different project.")
    p_move.add_argument("task_id")
    p_move.add_argument("--to", required=True,
        help="Destination project name (case-insensitive) or project id.")
    p_move.set_defaults(func=cmd_move)

    p_repeat = sub.add_parser("repeat",
        help="Set or clear an iCal RRULE recurrence on a task.")
    p_repeat.add_argument("task_id")
    p_repeat.add_argument("rrule", nargs="?",
        help="iCal RRULE string, e.g. 'RRULE:FREQ=DAILY;INTERVAL=1'.")
    p_repeat.add_argument("--clear", action="store_true",
        help="Remove the existing recurrence rule. Cannot combine with an RRULE.")
    p_repeat.set_defaults(func=cmd_repeat)

    p_edit = sub.add_parser("edit",
        help="Edit fields on an existing task (title, content, dates, priority).")
    p_edit.add_argument("task_id")
    p_edit.add_argument("--title", default=None, help="Replace the task's title.")
    p_edit.add_argument("--content", default=None,
        help="Replace the task's notes/content.")
    p_edit.add_argument("--due", default=None, metavar="WHEN",
        help="Set due date. ISO 8601, '+7d', 'monday', 'today', etc. — see dates.py.")
    p_edit.add_argument("--clear-due", dest="clear_due", action="store_true",
        help="Clear the due date. Cannot combine with --due.")
    p_edit.add_argument("--start", default=None, metavar="WHEN",
        help="Set start date. Same grammar as --due.")
    p_edit.add_argument("--clear-start", dest="clear_start", action="store_true",
        help="Clear the start date. Cannot combine with --start.")
    p_edit.add_argument("--priority", default=None, type=_parse_priority,
        metavar="P",
        help="One of: none, low, medium, high — or numeric 0/1/3/5.")
    p_edit.set_defaults(func=cmd_edit)

    p_punt = sub.add_parser("punt",
        help="Push a task's start date forward (hide it from views until WHEN).")
    p_punt.add_argument("task_id")
    p_punt.add_argument("duration", metavar="WHEN",
        help="Same grammar as `edit --start`: ISO 8601, '+7d', 'monday', etc.")
    p_punt.set_defaults(func=cmd_punt)

    p_remind = sub.add_parser("remind",
        help="Set reminders on an existing task (replaces existing reminders).")
    p_remind.add_argument("task_id")
    p_remind.add_argument("durations", nargs="*",
        help="One or more durations BEFORE due (e.g. '15m 1h 1d'). "
             "Use 'at-due' for a reminder at the due time itself.")
    p_remind.add_argument("--clear", action="store_true",
        help="Remove all reminders from the task. Cannot combine with durations.")
    p_remind.set_defaults(func=cmd_remind)

    p_tag = sub.add_parser("tag",
        help="Manage tags: add/remove on a task, rename/delete across the local mirror.")
    tag_sub = p_tag.add_subparsers(dest="tag_action", required=True)

    p_t_add = tag_sub.add_parser("add",
        help="Add tag(s) to a task (merges with existing).")
    p_t_add.add_argument("task_id")
    p_t_add.add_argument("tag", nargs="+",
        help="One or more tags to add. Duplicates are skipped silently.")
    p_t_add.set_defaults(func=cmd_tag_add)

    p_t_rm = tag_sub.add_parser("remove",
        help="Remove tag(s) from a task.")
    p_t_rm.add_argument("task_id")
    p_t_rm.add_argument("tag", nargs="+",
        help="One or more tags to remove.")
    p_t_rm.add_argument("--ignore-case", action="store_true",
        help="Match tag names case-insensitively.")
    p_t_rm.set_defaults(func=cmd_tag_remove)

    p_t_ren = tag_sub.add_parser("rename",
        help="Rename a tag across the local mirror. Auto-pre-syncs; "
             "excluded projects + historical completions are missed.")
    p_t_ren.add_argument("old", help="Existing tag name.")
    p_t_ren.add_argument("new", help="New tag name.")
    p_t_ren.add_argument("--apply", action="store_true",
        help="Actually perform the rename. Without --apply this is a "
             "dry run that prints affected tasks.")
    p_t_ren.add_argument("--ignore-case", action="store_true",
        help="Match the old tag name case-insensitively (renames every "
             "capitalization variant).")
    p_t_ren.set_defaults(func=cmd_tag_rename)

    p_t_del = tag_sub.add_parser("delete",
        help="Remove a tag across the local mirror. Auto-pre-syncs; "
             "excluded projects + historical completions are missed.")
    p_t_del.add_argument("tag", help="Tag to delete.")
    p_t_del.add_argument("--apply", action="store_true",
        help="Actually perform the deletion. Without --apply this is a "
             "dry run that prints affected tasks.")
    p_t_del.add_argument("--ignore-case", action="store_true",
        help="Match tag name case-insensitively (deletes every "
             "capitalization variant).")
    p_t_del.set_defaults(func=cmd_tag_delete)

    return p


def main() -> None:
    _load_secrets_file()
    parser = _build_parser()
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
