"""Candidate task query. Spec §6.

Parameterized SQL — project names are user-controlled, never concatenated.
NULL due_date sorts AFTER real dates (SQLite default puts NULL first)."""

from __future__ import annotations
from typing import Any
from .store import Store


def list_candidates(
    store: Store,
    excluded_project_ids: list[str],
    now_iso: str,
    limit: int = 60,
) -> list[dict[str, Any]]:
    placeholders = ",".join("?" * len(excluded_project_ids)) if excluded_project_ids else "''"
    excluded_clause = f"AND p.id NOT IN ({placeholders})" if excluded_project_ids else ""
    sql = f"""
        SELECT t.* FROM tasks t
        JOIN projects p ON t.project_id = p.id
        WHERE t.status = 0
          AND p.archived = 0
          {excluded_clause}
          AND (t.start_date IS NULL OR t.start_date <= ?)
        ORDER BY
          t.priority DESC,
          CASE WHEN t.due_date IS NULL THEN 1 ELSE 0 END,
          t.due_date ASC
        LIMIT ?
    """
    params = (*excluded_project_ids, now_iso, limit)
    return [dict(r) for r in store.conn.execute(sql, params)]
