"""Transactional sync: TickTick cloud → SQLite mirror. Spec §5.

Rules:
- BEGIN IMMEDIATE transaction wrapping the entire sync.
- Sweep marks status=0 tasks missing from cloud as status=3 (archived).
- status=2 (completed) rows are NEVER touched by the sweep.
- last_full_sync is written inside the transaction and only persists on COMMIT."""

from __future__ import annotations
import json
import logging
from datetime import datetime, timezone
from typing import Any
from .store import Store
from .ticktick import TickTickClient

log = logging.getLogger(__name__)


def _slugify(name: str) -> str:
    return "-".join(name.lower().split())


class Syncer:
    def __init__(self, store: Store, client: TickTickClient, excluded_names: list[str]) -> None:
        self.store = store
        self.client = client
        self.excluded_names = excluded_names

    def run(self) -> None:
        cur = self.store.conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            projects = self.client.list_projects()
            if not projects:
                raise RuntimeError(
                    "TickTick returned empty project list — aborting sync to protect local data"
                )
            seen_task_ids: set[str] = set()
            seen_project_ids: set[str] = set()

            for p in projects:
                seen_project_ids.add(p["id"])
                cur.execute(
                    "INSERT INTO projects(id, name, slug, archived, raw_json) "
                    "VALUES (?,?,?,0,?) "
                    "ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
                    "slug=excluded.slug, archived=0, raw_json=excluded.raw_json",
                    (p["id"], p["name"], _slugify(p["name"]), json.dumps(p)),
                )

            for p in projects:
                data = self.client.get_project_data(p["id"])
                for t in data.get("tasks", []):
                    seen_task_ids.add(t["id"])
                    cur.execute(
                        "INSERT INTO tasks(id, project_id, title, content, status, "
                        "priority, due_date, start_date, completed_at, tags, "
                        "repeat_flag, updated_at, raw_json) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(id) DO UPDATE SET "
                        "project_id=excluded.project_id, title=excluded.title, "
                        "content=excluded.content, status=excluded.status, "
                        "priority=excluded.priority, due_date=excluded.due_date, "
                        "start_date=excluded.start_date, "
                        "completed_at=excluded.completed_at, tags=excluded.tags, "
                        "repeat_flag=excluded.repeat_flag, "
                        "updated_at=excluded.updated_at, raw_json=excluded.raw_json",
                        (
                            t["id"], t.get("projectId", p["id"]),
                            t["title"], t.get("content"),
                            t.get("status", 0), t.get("priority"),
                            t.get("dueDate"), t.get("startDate"),
                            t.get("completedTime"),
                            json.dumps(t.get("tags", [])),
                            t.get("repeatFlag"),
                            t.get("modifiedTime") or datetime.now(timezone.utc).isoformat(),
                            json.dumps(t),
                        ),
                    )

            # Sweep: archive todos that disappeared. NEVER touch status=2.
            placeholders = ",".join("?" * len(seen_task_ids)) if seen_task_ids else "''"
            cur.execute(
                f"UPDATE tasks SET status = 3 "
                f"WHERE status = 0 AND id NOT IN ({placeholders})",
                tuple(seen_task_ids),
            )

            # Resolve excluded project names to ids.
            name_to_id = {p["name"].lower(): p["id"] for p in projects}
            excluded_ids: list[str] = []
            for name in self.excluded_names:
                pid = name_to_id.get(name.lower())
                if pid:
                    excluded_ids.append(pid)
                else:
                    log.warning("excluded_projects_by_name: no match for %r", name)
            cur.execute(
                "INSERT INTO sync_state(k, v) VALUES ('excluded_project_ids', ?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (json.dumps(excluded_ids),),
            )

            cur.execute(
                "INSERT INTO sync_state(k, v) VALUES ('last_full_sync', ?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (datetime.now(timezone.utc).isoformat(),),
            )
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
