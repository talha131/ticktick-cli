"""Tag operations against the local mirror.

TickTick's Open API treats tags as a denormalized list on each task —
there's no first-class rename or delete endpoint. Cross-cutting tag
operations (rename, delete) are emulated by finding every task that
carries the tag from the local mirror, then calling update_task on
each. Sync first if you want freshness.

Tag matching is case-sensitive by default to mirror TickTick's own
behavior ("Work" and "work" are distinct tags). Pass `ignore_case=True`
when you want to gather every capitalization variant — useful for
cleanup."""

from __future__ import annotations
import json
from typing import Any
from .store import Store


def get_task_tags(store: Store, task_id: str) -> list[str]:
    """Return the current tag list for a task from the local mirror.

    Empty list if the task has no tags or isn't in the mirror at all —
    callers shouldn't need to distinguish; both mean "nothing to start
    from" when computing a merged tag set."""
    row = store.conn.execute(
        "SELECT tags FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if not row or not row["tags"]:
        return []
    return json.loads(row["tags"])


def find_tasks_with_tag(
    store: Store,
    tag: str,
    *,
    ignore_case: bool = False,
) -> list[dict[str, Any]]:
    """Return every task whose tag list contains `tag`.

    Each entry is `{id, project_id, title, tags}` — enough for the
    caller to issue an update_task without an extra lookup. Filtered
    in Python rather than via SQLite JSON1: the volume is small
    (hundreds of tasks at most) and this keeps the query readable."""
    target = tag.casefold() if ignore_case else tag
    rows = store.conn.execute(
        "SELECT id, project_id, title, tags FROM tasks WHERE tags IS NOT NULL"
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        tags = json.loads(r["tags"]) if r["tags"] else []
        if ignore_case:
            matched = any(t.casefold() == target for t in tags)
        else:
            matched = target in tags
        if matched:
            out.append({
                "id": r["id"],
                "project_id": r["project_id"],
                "title": r["title"],
                "tags": tags,
            })
    return out
