"""Read path for recently-completed tasks.

Separate from `sync.py` on purpose: this is the loop-closing query
that powers `ticktick-cli recent`. It batches one call per invocation
against POST /open/v1/task/completed and caches historical days in
`completed_cache` so re-runs within the same calendar day are
near-instant.

Today's data is deliberately NOT cached. Tasks completed via the
TickTick mobile app land directly on the cloud, and `recent` is the
loop that surfaces them — caching today would defeat that purpose.
Historical days, on the other hand, are immutable enough in practice
that we cache them indefinitely. A user who un-completes a task in
the past will see a stale cache entry until manually evicted; the
trade-off is worth it for the same-day re-run cost.

The day key is the UTC date of `completedTime`. Multi-timezone
boundary tasks may, in rare cases, double-fetch — the merge step
dedupes by task id."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .store import Store
from .ticktick import TickTickClient

# Same format the rest of the codebase uses for TickTick wire dates.
_TICKTICK_DATE_FMT = "%Y-%m-%dT%H:%M:%S%z"


def _parse_completed_time(s: str) -> datetime:
    """TickTick returns completedTime with or without millisecond precision —
    `"2026-03-04T23:58:20.000+0000"` (List Completed Tasks example) and
    `"2026-05-26T15:00:00+0000"` (everywhere else). Handle both."""
    # The strptime "%f" directive is variable-width on parsing; check for
    # the dot to pick the right format string.
    if "." in s:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f%z")
    return datetime.strptime(s, _TICKTICK_DATE_FMT)


def _utc_day(iso_ts: str) -> str:
    """`'2026-05-26T15:00:00+0000'` → `'2026-05-26'` (UTC)."""
    return _parse_completed_time(iso_ts).astimezone(timezone.utc).strftime("%Y-%m-%d")


def list_recent(
    store: Store,
    client: TickTickClient,
    *,
    days: int,
    project_id_filter: str | None,
    limit: int,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return up to `limit` raw TickTick task dicts (status=2) completed
    within the past `days` days, sorted by `completedTime` DESC.

    Algorithm:
      1. Build the day window: [today_utc, today_utc - (days-1)].
      2. For each (project, historical_day), read the cache. Hits go into
         a working dict.
      3. Today is always considered "needs fetch". Any historical day
         missing from cache is also "needs fetch".
      4. Make ONE API call covering [earliest_needed_day .. now] with all
         relevant project ids. Partition the response by (project, day).
      5. Upsert cache rows for historical days only — including empty-
         result days so a re-run doesn't keep paying the API cost.
      6. Merge cached + fresh, drop tasks outside the window (defensive
         against a server returning extras), dedupe by id, sort DESC,
         truncate to limit.

    Tests inject `now` for deterministic windows; production passes
    nothing and gets `datetime.now(UTC)`."""
    if days <= 0:
        return []

    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    today_str = now_utc.strftime("%Y-%m-%d")
    window_days = [
        (now_utc - timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(days)
    ]
    window_set = set(window_days)

    # Resolve project scope.
    if project_id_filter is not None:
        project_ids = [project_id_filter]
    else:
        project_ids = [
            r["id"] for r in store.conn.execute(
                "SELECT id FROM projects WHERE archived = 0"
            )
        ]
    if not project_ids:
        return []

    # 1. Walk the cache for historical days.
    cache: dict[tuple[str, str], list[dict[str, Any]]] = {}
    days_needing_fetch: set[str] = {today_str}  # today is never cached
    for pid in project_ids:
        for day in window_days:
            if day == today_str:
                continue
            row = store.conn.execute(
                "SELECT tasks_json FROM completed_cache "
                "WHERE project_id = ? AND day = ?",
                (pid, day),
            ).fetchone()
            if row is not None:
                cache[(pid, day)] = json.loads(row["tasks_json"])
            else:
                days_needing_fetch.add(day)

    # 2. If anything needs fetching, batch one API call covering the span.
    if days_needing_fetch:
        earliest_day = min(days_needing_fetch)
        start_dt = datetime.strptime(earliest_day, "%Y-%m-%d").replace(
            tzinfo=timezone.utc,
        )
        end_dt = now_utc
        fetched = client.list_completed_tasks(
            project_ids=project_ids,
            start_date=start_dt.strftime(_TICKTICK_DATE_FMT),
            end_date=end_dt.strftime(_TICKTICK_DATE_FMT),
        )

        # Partition by (project_id, day).
        partitioned: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for t in fetched:
            pid = t.get("projectId")
            ct = t.get("completedTime")
            if not pid or not ct:
                continue
            try:
                day = _utc_day(ct)
            except ValueError:
                # Skip rows with completedTime in an unparseable shape
                # rather than crash the whole listing.
                continue
            partitioned.setdefault((pid, day), []).append(t)

        # 3. Persist historical days (including empty ones) and merge into cache.
        for pid in project_ids:
            for day in days_needing_fetch:
                if day == today_str:
                    # Bring today's results into the working set without caching.
                    cache[(pid, day)] = partitioned.get((pid, day), [])
                    continue
                if day not in window_set:
                    # Defensive: shouldn't happen — days_needing_fetch is
                    # always a subset of window_days.
                    continue
                tasks_for_cell = partitioned.get((pid, day), [])
                cache[(pid, day)] = tasks_for_cell
                store.conn.execute(
                    "INSERT INTO completed_cache(project_id, day, "
                    "fetched_at, tasks_json) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(project_id, day) DO UPDATE SET "
                    "fetched_at=excluded.fetched_at, "
                    "tasks_json=excluded.tasks_json",
                    (pid, day, now_utc.isoformat(), json.dumps(tasks_for_cell)),
                )

    # 4. Flatten, filter to window (defensive), dedupe, sort, truncate.
    all_tasks: list[dict[str, Any]] = []
    for (_pid, day), tasks in cache.items():
        if day not in window_set:
            continue
        all_tasks.extend(tasks)

    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for t in all_tasks:
        ct = t.get("completedTime")
        if not ct:
            continue
        try:
            if _utc_day(ct) not in window_set:
                continue
        except ValueError:
            continue
        tid = t.get("id")
        if not tid or tid in seen:
            continue
        seen.add(tid)
        unique.append(t)

    unique.sort(key=lambda t: t.get("completedTime", ""), reverse=True)
    return unique[:limit]
