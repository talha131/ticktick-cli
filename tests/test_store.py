import sqlite3
from pathlib import Path
from ticktick_cli.store import Store


def test_store_creates_schema(tmp_path: Path) -> None:
    s = Store(tmp_path / "tasks.db")
    s.init_schema()
    tables = {r[0] for r in s.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert tables >= {"projects", "tasks", "sync_state", "local_signals"}


def test_store_applies_pragmas(tmp_path: Path) -> None:
    s = Store(tmp_path / "tasks.db")
    s.init_schema()
    journal_mode = s.conn.execute("PRAGMA journal_mode").fetchone()[0]
    fk = s.conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert journal_mode == "wal"
    assert fk == 1


def test_tasks_has_completed_at_column(tmp_path: Path) -> None:
    s = Store(tmp_path / "tasks.db")
    s.init_schema()
    cols = {r[1] for r in s.conn.execute("PRAGMA table_info(tasks)")}
    assert "completed_at" in cols
    assert "status" in cols


def test_local_signals_fk_cascade(tmp_path: Path) -> None:
    s = Store(tmp_path / "tasks.db")
    s.init_schema()
    s.conn.execute("INSERT INTO projects(id, name, slug) VALUES ('p1', 'P', 'p')")
    s.conn.execute(
        "INSERT INTO tasks(id, project_id, title, status, updated_at) "
        "VALUES ('t1', 'p1', 'T', 0, '2026-05-25T12:00:00')"
    )
    s.conn.execute(
        "INSERT INTO local_signals(task_id, last_promoted_at, promotion_count) "
        "VALUES ('t1', '2026-05-25T13:00:00', 1)"
    )
    s.conn.commit()
    s.conn.execute("DELETE FROM tasks WHERE id='t1'")
    remaining = s.conn.execute(
        "SELECT COUNT(*) FROM local_signals WHERE task_id='t1'"
    ).fetchone()[0]
    assert remaining == 0  # cascaded


def test_idempotent_init(tmp_path: Path) -> None:
    db = tmp_path / "tasks.db"
    Store(db).init_schema()
    # second open + init should not crash
    Store(db).init_schema()
