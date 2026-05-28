import json
import time
from pathlib import Path
from unittest.mock import MagicMock
from ticktick_cli.store import Store
from ticktick_cli.sync import Syncer


def _client_returning(projects, project_data):
    c = MagicMock()
    c.list_projects.return_value = projects
    c.get_project_data.side_effect = lambda pid: project_data[pid]
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
