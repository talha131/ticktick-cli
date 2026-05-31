"""TickTick Open API client.

Sync (not async) keeps composition simple; revisit if /sync becomes
latency-bound. Wraps the endpoints we actually use; see TickTick's
OpenAPI docs for the full surface."""

from __future__ import annotations
from typing import Any, Protocol
import httpx
import random
import sys
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

BASE_URL = "https://api.ticktick.com/open/v1"


def build_update_payload(
    task_id: str,
    *,
    project_id: str,
    title: str | None = None,
    content: str | None = None,
    due_date: str | None = None,
    start_date: str | None = None,
    priority: int | None = None,
    reminders: list[str] | None = None,
    repeat_flag: str | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Construct the JSON body for POST /open/v1/task/{taskId} without
    sending it. Exposed so dry-run paths in the CLI can preview what
    `update_task` would PATCH. Follows the same None-means-skip
    convention — see `update_task` for the field semantics."""
    payload: dict[str, Any] = {"id": task_id, "projectId": project_id}
    if title is not None:
        payload["title"] = title
    if content is not None:
        payload["content"] = content
    if due_date is not None:
        payload["dueDate"] = due_date
    if start_date is not None:
        payload["startDate"] = start_date
    if priority is not None:
        payload["priority"] = priority
    if reminders is not None:
        payload["reminders"] = reminders
    if repeat_flag is not None:
        payload["repeatFlag"] = repeat_flag
    if tags is not None:
        payload["tags"] = tags
    return payload


@dataclass(frozen=True)
class _RetryPolicy:
    """Retry behaviour for TickTickClient._request.

    schedule: base delays in seconds between attempts (initial→1, 1→2,
        2→3). Length determines the max retries — 3 entries = 3
        retries = 4 total attempts.
    jitter: each delay multiplied by uniform(1 - jitter, 1 + jitter).
    wall_clock_cap: cumulative elapsed seconds beyond which no further
        retry is attempted. Honoured by _compute_delay (returns None).
    """
    schedule: tuple[float, ...] = (0.5, 2.0, 8.0)
    jitter: float = 0.25
    wall_clock_cap: float = 13.0


def _compute_delay(
    policy: _RetryPolicy,
    *,
    attempt: int,
    elapsed: float,
    retry_after: float | None,
) -> float | None:
    """Compute the delay before the next retry, or None to abort.

    attempt is 1-indexed — attempt=1 means "we just finished the first
    try; how long to wait before the second?". The corresponding
    schedule entry is policy.schedule[attempt - 1].

    If retry_after is provided (from a 429 response), it overrides the
    schedule value but is still subject to BOTH the attempt-count cap
    and the wall_clock_cap. The attempt cap applies regardless of
    retry_after — spec §3.2 says max retries and wall-clock are
    "whichever fires first."
    """
    if attempt < 1 or attempt > len(policy.schedule):
        return None
    if retry_after is not None:
        candidate = float(retry_after)
    else:
        base = policy.schedule[attempt - 1]
        candidate = base * random.uniform(1.0 - policy.jitter, 1.0 + policy.jitter)
    if elapsed + candidate > policy.wall_clock_cap:
        return None
    return candidate


def _parse_retry_after(value: str | None) -> float | None:
    """Parse an HTTP Retry-After header into seconds-from-now.

    Per RFC 7231, the header is either a non-negative integer
    (seconds) or an HTTP-date. Returns None on malformed input —
    callers should fall back to the regular backoff schedule.
    """
    if value is None:
        return None
    s = value.strip()
    if not s:
        return None
    try:
        n = int(s)
        return float(max(0, n))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    seconds = dt.timestamp() - time.time()
    return max(0.0, seconds)


def _classify(exc: BaseException, method: str) -> bool:
    """Return True iff the given exception is a transient failure that
    we should retry for the given HTTP method.

    Policy (see spec §3.1):
    - Pre-send connection failures (ConnectError, ConnectTimeout)
      retry on every method, including POST — the server never saw
      the request, so replaying it is safe.
    - Post-send timeouts (ReadTimeout, WriteTimeout) retry only on
      GET and DELETE. POSTs do not retry because TickTick's behaviour
      on a replayed update is undocumented.
    - 429 retries on every method (with Retry-After honoured upstream).
    - 5xx retries on GET and DELETE only — POST might have processed.
    - Everything else (4xx ≠ 429, unknown exceptions) is not retried.
    """
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return True
    if isinstance(exc, (httpx.ReadTimeout, httpx.WriteTimeout)):
        return method in ("GET", "DELETE")
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == 429:
            return True
        if 500 <= code < 600:
            return method in ("GET", "DELETE")
        return False
    return False


def _emit_retry_warning(
    *,
    attempt: int,
    total: int,
    delay: float,
    exc: BaseException,
    retry_after_used: bool,
) -> None:
    """Emit one stderr line describing the retry decision. Mirrors the
    existing _resync_mirror_safe warning style: a single 'warning:' line
    so scripted callers can grep/ignore it without parsing JSON-on-stdout."""
    msg = str(exc)
    if len(msg) > 120:
        msg = msg[:117] + "..."
    suffix = " (server Retry-After)" if retry_after_used else ""
    print(
        f"warning: retry {attempt}/{total} after {delay:.1f}s{suffix} — "
        f"{type(exc).__name__}: {msg}",
        file=sys.stderr,
    )


class _AuthLike(Protocol):
    def get_access_token_sync(self) -> str: ...


class TickTickClient:
    def __init__(self, auth: _AuthLike, base_url: str = BASE_URL) -> None:
        self.auth = auth
        self.base_url = base_url

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.auth.get_access_token_sync()}"}

    def _request(
        self,
        method: str,
        url: str,
        *,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
        _policy: _RetryPolicy | None = None,
    ) -> httpx.Response:
        """Single HTTP entry point.

        Applies auth headers and retry-with-backoff per
        ``docs/superpowers/specs/2026-05-31-retry-with-backoff-design.md``:
        GET/DELETE retry on pre-send + post-send transient failures;
        POST retries only on pre-send. HTTP 429 retries on every
        method with ``Retry-After`` honoured. Wall-clock cap ~13s.
        On exhaustion, the original exception is raised unchanged.

        ``_policy`` is a test-only hook for injecting alternate
        schedules; production callers should leave it at the default.
        """
        policy = _policy or _RetryPolicy()
        attempt = 1  # how many tries have happened so far
        started = time.monotonic()
        while True:
            try:
                kwargs: dict[str, Any] = {"headers": self._headers()}
                if json is not None:
                    kwargs["json"] = json
                if params is not None:
                    kwargs["params"] = params
                if method == "GET":
                    r = httpx.get(url, **kwargs)
                elif method == "POST":
                    r = httpx.post(url, **kwargs)
                elif method == "DELETE":
                    r = httpx.delete(url, **kwargs)
                else:
                    raise ValueError(f"unsupported HTTP method: {method!r}")
                r.raise_for_status()
                return r
            except httpx.HTTPError as exc:
                if not _classify(exc, method):
                    raise
                # Compute the delay (handles wall-clock cap + Retry-After)
                retry_after: float | None = None
                if isinstance(exc, httpx.HTTPStatusError):
                    retry_after = _parse_retry_after(
                        exc.response.headers.get("Retry-After"),
                    )
                elapsed = time.monotonic() - started
                delay = _compute_delay(
                    policy,
                    attempt=attempt,
                    elapsed=elapsed,
                    retry_after=retry_after,
                )
                if delay is None:
                    raise
                _emit_retry_warning(
                    attempt=attempt,
                    total=len(policy.schedule),
                    delay=delay,
                    exc=exc,
                    retry_after_used=retry_after is not None,
                )
                time.sleep(delay)
                attempt += 1

    def list_projects(self) -> list[dict[str, Any]]:
        r = self._request("GET", f"{self.base_url}/project")
        return r.json()

    def get_project_data(self, project_id: str) -> dict[str, Any]:
        """Returns {project, tasks, columns}. NOTE: per TickTick's API, this
        endpoint returns only ACTIVE tasks — historical completions are
        served from POST /open/v1/task/completed."""
        r = self._request("GET", f"{self.base_url}/project/{project_id}/data")
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
        r = self._request("POST", f"{self.base_url}/task", json=payload)
        return r.json()

    def update_task(
        self,
        task_id: str,
        *,
        project_id: str,
        title: str | None = None,
        content: str | None = None,
        due_date: str | None = None,
        start_date: str | None = None,
        priority: int | None = None,
        reminders: list[str] | None = None,
        repeat_flag: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """POST /open/v1/task/{taskId}. The body must include `id` and
        `projectId` (TickTick rejects partial updates without them). Any
        other field passed replaces its current value on the server.

        For each optional kwarg, `None` means "don't touch this field" and
        any other value (including `""` or `[]`) is sent through to
        explicitly set or clear it.

        Caveat for date fields: TickTick's response to dueDate="" /
        startDate="" is not contractually documented. Empty-string is
        what we send for "clear this date" because it matches the
        repeat_flag="" / reminders=[] convention, but you should verify
        in the TickTick UI that a cleared date actually disappears
        (rather than becoming 1970-01-01). If empty-string doesn't
        clear the field server-side, this needs a different strategy."""
        payload = build_update_payload(
            task_id,
            project_id=project_id,
            title=title,
            content=content,
            due_date=due_date,
            start_date=start_date,
            priority=priority,
            reminders=reminders,
            repeat_flag=repeat_flag,
            tags=tags,
        )
        r = self._request(
            "POST", f"{self.base_url}/task/{task_id}", json=payload,
        )
        return r.json()

    def complete_task(self, project_id: str, task_id: str) -> None:
        """POST /open/v1/project/{project_id}/task/{task_id}/complete.
        TickTick returns 200 with empty body on success."""
        self._request(
            "POST",
            f"{self.base_url}/project/{project_id}/task/{task_id}/complete",
        )

    def delete_task(self, project_id: str, task_id: str) -> None:
        """DELETE /open/v1/project/{project_id}/task/{task_id}. TickTick
        returns 200 with empty body on success.

        The Open API docs do not specify whether this is a soft delete
        (moves to Trash) or hard delete. TickTick's UI uses a Trash
        folder with 30-day retention; the API most likely follows the
        same path, but it's not contractually guaranteed."""
        self._request(
            "DELETE",
            f"{self.base_url}/project/{project_id}/task/{task_id}",
        )

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
        r = self._request(
            "POST", f"{self.base_url}/task/completed", json=payload,
        )
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
        r = self._request(
            "POST", f"{self.base_url}/task/move", json=payload,
        )
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
