from pathlib import Path
from ticktick_cli.store import Store
from ticktick_cli.candidates import list_candidates


def _seed(s: Store, rows: list[dict]) -> None:
    s.conn.execute("INSERT INTO projects(id, name, slug) VALUES ('p1','P','p')")
    for r in rows:
        s.conn.execute(
            "INSERT INTO tasks(id, project_id, title, status, priority, due_date, "
            "start_date, completed_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (r["id"], "p1", r["title"], r.get("status", 0), r.get("priority", 0),
             r.get("due_date"), r.get("start_date"), None, "2026-05-24T00:00:00"),
        )


def test_excludes_completed_and_archived(tmp_path: Path) -> None:
    s = Store(tmp_path / "tasks.db"); s.init_schema()
    _seed(s, [
        {"id": "a", "title": "todo", "status": 0},
        {"id": "b", "title": "done", "status": 2},
        {"id": "c", "title": "arch", "status": 3},
    ])
    rows = list_candidates(s, excluded_project_ids=[], now_iso="2026-05-25T10:00:00")
    ids = [r["id"] for r in rows]
    assert ids == ["a"]


def test_excludes_future_start_date(tmp_path: Path) -> None:
    s = Store(tmp_path / "tasks.db"); s.init_schema()
    _seed(s, [
        {"id": "now", "title": "now", "start_date": None},
        {"id": "later", "title": "later", "start_date": "2027-01-01T00:00:00"},
    ])
    rows = list_candidates(s, excluded_project_ids=[], now_iso="2026-05-25T10:00:00")
    assert [r["id"] for r in rows] == ["now"]


def test_orders_priority_desc_then_due_with_nulls_last(tmp_path: Path) -> None:
    s = Store(tmp_path / "tasks.db"); s.init_schema()
    _seed(s, [
        {"id": "low-due", "title": "x", "priority": 1, "due_date": "2026-05-30"},
        {"id": "high-no-due", "title": "y", "priority": 5, "due_date": None},
        {"id": "high-due", "title": "z", "priority": 5, "due_date": "2026-05-26"},
    ])
    rows = list_candidates(s, excluded_project_ids=[], now_iso="2026-05-25T10:00:00")
    assert [r["id"] for r in rows] == ["high-due", "high-no-due", "low-due"]


def test_excludes_by_project_id(tmp_path: Path) -> None:
    s = Store(tmp_path / "tasks.db"); s.init_schema()
    s.conn.execute("INSERT INTO projects(id, name, slug) VALUES ('p2','Someday','someday')")
    _seed(s, [{"id": "keep", "title": "k"}])
    s.conn.execute(
        "INSERT INTO tasks(id, project_id, title, status, updated_at) "
        "VALUES ('skip', 'p2', 's', 0, '2026-05-24T00:00:00')"
    )
    rows = list_candidates(s, excluded_project_ids=["p2"], now_iso="2026-05-25T10:00:00")
    assert [r["id"] for r in rows] == ["keep"]
