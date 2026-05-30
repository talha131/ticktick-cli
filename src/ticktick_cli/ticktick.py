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
        repeat_flag: str | None = None,
    ) -> dict[str, Any]:
        """POST /open/v1/task. Returns the created task as TickTick returns it
        (includes the assigned task id and timestamps).

        `reminders` is a list of iCal TRIGGER strings, e.g.
        ["TRIGGER:-PT15M"] for "15 minutes before due". See
        format_trigger() to build these from a minutes-before integer.

        `repeat_flag` is a raw iCal RRULE string, e.g.
        "RRULE:FREQ=DAILY;INTERVAL=1". Passed through verbatim."""
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
        if repeat_flag is not None:
            payload["repeatFlag"] = repeat_flag
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
        repeat_flag: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """POST /open/v1/task/{taskId}. The body must include `id` and
        `projectId` (TickTick rejects partial updates without them). Any
        other field passed replaces its current value on the server.

        For each optional kwarg, `None` means "don't touch this field" and
        any other value (including `""` or `[]`) is sent through to
        explicitly set or clear it."""
        payload: dict[str, Any] = {"id": task_id, "projectId": project_id}
        if reminders is not None:
            payload["reminders"] = reminders
        if repeat_flag is not None:
            payload["repeatFlag"] = repeat_flag
        if tags is not None:
            payload["tags"] = tags
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

    def delete_task(self, project_id: str, task_id: str) -> None:
        """DELETE /open/v1/project/{project_id}/task/{task_id}. TickTick
        returns 200 with empty body on success.

        The Open API docs do not specify whether this is a soft delete
        (moves to Trash) or hard delete. TickTick's UI uses a Trash
        folder with 30-day retention; the API most likely follows the
        same path, but it's not contractually guaranteed."""
        r = httpx.delete(
            f"{self.base_url}/project/{project_id}/task/{task_id}",
            headers=self._headers(),
        )
        r.raise_for_status()

    def list_completed_tasks(
        self,
        *,
        project_ids: list[str] | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict[str, Any]]:
        """POST /open/v1/task/completed. Returns the array of completed
        Task objects directly (the endpoint responds with a JSON array,
        not an envelope).

        All filter fields are optional, but the API docs recommend
        sending at least one to keep the result set bounded. Dates are
        ISO 8601 with timezone offset, e.g. "2026-05-01T00:00:00+0000"
        — the same format TickTick uses everywhere else (dueDate,
        completedTime, etc.).

        This is the only documented way to retrieve historical
        completions; /open/v1/project/{id}/data returns active tasks
        only."""
        payload: dict[str, Any] = {}
        if project_ids is not None:
            payload["projectIds"] = project_ids
        if start_date is not None:
            payload["startDate"] = start_date
        if end_date is not None:
            payload["endDate"] = end_date
        r = httpx.post(
            f"{self.base_url}/task/completed",
            headers=self._headers(),
            json=payload,
        )
        r.raise_for_status()
        return r.json()

    def move_task(
        self,
        task_id: str,
        *,
        from_project_id: str,
        to_project_id: str,
    ) -> dict[str, Any]:
        """POST /open/v1/task/move. The endpoint takes an array of move ops
        and returns an array of {id, etag} results. We expose the single-task
        case (the CLI's move verb is one-at-a-time) and return the first
        element so callers don't have to unwrap a one-item list."""
        payload = [{
            "taskId": task_id,
            "fromProjectId": from_project_id,
            "toProjectId": to_project_id,
        }]
        r = httpx.post(f"{self.base_url}/task/move",
                       headers=self._headers(), json=payload)
        r.raise_for_status()
        # raise_for_status() only guards HTTP codes; a 200 with an empty
        # array would still IndexError. Return {} so callers get a
        # predictable shape regardless of API quirks.
        data = r.json()
        return data[0] if data else {}


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
