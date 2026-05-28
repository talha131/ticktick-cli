"""CLI entry point.

A thin wrapper around TickTick's Open API plus a local SQLite mirror.

Subcommands: setup, sync, candidates, recent, add, complete, remind.
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
from .store import Store
from .sync import Syncer
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
    )
    # Refresh mirror so the new task is visible to `candidates` immediately.
    Syncer(store=store, client=client,
           excluded_names=settings.filters.excluded_projects_by_name).run()
    print(json.dumps({"id": created.get("id"), "title": created.get("title"),
                      "project_id": project_id,
                      "reminders": created.get("reminders", [])}, indent=2))
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
           excluded_names=settings.filters.excluded_projects_by_name).run()
    print(json.dumps({"id": args.task_id,
                      "reminders": updated.get("reminders", triggers)},
                     indent=2))
    return 0


def cmd_complete(args: argparse.Namespace) -> int:
    """Mark a task complete via TickTick API and re-sync."""
    settings = _load_settings_from_home()
    store = _open_store(settings)
    project_id = _lookup_project_id(store, args.task_id)
    client = _build_client()
    client.complete_task(project_id, args.task_id)
    Syncer(store=store, client=client,
           excluded_names=settings.filters.excluded_projects_by_name).run()
    print(f"Completed {args.task_id}")
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
    p_add.set_defaults(func=cmd_add)

    p_done = sub.add_parser("complete", help="Mark a task complete.")
    p_done.add_argument("task_id")
    p_done.set_defaults(func=cmd_complete)

    p_remind = sub.add_parser("remind",
        help="Set reminders on an existing task (replaces existing reminders).")
    p_remind.add_argument("task_id")
    p_remind.add_argument("durations", nargs="*",
        help="One or more durations BEFORE due (e.g. '15m 1h 1d'). "
             "Use 'at-due' for a reminder at the due time itself.")
    p_remind.add_argument("--clear", action="store_true",
        help="Remove all reminders from the task. Cannot combine with durations.")
    p_remind.set_defaults(func=cmd_remind)

    return p


def main() -> None:
    _load_secrets_file()
    parser = _build_parser()
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
