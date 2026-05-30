import json
import time
from pathlib import Path
from unittest.mock import MagicMock
from ticktick_cli.store import Store
from ticktick_cli.sync import Syncer


def _client_returning(projects, project_data, completed=None):
    c = MagicMock()
    c.list_projects.return_value = projects
    c.get_project_data.side_effect = lambda pid: project_data[pid]
    c.list_completed_tasks.return_value = completed or []
    return c


def test_full_sync_inserts_projects_and_tasks(tmp_path: Path) -> None:
    s = Store(tmp_path / "tasks.db"); s.init_schema()
    client = _client_returning(
        projects=[{"id": "p1", "name": "GCE"}],
        project_data={
            "p1": {
                "project": {"id": "p1", "name": "GCE"},
                "tasks": [
                    {"id": "t1", "title": "A", "status": 0, "projectId": "p1",
                     "modifiedTime": "2026-05-24T10:00:00+0000"},
                    {"id": "t2", "title": "B", "status": 2, "projectId": "p1",
                     "completedTime": "2026-05-24T15:00:00+0000",
                     "modifiedTime": "2026-05-24T15:00:00+0000"},
                ],
            }
        },
    )
    syncer = Syncer(store=s, client=client, excluded_names=[])
    syncer.run()

    rows = list(s.conn.execute("SELECT id, status, completed_at FROM tasks"))
    by_id = {r["id"]: r for r in rows}
    assert by_id["t1"]["status"] == 0 and by_id["t1"]["completed_at"] is None
    assert by_id["t2"]["status"] == 2 and by_id["t2"]["completed_at"].startswith("2026-05-24")


def test_sync_marks_missing_tasks_archived(tmp_path: Path) -> None:
    s = Store(tmp_path / "tasks.db"); s.init_schema()
    s.conn.execute("INSERT INTO projects(id, name, slug) VALUES ('p1','GCE','gce')")
    s.conn.execute(
        "INSERT INTO tasks(id, project_id, title, status, updated_at) "
        "VALUES ('ghost', 'p1', 'gone', 0, '2026-05-20T00:00:00')"
    )
    s.conn.execute(
        "INSERT INTO tasks(id, project_id, title, status, completed_at, updated_at) "
        "VALUES ('done', 'p1', 'finished', 2, '2026-05-21T00:00:00', '2026-05-21T00:00:00')"
    )
    client = _client_returning(
        projects=[{"id": "p1", "name": "GCE"}],
        project_data={"p1": {"project": {"id": "p1", "name": "GCE"}, "tasks": []}},
    )
    Syncer(store=s, client=client, excluded_names=[]).run()
    rows = {r["id"]: r["status"] for r in s.conn.execute("SELECT id, status FROM tasks")}
    assert rows["ghost"] == 3  # archived
    assert rows["done"] == 2   # completed history NOT touched


def test_sync_writes_last_full_sync_only_on_success(tmp_path: Path) -> None:
    s = Store(tmp_path / "tasks.db"); s.init_schema()
    client = MagicMock()
    client.list_projects.side_effect = RuntimeError("boom")
    try:
        Syncer(store=s, client=client, excluded_names=[]).run()
    except RuntimeError:
        pass
    row = s.conn.execute(
        "SELECT v FROM sync_state WHERE k='last_full_sync'"
    ).fetchone()
    assert row is None  # nothing committed


def test_sync_empty_project_list_raises_and_does_not_archive(tmp_path: Path) -> None:
    """Empty project list from TickTick must raise and leave existing tasks untouched."""
    s = Store(tmp_path / "tasks.db"); s.init_schema()
    s.conn.execute("INSERT INTO projects(id, name, slug) VALUES ('p1','GCE','gce')")
    s.conn.execute(
        "INSERT INTO tasks(id, project_id, title, status, updated_at) "
        "VALUES ('t1', 'p1', 'Keep me', 0, '2026-05-24T00:00:00')"
    )
    s.conn.commit()

    client = MagicMock()
    client.list_projects.return_value = []  # transient empty 200

    import pytest
    with pytest.raises(RuntimeError, match="empty project list"):
        Syncer(store=s, client=client, excluded_names=[]).run()

    # Task must still be status=0 — no archiving occurred
    row = s.conn.execute("SELECT status FROM tasks WHERE id='t1'").fetchone()
    assert row["status"] == 0


