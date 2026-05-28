"""SQLite store: schema, pragmas, connection lifecycle. Spec §4.1.

Pragmas applied on every open: WAL journal, foreign keys ON, NORMAL synchronous.
Schema is idempotent (CREATE TABLE IF NOT EXISTS)."""

from __future__ import annotations
import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  slug TEXT NOT NULL,
  archived INTEGER DEFAULT 0,
  raw_json TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  project_id TEXT REFERENCES projects(id),
  title TEXT NOT NULL,
  content TEXT,
  status INTEGER NOT NULL,            -- 0=todo, 2=done, 3=archived (local)
  priority INTEGER,
  due_date TEXT,
  start_date TEXT,
  completed_at TEXT,
  tags TEXT,
  updated_at TEXT NOT NULL,
  raw_json TEXT
);

CREATE TABLE IF NOT EXISTS sync_state (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS local_signals (
  task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
  last_promoted_at TEXT,
  promotion_count INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_tasks_status_due ON tasks(status, due_date);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_tasks_completed ON tasks(status, completed_at DESC);
"""


class Store:
    def __init__(self, db_path: Path) -> None:
        self.path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self._apply_pragmas()

    def _apply_pragmas(self) -> None:
        cur = self.conn.cursor()
        result = cur.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if result != "wal":
            raise RuntimeError(f"WAL mode not set; got: {result!r}")
        cur.execute("PRAGMA foreign_keys = ON")
        cur.execute("PRAGMA synchronous = NORMAL")

    def init_schema(self) -> None:
        self.conn.executescript(_SCHEMA)
