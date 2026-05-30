"""Tests for src/ticktick_cli/recent.py.

`list_recent` is the read path for completed tasks. It batches one API
call per invocation against POST /open/v1/task/completed, caches
historical days indefinitely in the `completed_cache` table, and always
re-fetches today so swipe-completed-on-mobile tasks show up without a
full sync.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ticktick_cli.recent import list_recent
from ticktick_cli.store import Store

# A fixed "now" so test windows are deterministic.
NOW = datetime(2026, 5, 30, 14, 0, 0, tzinfo=timezone.utc)


def _seed_project(s: Store, pid: str, name: str) -> None:
    s.conn.execute(
        "INSERT INTO projects(id, name, slug, archived) VALUES (?,?,?,0)",
        (pid, name, name.lower()),
    )


def _completion(
    task_id: str,
    project_id: str,
    completed_time: str,
    title: str | None = None,
    **extras,
) -> dict:
    base = {
        "id": task_id,
        "projectId": project_id,
        "title": title or f"Task {task_id}",
        "status": 2,
        "completedTime": completed_time,
    }
    base.update(extras)
    return base


def test_list_recent_returns_empty_when_no_completions(tmp_path: Path) -> None:
    """Empty API response → empty list, no error."""
    s = Store(tmp_path / "tasks.db"); s.init_schema()
    _seed_project(s, "p1", "Work")
    client = MagicMock()
    client.list_completed_tasks.return_value = []

    result = list_recent(
        s, client, days=7, project_id_filter=None, limit=20, now=NOW,
    )
    assert result == []


def test_list_recent_returns_sorted_by_completed_at_desc(tmp_path: Path) -> None:
    """Mixed-project completions come back sorted DESC by completedTime."""
    s = Store(tmp_path / "tasks.db"); s.init_schema()
    _seed_project(s, "p1", "Work")
    _seed_project(s, "p2", "Personal")
    client = MagicMock()
    client.list_completed_tasks.return_value = [
        _completion("a", "p1", "2026-05-28T10:00:00+0000"),
        _completion("b", "p2", "2026-05-30T13:30:00+0000"),
        _completion("c", "p1", "2026-05-29T20:00:00+0000"),
    ]
    result = list_recent(
        s, client, days=7, project_id_filter=None, limit=20, now=NOW,
    )
    assert [t["id"] for t in result] == ["b", "c", "a"]


def test_list_recent_truncates_to_limit_after_sort(tmp_path: Path) -> None:
    """Limit applies AFTER cross-project sort — most-recent wins."""
    s = Store(tmp_path / "tasks.db"); s.init_schema()
    _seed_project(s, "p1", "Work")
    client = MagicMock()
    client.list_completed_tasks.return_value = [
        _completion("old", "p1", "2026-05-25T10:00:00+0000"),
        _completion("mid", "p1", "2026-05-27T10:00:00+0000"),
        _completion("new", "p1", "2026-05-30T10:00:00+0000"),
    ]
    result = list_recent(
        s, client, days=7, project_id_filter=None, limit=2, now=NOW,
    )
    assert [t["id"] for t in result] == ["new", "mid"]


def test_list_recent_filters_by_project_id(tmp_path: Path) -> None:
    """project_id_filter passes a one-element projectIds list to the API."""
    s = Store(tmp_path / "tasks.db"); s.init_schema()
    _seed_project(s, "p1", "Work")
    _seed_project(s, "p2", "Personal")
    client = MagicMock()
    client.list_completed_tasks.return_value = []

    list_recent(
        s, client, days=7, project_id_filter="p2", limit=20, now=NOW,
    )
    kwargs = client.list_completed_tasks.call_args.kwargs
    assert kwargs["project_ids"] == ["p2"]


def test_list_recent_uses_all_projects_when_no_filter(tmp_path: Path) -> None:
    """No filter → projectIds is the list of non-archived projects."""
    s = Store(tmp_path / "tasks.db"); s.init_schema()
    _seed_project(s, "p1", "Work")
    _seed_project(s, "p2", "Personal")
    # Archived projects are excluded.
    s.conn.execute(
        "INSERT INTO projects(id, name, slug, archived) VALUES ('p3','Old','old',1)"
    )
    client = MagicMock()
    client.list_completed_tasks.return_value = []

    list_recent(
        s, client, days=7, project_id_filter=None, limit=20, now=NOW,
    )
    kwargs = client.list_completed_tasks.call_args.kwargs
    assert sorted(kwargs["project_ids"]) == ["p1", "p2"]


def test_list_recent_passes_date_window(tmp_path: Path) -> None:
    """--days N → API gets start ~ today - (N-1) days, end ~ now."""
    s = Store(tmp_path / "tasks.db"); s.init_schema()
    _seed_project(s, "p1", "Work")
    client = MagicMock()
    client.list_completed_tasks.return_value = []

    list_recent(
        s, client, days=7, project_id_filter=None, limit=20, now=NOW,
    )
    kwargs = client.list_completed_tasks.call_args.kwargs
    # NOW = 2026-05-30 14:00 UTC; days=7 → earliest day is 2026-05-24.
    assert kwargs["start_date"].startswith("2026-05-24")
    # End is "now" (today). Loose check: starts with today.
    assert kwargs["end_date"].startswith("2026-05-30")


def test_list_recent_caches_historical_days_indefinitely(tmp_path: Path) -> None:
    """Second same-day invocation hits the cache for historical days —
    the API gets called once again (for today's range only), not for
    the full 7-day window."""
    s = Store(tmp_path / "tasks.db"); s.init_schema()
    _seed_project(s, "p1", "Work")
    client = MagicMock()
    client.list_completed_tasks.return_value = [
        _completion("hist", "p1", "2026-05-26T10:00:00+0000"),
        _completion("today1", "p1", "2026-05-30T09:00:00+0000"),
    ]

    # First call — full window.
    list_recent(
        s, client, days=7, project_id_filter=None, limit=20, now=NOW,
    )
    first_call_kwargs = client.list_completed_tasks.call_args.kwargs
    assert first_call_kwargs["start_date"].startswith("2026-05-24")

    # Second call with same NOW — only today should be re-fetched.
    client.list_completed_tasks.reset_mock()
    client.list_completed_tasks.return_value = [
        _completion("today1", "p1", "2026-05-30T09:00:00+0000"),
    ]
    result = list_recent(
        s, client, days=7, project_id_filter=None, limit=20, now=NOW,
    )

    # Exactly one API call on the second invocation.
    assert client.list_completed_tasks.call_count == 1
    second_call_kwargs = client.list_completed_tasks.call_args.kwargs
    # Range now starts at today (May 30), not 7 days ago.
    assert second_call_kwargs["start_date"].startswith("2026-05-30")

    # Cached historical task still appears in the merged result.
    ids = {t["id"] for t in result}
    assert "hist" in ids
    assert "today1" in ids


def test_list_recent_persists_empty_historical_days(tmp_path: Path) -> None:
    """A historical day with zero completions still gets cached (as [])
    so a re-run doesn't keep paying the API cost for the empty day."""
    s = Store(tmp_path / "tasks.db"); s.init_schema()
    _seed_project(s, "p1", "Work")
    client = MagicMock()
    client.list_completed_tasks.return_value = []  # zero results for full window

    list_recent(
        s, client, days=7, project_id_filter=None, limit=20, now=NOW,
    )
    # Historical days (6) should now have cache entries with empty payloads.
    rows = list(s.conn.execute(
        "SELECT project_id, day, tasks_json FROM completed_cache "
        "WHERE project_id='p1' ORDER BY day"
    ))
    cached_days = {r["day"]: r["tasks_json"] for r in rows}
    # 6 historical days (today is never cached).
    assert len(cached_days) == 6
    for ts_json in cached_days.values():
        assert ts_json == "[]"


def test_list_recent_does_not_cache_today(tmp_path: Path) -> None:
    """Today's completions are returned but never written to the cache —
    swipe-completions later in the day still need to be picked up."""
    s = Store(tmp_path / "tasks.db"); s.init_schema()
    _seed_project(s, "p1", "Work")
    client = MagicMock()
    client.list_completed_tasks.return_value = [
        _completion("today_task", "p1", "2026-05-30T12:00:00+0000"),
    ]

    list_recent(
        s, client, days=7, project_id_filter=None, limit=20, now=NOW,
    )
    today_str = "2026-05-30"
    row = s.conn.execute(
        "SELECT 1 FROM completed_cache WHERE project_id='p1' AND day=?",
        (today_str,),
    ).fetchone()
    assert row is None


def test_list_recent_excludes_tasks_outside_window(tmp_path: Path) -> None:
    """Completions older than --days are filtered out even if the API
    returns them (defensive against an over-eager server).

    The /task/completed endpoint accepts a date range and should respect
    it server-side, but mixed-timezone boundary effects can still leak
    a few outliers in. Our day-bucket check is the second line of defense."""
    s = Store(tmp_path / "tasks.db"); s.init_schema()
    _seed_project(s, "p1", "Work")
    client = MagicMock()
    client.list_completed_tasks.return_value = [
        _completion("in_window", "p1", "2026-05-30T10:00:00+0000"),
        _completion("too_old", "p1", "2026-05-01T10:00:00+0000"),
    ]
    result = list_recent(
        s, client, days=7, project_id_filter=None, limit=20, now=NOW,
    )
    ids = {t["id"] for t in result}
    assert "in_window" in ids
    assert "too_old" not in ids


def test_list_recent_dedupes_by_task_id(tmp_path: Path) -> None:
    """If a task appears twice (e.g. boundary-time double-count from a
    cached day + a re-fetch), dedupe on id so the consumer sees it once."""
    s = Store(tmp_path / "tasks.db"); s.init_schema()
    _seed_project(s, "p1", "Work")
    # Pre-seed the cache for May 26 with a task whose completedTime is
    # right at midnight.
    s.conn.execute(
        "INSERT INTO completed_cache(project_id, day, fetched_at, tasks_json) "
        "VALUES ('p1', '2026-05-26', '2026-05-30T00:00:00+00:00', ?)",
        (json.dumps([
            _completion("dup", "p1", "2026-05-26T23:59:59+0000")
        ]),),
    )
    client = MagicMock()
    # On the fetch for today, the API also returns "dup" (some edge case).
    client.list_completed_tasks.return_value = [
        _completion("dup", "p1", "2026-05-26T23:59:59+0000"),
    ]
    result = list_recent(
        s, client, days=7, project_id_filter=None, limit=20, now=NOW,
    )
    ids = [t["id"] for t in result]
    assert ids.count("dup") == 1


def test_list_recent_with_no_projects_returns_empty(tmp_path: Path) -> None:
    """No projects in mirror → return [] without an API call."""
    s = Store(tmp_path / "tasks.db"); s.init_schema()
    client = MagicMock()

    result = list_recent(
        s, client, days=7, project_id_filter=None, limit=20, now=NOW,
    )
    assert result == []
    client.list_completed_tasks.assert_not_called()


def test_list_recent_zero_days_returns_empty(tmp_path: Path) -> None:
    """--days 0 → empty window, zero API calls, empty result."""
    s = Store(tmp_path / "tasks.db"); s.init_schema()
    _seed_project(s, "p1", "Work")
    client = MagicMock()

    result = list_recent(
        s, client, days=0, project_id_filter=None, limit=20, now=NOW,
    )
    assert result == []
    client.list_completed_tasks.assert_not_called()