def test_sync_persists_repeat_flag(tmp_path: Path) -> None:
    """Tasks with a repeatFlag from TickTick land in the local mirror's
    repeat_flag column. Tasks without one stay NULL."""
    s = Store(tmp_path / "tasks.db"); s.init_schema()
    client = _client_returning(
        projects=[{"id": "p1", "name": "GCE"}],
        project_data={
            "p1": {
                "project": {"id": "p1", "name": "GCE"},
                "tasks": [
                    {"id": "t1", "title": "Daily standup", "status": 0,
                     "projectId": "p1",
                     "repeatFlag": "RRULE:FREQ=DAILY;INTERVAL=1",
                     "modifiedTime": "2026-05-24T10:00:00+0000"},
                    {"id": "t2", "title": "One-shot", "status": 0,
                     "projectId": "p1",
                     "modifiedTime": "2026-05-24T10:00:00+0000"},
                ],
            }
        },
    )
    Syncer(store=s, client=client, excluded_names=[]).run()
    rows = {r["id"]: r["repeat_flag"]
            for r in s.conn.execute("SELECT id, repeat_flag FROM tasks")}
    assert rows["t1"] == "RRULE:FREQ=DAILY;INTERVAL=1"
    assert rows["t2"] is None


def test_sync_pulls_recent_completions(tmp_path: Path) -> None:
    """Completed tasks returned by /open/v1/task/completed land in the
    mirror with status=2 and completed_at populated, alongside actives."""
    s = Store(tmp_path / "tasks.db"); s.init_schema()
    client = _client_returning(
        projects=[{"id": "p1", "name": "GCE"}],
        project_data={
            "p1": {
                "project": {"id": "p1", "name": "GCE"},
                "tasks": [
                    {"id": "live", "title": "Still going", "status": 0,
                     "projectId": "p1",
                     "modifiedTime": "2026-05-28T10:00:00+0000"},
                ],
            }
        },
        completed=[
            {"id": "done1", "projectId": "p1", "title": "Finished a",
             "status": 2, "priority": 3,
             "completedTime": "2026-05-26T15:00:00+0000",
             "modifiedTime": "2026-05-26T15:00:00+0000"},
            {"id": "done2", "projectId": "p1", "title": "Finished b",
             "status": 2,
             "completedTime": "2026-05-20T09:00:00+0000",
             "modifiedTime": "2026-05-20T09:00:00+0000"},
        ],
    )
    Syncer(store=s, client=client, excluded_names=[]).run()
    rows = {
        r["id"]: r
        for r in s.conn.execute(
            "SELECT id, status, completed_at, priority FROM tasks"
        )
    }
    assert rows["live"]["status"] == 0
    assert rows["done1"]["status"] == 2
    assert rows["done1"]["completed_at"].startswith("2026-05-26")
    assert rows["done1"]["priority"] == 3
    assert rows["done2"]["status"] == 2
    assert rows["done2"]["completed_at"].startswith("2026-05-20")
    # And the API was called with a bounded date range, not unfiltered.
    call_kwargs = client.list_completed_tasks.call_args.kwargs
    assert "start_date" in call_kwargs and "end_date" in call_kwargs


