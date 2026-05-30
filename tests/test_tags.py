import json
from pathlib import Path
from ticktick_cli.store import Store
from ticktick_cli.tags import find_tasks_with_tag, get_task_tags


def _seed(s: Store, rows: list[tuple[str, str, str, list[str]]]) -> None:
    """rows: (task_id, project_id, title, tags) tuples."""
    s.conn.execute("INSERT INTO projects(id, name, slug) VALUES ('p1','P','p')")
    for tid, pid, title, tags in rows:
        s.conn.execute(
            "INSERT INTO tasks(id, project_id, title, status, tags, updated_at) "
            "VALUES (?, ?, ?, 0, ?, '2026-05-29T00:00:00')",
            (tid, pid, title, json.dumps(tags) if tags else None),
        )


def test_get_task_tags_returns_list(tmp_path: Path) -> None:
    s = Store(tmp_path / "tasks.db"); s.init_schema()
    _seed(s, [("t1", "p1", "Buy milk", ["errand", "shopping"])])
    assert get_task_tags(s, "t1") == ["errand", "shopping"]


def test_get_task_tags_empty_when_no_tags(tmp_path: Path) -> None:
    s = Store(tmp_path / "tasks.db"); s.init_schema()
    _seed(s, [("t1", "p1", "No tags", [])])
    assert get_task_tags(s, "t1") == []


def test_get_task_tags_empty_when_task_missing(tmp_path: Path) -> None:
    s = Store(tmp_path / "tasks.db"); s.init_schema()
    assert get_task_tags(s, "nope") == []


def test_find_tasks_with_tag_returns_matching(tmp_path: Path) -> None:
    s = Store(tmp_path / "tasks.db"); s.init_schema()
    _seed(s, [
        ("t1", "p1", "A", ["work", "urgent"]),
        ("t2", "p1", "B", ["personal"]),
        ("t3", "p1", "C", ["work"]),
    ])
    rows = find_tasks_with_tag(s, "work")
    ids = sorted(r["id"] for r in rows)
    assert ids == ["t1", "t3"]
    by_id = {r["id"]: r for r in rows}
    assert by_id["t1"]["tags"] == ["work", "urgent"]
    assert by_id["t1"]["project_id"] == "p1"
    assert by_id["t1"]["title"] == "A"


def test_find_tasks_with_tag_case_sensitive_by_default(tmp_path: Path) -> None:
    """TickTick treats tags as exact strings — `Work` and `work` are
    distinct. Default match must respect that."""
    s = Store(tmp_path / "tasks.db"); s.init_schema()
    _seed(s, [
        ("t1", "p1", "A", ["Work"]),
        ("t2", "p1", "B", ["work"]),
    ])
    rows = find_tasks_with_tag(s, "work")
    assert [r["id"] for r in rows] == ["t2"]


def test_find_tasks_with_tag_ignore_case(tmp_path: Path) -> None:
    """ignore_case=True opts into case-folded matching across all variants
    of the tag — useful for cleanup operations where the user doesn't want
    to enumerate every capitalization."""
    s = Store(tmp_path / "tasks.db"); s.init_schema()
    _seed(s, [
        ("t1", "p1", "A", ["Work"]),
        ("t2", "p1", "B", ["work"]),
        ("t3", "p1", "C", ["WORK"]),
        ("t4", "p1", "D", ["other"]),
    ])
    rows = find_tasks_with_tag(s, "work", ignore_case=True)
    assert sorted(r["id"] for r in rows) == ["t1", "t2", "t3"]


def test_get_task_tags_round_trips_emoji(tmp_path: Path) -> None:
    """SQLite stores the tag list as a JSON string (`json.dumps` with
    ensure_ascii=True, the Python default — emojis go in as \\uXXXX
    surrogate pairs). The reader must materialize them back to literal
    code points so downstream string comparisons see what the user
    typed, not their escaped form."""
    s = Store(tmp_path / "tasks.db"); s.init_schema()
    _seed(s, [("t1", "p1", "Buy milk", ["🔥urgent", "work", "🚀launch"])])
    assert get_task_tags(s, "t1") == ["🔥urgent", "work", "🚀launch"]


def test_find_tasks_with_tag_matches_emoji_exactly(tmp_path: Path) -> None:
    """An emoji-bearing tag is an ordinary Python string — equality
    matching against another identical string works, and an emoji tag
    is distinct from any text tag (`🔥` ≠ `fire`)."""
    s = Store(tmp_path / "tasks.db"); s.init_schema()
    _seed(s, [
        ("t1", "p1", "A", ["🔥urgent", "work"]),
        ("t2", "p1", "B", ["fire", "work"]),
        ("t3", "p1", "C", ["🔥urgent"]),
    ])
    rows = find_tasks_with_tag(s, "🔥urgent")
    assert sorted(r["id"] for r in rows) == ["t1", "t3"]
    # And the matched rows surface the emoji tags exactly as stored,
    # not as escape sequences — callers need the literal string to
    # send back to TickTick on a rewrite (rename/delete sweeps).
    assert any("🔥urgent" in r["tags"] for r in rows)


def test_find_tasks_with_tag_skips_tasks_without_tags(tmp_path: Path) -> None:
    s = Store(tmp_path / "tasks.db"); s.init_schema()
    _seed(s, [
        ("t1", "p1", "A", []),
        ("t2", "p1", "B", ["work"]),
    ])
    rows = find_tasks_with_tag(s, "work")
    assert [r["id"] for r in rows] == ["t2"]
