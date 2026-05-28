"""TickTick Open API client.

Sync (not async) keeps composition simple; revisit if /sync becomes
latency-bound. Wraps the endpoints we actually use; see TickTick's
OpenAPI docs for the full surface."""

from __future__ import annotations
from typing import Any, Protocol
import httpx

BASE_URL = "https://api.ticktick.com/open/v1"


class _AuthLike(Protocol):
    def get_access_token_sync(self) -> str: ...


class TickTickClient:
    def __init__(self, auth: _AuthLike, base_url: str = BASE_URL) -> None:
        self.auth = auth
        self.base_url = base_url

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.auth.get_access_token_sync()}"}

    def list_projects(self) -> list[dict[str, Any]]:
        r = httpx.get(f"{self.base_url}/project", headers=self._headers())
        r.raise_for_status()
        return r.json()

    def get_project_data(self, project_id: str) -> dict[str, Any]:
        """Returns {project, tasks, columns}. NOTE: per TickTick's API, this
        endpoint returns only ACTIVE tasks — historical completions are
        served from POST /open/v1/task/completed."""
        r = httpx.get(f"{self.base_url}/project/{project_id}/data",
                      headers=self._headers())
        r.raise_for_status()
        return r.json()

    def create_task(
        self,
        *,
        project_id: str,
        title: str,
        content: str | None = None,
        priority: int | None = None,
        due_date: str | None = None,
        tags: list[str] | None = None,
        reminders: list[str] | None = None,
    ) -> dict[str, Any]:
        """POST /open/v1/task. Returns the created task as TickTick returns it
        (includes the assigned task id and timestamps).

        `reminders` is a list of iCal TRIGGER strings, e.g.
        ["TRIGGER:-PT15M"] for "15 minutes before due". See
        format_trigger() to build these from a minutes-before integer."""
        payload: dict[str, Any] = {"projectId": project_id, "title": title}
        if content is not None:
            payload["content"] = content
        if priority is not None:
            payload["priority"] = priority
        if due_date is not None:
            payload["dueDate"] = due_date
        if tags:
            payload["tags"] = tags
        if reminders:
            payload["reminders"] = reminders
        r = httpx.post(f"{self.base_url}/task", headers=self._headers(),
                       json=payload)
        r.raise_for_status()
        return r.json()

    def update_task(
        self,
        task_id: str,
        *,
        project_id: str,
        reminders: list[str] | None = None,
    ) -> dict[str, Any]:
        """POST /open/v1/task/{taskId}. The body must include `id` and
        `projectId` (TickTick rejects partial updates without them). Any
        other field passed replaces its current value on the server.

        Today we only support setting `reminders`; extend this method as
        we expose more mutations through the CLI."""
        payload: dict[str, Any] = {"id": task_id, "projectId": project_id}
        if reminders is not None:
            payload["reminders"] = reminders
        r = httpx.post(
            f"{self.base_url}/task/{task_id}",
            headers=self._headers(),
            json=payload,
        )
        r.raise_for_status()
        return r.json()

    def complete_task(self, project_id: str, task_id: str) -> None:
        """POST /open/v1/project/{project_id}/task/{task_id}/complete.
        TickTick returns 200 with empty body on success."""
        r = httpx.post(
            f"{self.base_url}/project/{project_id}/task/{task_id}/complete",
            headers=self._headers(),
        )
        r.raise_for_status()


# ---- iCal TRIGGER helpers --------------------------------------------------

# TickTick reminders are iCal TRIGGER strings (RFC 5545):
#   - "TRIGGER:-PT15M"  → 15 minutes BEFORE the task time (negative = before)
#   - "TRIGGER:PT0S"    → at the task time
#   - "TRIGGER:-P1DT2H" → 1 day 2 hours before
# TickTick's GET responses sometimes show positive-duration examples like
# "TRIGGER:P0DT9H0M0S" (9 hours AFTER start — used by all-day tasks for
# morning reminders). When we WRITE reminders we use the negative form,
# because "remind me N minutes before due" is the dominant CLI use case.


def format_trigger(minutes_before: int) -> str:
    """Build an iCal TRIGGER for N minutes before the task time.

    Returns "TRIGGER:PT0S" for 0 (at the task time) and
    "TRIGGER:-PT{n}M" otherwise. Negative input is rejected — pass a
    non-negative integer; add an "after" helper later if needed."""
    if minutes_before < 0:
        raise ValueError(
            f"minutes_before must be >= 0 (got {minutes_before}). "
            f"Use 0 for at-due."
        )
    if minutes_before == 0:
        return "TRIGGER:PT0S"
    return f"TRIGGER:-PT{minutes_before}M"


_DURATION_UNITS = {"m": 1, "h": 60, "d": 1440}


def parse_duration(spec: str) -> int:
    """Parse a CLI duration like '15m', '1h', '2d' into minutes.

    Accepted forms: <int><unit> where unit is m/h/d. Also accepts plain
    integers (interpreted as minutes), '0', and the literal 'at-due'
    (both → 0 minutes, i.e. fire at the task's due time)."""
    s = spec.strip().lower()
    if s in ("at-due", "0"):
        return 0
    if s and s[-1] in _DURATION_UNITS:
        try:
            n = int(s[:-1])
        except ValueError as e:
            raise ValueError(f"Invalid duration: {spec!r}") from e
        return n * _DURATION_UNITS[s[-1]]
    try:
        return int(s)
    except ValueError as e:
        raise ValueError(
            f"Invalid duration: {spec!r}. Expected forms: '15m', '1h', "
            f"'2d', or 'at-due'."
        ) from e