def test_sync_skips_orphan_completions_without_known_project(tmp_path: Path) -> None:
    """If the completed endpoint returns a task whose project is no
    longer in /open/v1/project (deleted by the user), the upsert would
    violate the project_id FK. Skip the row silently — mirrors how the
    active-task path treats unknown projects."""
    s = Store(tmp_path / "tasks.db"); s.init_schema()
    client = _client_returning(
        projects=[{"id": "p1", "name": "GCE"}],
        project_data={
            "p1": {"project": {"id": "p1", "name": "GCE"}, "tasks": []}
        },
        completed=[
            {"id": "orphan", "projectId": "ghost-project",
             "title": "From deleted project", "status": 2,
             "completedTime": "2026-05-15T09:00:00+0000",
             "modifiedTime": "2026-05-15T09:00:00+0000"},
        ],
    )
    Syncer(store=s, client=client, excluded_names=[]).run()
    rows = list(s.conn.execute("SELECT id FROM tasks"))
    assert rows == []


def test_sync_freshly_completed_task_not_archived_by_sweep(tmp_path: Path) -> None:
    """A task that was status=0 in the mirror but flipped to status=2 on
    the cloud must not be marked status=3 by the sweep — the completed
    fetch should claim it and the upsert flips it to status=2."""
    s = Store(tmp_path / "tasks.db"); s.init_schema()
    s.conn.execute("INSERT INTO projects(id, name, slug) VALUES ('p1','GCE','gce')")
    s.conn.execute(
        "INSERT INTO tasks(id, project_id, title, status, updated_at) "
        "VALUES ('flipped', 'p1', 'Just finished', 0, '2026-05-25T00:00:00')"
    )
    s.conn.commit()
    client = _client_returning(
        projects=[{"id": "p1", "name": "GCE"}],
        project_data={"p1": {"project": {"id": "p1", "name": "GCE"}, "tasks": []}},
        completed=[
            {"id": "flipped", "projectId": "p1", "title": "Just finished",
             "status": 2,
             "completedTime": "2026-05-28T14:00:00+0000",
             "modifiedTime": "2026-05-28T14:00:00+0000"},
        ],
    )
    Syncer(store=s, client=client, excluded_names=[]).run()
    row = s.conn.execute(
        "SELECT status, completed_at FROM tasks WHERE id='flipped'"
    ).fetchone()
    assert row["status"] == 2
    assert row["completed_at"].startswith("2026-05-28")


def test_sync_skips_completions_call_when_lookback_zero(tmp_path: Path) -> None:
    """A lookback of 0 days disables the completed-tasks fetch entirely.
    Useful for tests that want to inspect only the active-task path, and
    for users who want the lighter sync."""
    s = Store(tmp_path / "tasks.db"); s.init_schema()
    client = _client_returning(
        projects=[{"id": "p1", "name": "GCE"}],
        project_data={"p1": {"project": {"id": "p1", "name": "GCE"}, "tasks": []}},
    )
    Syncer(store=s, client=client, excluded_names=[],
           completions_lookback_days=0).run()
    client.list_completed_tasks.assert_not_called()


def test_excluded_projects_resolved_to_ids(tmp_path: Path) -> None:
    s = Store(tmp_path / "tasks.db"); s.init_schema()
    client = _client_returning(
        projects=[{"id": "p1", "name": "GCE"}, {"id": "p2", "name": "Someday"}],
        project_data={
            "p1": {"project": {"id": "p1", "name": "GCE"},
                   "tasks": [{"id": "t1", "title": "X", "status": 0, "projectId": "p1",
                              "modifiedTime": "2026-05-24T10:00:00+0000"}]},
            "p2": {"project": {"id": "p2", "name": "Someday"},
                   "tasks": [{"id": "t2", "title": "Y", "status": 0, "projectId": "p2",
                              "modifiedTime": "2026-05-24T10:00:00+0000"}]},
        },
    )
    Syncer(store=s, client=client, excluded_names=["Someday"]).run()
    row = s.conn.execute("SELECT v FROM sync_state WHERE k='excluded_project_ids'").fetchone()
    assert row is not None
    assert json.loads(row["v"]) == ["p2"]
