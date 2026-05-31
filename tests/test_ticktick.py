import pytest
import httpx
from ticktick_cli.ticktick import (
    TickTickClient,
    _classify,
    _RetryPolicy,
    _compute_delay,
    _parse_retry_after,
)


class StubAuth:
    def get_access_token_sync(self): return "fake-token"


def test_list_projects(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://api.ticktick.com/open/v1/project",
        json=[{"id": "p1", "name": "GCE / Teaching"},
              {"id": "p2", "name": "LMS Startup"}],
    )
    client = TickTickClient(auth=StubAuth())
    projects = client.list_projects()
    assert len(projects) == 2
    assert projects[0]["id"] == "p1"


def test_get_project_data_includes_completed(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://api.ticktick.com/open/v1/project/p1/data",
        json={
            "project": {"id": "p1", "name": "GCE"},
            "tasks": [
                {"id": "t1", "title": "Lecture prep", "status": 0,
                 "projectId": "p1", "modifiedTime": "2026-05-24T10:00:00+0000"},
                {"id": "t2", "title": "Grade quizzes", "status": 2,
                 "projectId": "p1", "completedTime": "2026-05-24T15:00:00+0000",
                 "modifiedTime": "2026-05-24T15:00:00+0000"},
            ],
        },
    )
    client = TickTickClient(auth=StubAuth())
    data = client.get_project_data("p1")
    assert len(data["tasks"]) == 2
    completed = [t for t in data["tasks"] if t["status"] == 2][0]
    assert completed["completedTime"].startswith("2026-05-24")


def test_auth_header_sent(httpx_mock) -> None:
    httpx_mock.add_response(url="https://api.ticktick.com/open/v1/project", json=[])
    client = TickTickClient(auth=StubAuth())
    client.list_projects()
    last = httpx_mock.get_request()
    assert last.headers["Authorization"] == "Bearer fake-token"


def test_create_task_posts_payload_and_returns_created(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task",
        json={"id": "newid", "projectId": "p1", "title": "Buy milk",
              "status": 0, "priority": 3, "modifiedTime": "2026-05-28T20:00:00+0000"},
    )
    client = TickTickClient(auth=StubAuth())
    created = client.create_task(
        project_id="p1", title="Buy milk", priority=3,
        due_date="2026-05-30T00:00:00+0000",
    )
    assert created["id"] == "newid"
    last = httpx_mock.get_request()
    import json as _json
    body = _json.loads(last.content)
    assert body == {
        "projectId": "p1",
        "title": "Buy milk",
        "priority": 3,
        "dueDate": "2026-05-30T00:00:00+0000",
    }


def test_create_task_omits_optional_fields_when_none(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task",
        json={"id": "x", "projectId": "p1", "title": "Minimal"},
    )
    client = TickTickClient(auth=StubAuth())
    client.create_task(project_id="p1", title="Minimal")
    import json as _json
    body = _json.loads(httpx_mock.get_request().content)
    assert set(body.keys()) == {"projectId", "title"}


def test_complete_task(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/project/p1/task/t1/complete",
        status_code=200,
    )
    client = TickTickClient(auth=StubAuth())
    client.complete_task("p1", "t1")  # no return value; no exception = success
    last = httpx_mock.get_request()
    assert last.headers["Authorization"] == "Bearer fake-token"


def test_delete_task(httpx_mock) -> None:
    httpx_mock.add_response(
        method="DELETE",
        url="https://api.ticktick.com/open/v1/project/p1/task/t1",
        status_code=200,
    )
    client = TickTickClient(auth=StubAuth())
    client.delete_task("p1", "t1")
    last = httpx_mock.get_request()
    assert last.method == "DELETE"
    assert last.headers["Authorization"] == "Bearer fake-token"


def test_move_task_posts_array_payload(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/move",
        json=[{"id": "t1", "etag": "abc"}],
    )
    client = TickTickClient(auth=StubAuth())
    client.move_task("t1", from_project_id="p1", to_project_id="p2")
    import json as _json
    body = _json.loads(httpx_mock.get_request().content)
    assert body == [
        {"taskId": "t1", "fromProjectId": "p1", "toProjectId": "p2"}
    ]


def test_move_task_returns_first_result(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/move",
        json=[{"id": "t1", "etag": "abc"}],
    )
    client = TickTickClient(auth=StubAuth())
    result = client.move_task("t1", from_project_id="p1", to_project_id="p2")
    assert result == {"id": "t1", "etag": "abc"}


def test_move_task_handles_empty_response_array(httpx_mock) -> None:
    """A 200 with an empty JSON array (transient API quirk, future shape
    change) must not raise IndexError. The contract for the caller is
    'returns a dict' — empty dict on no-result is the least surprising."""
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/move",
        json=[],
    )
    client = TickTickClient(auth=StubAuth())
    result = client.move_task("t1", from_project_id="p1", to_project_id="p2")
    assert result == {}


def test_create_task_includes_repeat_flag(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task",
        json={"id": "n", "projectId": "p1", "title": "Daily"},
    )
    client = TickTickClient(auth=StubAuth())
    client.create_task(project_id="p1", title="Daily",
                       repeat_flag="RRULE:FREQ=DAILY;INTERVAL=1")
    import json as _json
    body = _json.loads(httpx_mock.get_request().content)
    assert body["repeatFlag"] == "RRULE:FREQ=DAILY;INTERVAL=1"


def test_create_task_omits_repeat_flag_when_none(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task",
        json={"id": "n", "projectId": "p1", "title": "One-shot"},
    )
    client = TickTickClient(auth=StubAuth())
    client.create_task(project_id="p1", title="One-shot")
    import json as _json
    body = _json.loads(httpx_mock.get_request().content)
    assert "repeatFlag" not in body


def test_update_task_sets_repeat_flag(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={"id": "t1", "projectId": "p1"},
    )
    client = TickTickClient(auth=StubAuth())
    client.update_task("t1", project_id="p1",
                       repeat_flag="RRULE:FREQ=WEEKLY;BYDAY=MO")
    import json as _json
    body = _json.loads(httpx_mock.get_request().content)
    assert body["repeatFlag"] == "RRULE:FREQ=WEEKLY;BYDAY=MO"


def test_update_task_clears_repeat_flag_with_empty_string(httpx_mock) -> None:
    """Empty string is the explicit 'clear' value, distinct from None
    (which means 'leave it alone'). Matches the reminders=[] semantics."""
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={"id": "t1", "projectId": "p1"},
    )
    client = TickTickClient(auth=StubAuth())
    client.update_task("t1", project_id="p1", repeat_flag="")
    import json as _json
    body = _json.loads(httpx_mock.get_request().content)
    assert body["repeatFlag"] == ""


def test_update_task_omits_repeat_flag_when_none(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={"id": "t1", "projectId": "p1"},
    )
    client = TickTickClient(auth=StubAuth())
    client.update_task("t1", project_id="p1")
    import json as _json
    body = _json.loads(httpx_mock.get_request().content)
    assert "repeatFlag" not in body


def test_update_task_sets_tags(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={"id": "t1", "projectId": "p1"},
    )
    client = TickTickClient(auth=StubAuth())
    client.update_task("t1", project_id="p1", tags=["urgent", "work"])
    import json as _json
    body = _json.loads(httpx_mock.get_request().content)
    assert body["tags"] == ["urgent", "work"]


def test_update_task_clears_tags_with_empty_list(httpx_mock) -> None:
    """Empty list explicitly clears all tags. Distinct from None which
    means 'leave the tags field untouched on the server.'"""
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={"id": "t1", "projectId": "p1"},
    )
    client = TickTickClient(auth=StubAuth())
    client.update_task("t1", project_id="p1", tags=[])
    import json as _json
    body = _json.loads(httpx_mock.get_request().content)
    assert body["tags"] == []


def test_create_task_emoji_tag_in_payload(httpx_mock) -> None:
    """Emoji tags get serialized through httpx → JSON and arrive on the
    server side intact. httpx uses json.dumps internally (ensure_ascii=True
    by default, so the wire form is \\uXXXX surrogate pairs), but
    json.loads on the receiving end materializes the original code points.
    This test exercises that contract on the client send side."""
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task",
        json={"id": "n", "projectId": "p1", "title": "Pay bills",
              "tags": ["🔥urgent", "💰finance"]},
    )
    client = TickTickClient(auth=StubAuth())
    client.create_task(project_id="p1", title="Pay bills",
                       tags=["🔥urgent", "💰finance"])
    import json as _json
    body = _json.loads(httpx_mock.get_request().content)
    assert body["tags"] == ["🔥urgent", "💰finance"]


def test_update_task_emoji_tag_in_payload(httpx_mock) -> None:
    """Same emoji contract as create_task, but on the update path —
    rename/delete sweeps go through this one, so a mangled UTF-8 would
    silently corrupt every tag on every swept task."""
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={"id": "t1", "projectId": "p1"},
    )
    client = TickTickClient(auth=StubAuth())
    client.update_task("t1", project_id="p1", tags=["🚀launch", "work"])
    import json as _json
    body = _json.loads(httpx_mock.get_request().content)
    assert body["tags"] == ["🚀launch", "work"]


def test_list_completed_tasks_posts_filter_body(httpx_mock) -> None:
    """All three filter keys land in the request body verbatim, and the
    JSON array response is returned unwrapped."""
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/completed",
        json=[
            {"id": "c1", "projectId": "p1", "title": "Old thing",
             "status": 2, "completedTime": "2026-05-04T23:58:20.000+0000"},
            {"id": "c2", "projectId": "p1", "title": "Older thing",
             "status": 2, "completedTime": "2026-05-02T08:12:00.000+0000"},
        ],
    )
    client = TickTickClient(auth=StubAuth())
    out = client.list_completed_tasks(
        project_ids=["p1"],
        start_date="2026-05-01T00:00:00+0000",
        end_date="2026-05-30T23:59:59+0000",
    )
    assert len(out) == 2 and out[0]["id"] == "c1"
    import json as _json
    body = _json.loads(httpx_mock.get_request().content)
    assert body == {
        "projectIds": ["p1"],
        "startDate": "2026-05-01T00:00:00+0000",
        "endDate": "2026-05-30T23:59:59+0000",
    }


def test_list_completed_tasks_omits_optional_fields_when_none(httpx_mock) -> None:
    """Passing no filters sends an empty body — the API accepts it and
    we don't want to invent a default range here (the caller decides)."""
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/completed",
        json=[],
    )
    client = TickTickClient(auth=StubAuth())
    client.list_completed_tasks()
    import json as _json
    body = _json.loads(httpx_mock.get_request().content)
    assert body == {}


def test_update_task_omits_tags_when_none(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={"id": "t1", "projectId": "p1"},
    )
    client = TickTickClient(auth=StubAuth())
    client.update_task("t1", project_id="p1")
    import json as _json
    body = _json.loads(httpx_mock.get_request().content)
    assert "tags" not in body


def test_update_task_sets_title(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={"id": "t1", "projectId": "p1"},
    )
    client = TickTickClient(auth=StubAuth())
    client.update_task("t1", project_id="p1", title="Renamed")
    import json as _json
    body = _json.loads(httpx_mock.get_request().content)
    assert body["title"] == "Renamed"


def test_update_task_sets_content(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={"id": "t1", "projectId": "p1"},
    )
    client = TickTickClient(auth=StubAuth())
    client.update_task("t1", project_id="p1", content="New notes")
    import json as _json
    body = _json.loads(httpx_mock.get_request().content)
    assert body["content"] == "New notes"


def test_update_task_sets_due_date(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={"id": "t1", "projectId": "p1"},
    )
    client = TickTickClient(auth=StubAuth())
    client.update_task("t1", project_id="p1",
                       due_date="2026-06-15T15:00:00+0000")
    import json as _json
    body = _json.loads(httpx_mock.get_request().content)
    assert body["dueDate"] == "2026-06-15T15:00:00+0000"


def test_update_task_sets_start_date(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={"id": "t1", "projectId": "p1"},
    )
    client = TickTickClient(auth=StubAuth())
    client.update_task("t1", project_id="p1",
                       start_date="2026-06-15T09:00:00+0000")
    import json as _json
    body = _json.loads(httpx_mock.get_request().content)
    assert body["startDate"] == "2026-06-15T09:00:00+0000"


def test_update_task_sets_priority(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={"id": "t1", "projectId": "p1"},
    )
    client = TickTickClient(auth=StubAuth())
    client.update_task("t1", project_id="p1", priority=5)
    import json as _json
    body = _json.loads(httpx_mock.get_request().content)
    assert body["priority"] == 5


def test_update_task_clears_dates_with_empty_string(httpx_mock) -> None:
    """Empty string is the explicit 'clear' value, matching the
    reminders=[] / repeat_flag='' convention. NOTE: TickTick's response
    to this on the server side is not contractually documented — the
    user must verify manually that the field actually disappears in the
    UI (not just becomes 1970-01-01). If TickTick rejects empty-string
    for dates, switch to a different strategy and update this test."""
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={"id": "t1", "projectId": "p1"},
    )
    client = TickTickClient(auth=StubAuth())
    client.update_task("t1", project_id="p1", due_date="", start_date="")
    import json as _json
    body = _json.loads(httpx_mock.get_request().content)
    assert body["dueDate"] == ""
    assert body["startDate"] == ""


def test_update_task_omits_new_fields_when_none(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={"id": "t1", "projectId": "p1"},
    )
    client = TickTickClient(auth=StubAuth())
    client.update_task("t1", project_id="p1")
    import json as _json
    body = _json.loads(httpx_mock.get_request().content)
    for k in ("title", "content", "dueDate", "startDate", "priority"):
        assert k not in body, f"{k!r} should be omitted when None"


# ---------------------------------------------------------------------------
# Task 2.1: _classify tests
# ---------------------------------------------------------------------------

def _make_status_error(status_code: int) -> httpx.HTTPStatusError:
    """Build a real HTTPStatusError for a given status code so _classify
    can read response.status_code off it."""
    req = httpx.Request("GET", "https://example.invalid/")
    resp = httpx.Response(status_code=status_code, request=req)
    return httpx.HTTPStatusError(
        f"HTTP {status_code}", request=req, response=resp,
    )


@pytest.mark.parametrize(
    "exc,method,expected",
    [
        # Pre-send failures — retry on all methods including POST
        (httpx.ConnectError("dns"), "GET", True),
        (httpx.ConnectError("dns"), "POST", True),
        (httpx.ConnectError("dns"), "DELETE", True),
        (httpx.ConnectTimeout("tls"), "GET", True),
        (httpx.ConnectTimeout("tls"), "POST", True),
        (httpx.ConnectTimeout("tls"), "DELETE", True),
        # Post-send (ambiguous) — retry GET/DELETE only
        (httpx.ReadTimeout("slow"), "GET", True),
        (httpx.ReadTimeout("slow"), "DELETE", True),
        (httpx.ReadTimeout("slow"), "POST", False),
        (httpx.WriteTimeout("slow"), "GET", True),
        (httpx.WriteTimeout("slow"), "POST", False),
        (httpx.WriteTimeout("slow"), "DELETE", True),
        # 429 — retry on all methods
        (_make_status_error(429), "GET", True),
        (_make_status_error(429), "POST", True),
        (_make_status_error(429), "DELETE", True),
        # 5xx — retry GET/DELETE only (POST might have processed)
        (_make_status_error(500), "GET", True),
        (_make_status_error(500), "POST", False),
        (_make_status_error(502), "DELETE", True),
        (_make_status_error(503), "GET", True),
        (_make_status_error(504), "POST", False),
        # 4xx other than 429 — never retry
        (_make_status_error(400), "GET", False),
        (_make_status_error(401), "POST", False),
        (_make_status_error(404), "GET", False),
        # Unknown exception type — re-raise (never retry)
        (RuntimeError("unexpected"), "GET", False),
    ],
)
def test_classify_decides_whether_to_retry(exc, method, expected):
    assert _classify(exc, method) is expected


# ---------------------------------------------------------------------------
# Task 2.2: _RetryPolicy and _compute_delay tests
# ---------------------------------------------------------------------------

def test_retry_policy_defaults():
    p = _RetryPolicy()
    assert p.schedule == (0.5, 2.0, 8.0)
    assert p.jitter == 0.25
    assert p.wall_clock_cap == 13.0


def test_compute_delay_uses_schedule_with_jitter(monkeypatch):
    # Force the jitter multiplier to a known value
    monkeypatch.setattr(
        "ticktick_cli.ticktick.random.uniform", lambda lo, hi: 1.0,
    )
    p = _RetryPolicy()
    assert _compute_delay(p, attempt=1, elapsed=0.0, retry_after=None) == 0.5
    assert _compute_delay(p, attempt=2, elapsed=0.5, retry_after=None) == 2.0
    assert _compute_delay(p, attempt=3, elapsed=2.5, retry_after=None) == 8.0


def test_compute_delay_applies_jitter_range(monkeypatch):
    captured = {}

    def fake_uniform(lo, hi):
        captured["lo"] = lo
        captured["hi"] = hi
        return 1.1
    monkeypatch.setattr(
        "ticktick_cli.ticktick.random.uniform", fake_uniform,
    )
    p = _RetryPolicy()
    delay = _compute_delay(p, attempt=1, elapsed=0.0, retry_after=None)
    assert captured == {"lo": 0.75, "hi": 1.25}
    assert delay == pytest.approx(0.5 * 1.1)


def test_compute_delay_honors_retry_after():
    p = _RetryPolicy()
    # When retry_after is set, it overrides the schedule regardless of attempt
    assert _compute_delay(p, attempt=1, elapsed=0.0, retry_after=7.0) == 7.0
    assert _compute_delay(p, attempt=2, elapsed=2.0, retry_after=3.5) == 3.5


def test_compute_delay_returns_none_when_wall_clock_cap_would_overrun(
    monkeypatch,
):
    monkeypatch.setattr(
        "ticktick_cli.ticktick.random.uniform", lambda lo, hi: 1.0,
    )
    p = _RetryPolicy()
    # Already 10s in; the 8s third-attempt delay would push us to 18s
    # past the 13s cap — return None to signal "do not retry further".
    assert _compute_delay(p, attempt=3, elapsed=10.0, retry_after=None) is None


def test_compute_delay_retry_after_also_subject_to_cap():
    p = _RetryPolicy()
    # 12s elapsed + a 60s Retry-After would exceed 13s cap — bail
    assert _compute_delay(p, attempt=1, elapsed=12.0, retry_after=60.0) is None


# ---------------------------------------------------------------------------
# Task 2.3: _parse_retry_after tests
# ---------------------------------------------------------------------------

def test_parse_retry_after_integer_seconds():
    assert _parse_retry_after("7") == 7.0
    assert _parse_retry_after("0") == 0.0
    assert _parse_retry_after("  3  ") == 3.0  # tolerate whitespace


def test_parse_retry_after_http_date(monkeypatch):
    # Freeze time so the "seconds until" math is deterministic
    import ticktick_cli.ticktick as ttmod
    monkeypatch.setattr(ttmod.time, "time", lambda: 1717000000.0)
    # HTTP-date 5 seconds after our frozen now: 2024-05-29T16:26:45 UTC
    # (1717000005 = 1717000000 + 5)
    val = _parse_retry_after("Wed, 29 May 2024 16:26:45 +0000")
    assert val == pytest.approx(5.0, abs=1.0)


def test_parse_retry_after_invalid_returns_none():
    assert _parse_retry_after("not a date") is None
    assert _parse_retry_after("") is None
    assert _parse_retry_after(None) is None


def test_parse_retry_after_negative_returns_zero():
    """RFC 7231 doesn't forbid negative integers as a degenerate case
    (a date in the past). We treat them as 'retry immediately' rather
    than raising."""
    assert _parse_retry_after("-5") == 0.0


# ---------------------------------------------------------------------------
# Task 2.4: End-to-end retry-loop tests
# ---------------------------------------------------------------------------

@pytest.fixture
def no_sleep(monkeypatch):
    """Replace time.sleep with a recording stub so retry tests don't
    actually wait. Returns the list of slept durations for assertions."""
    slept: list[float] = []
    monkeypatch.setattr(
        "ticktick_cli.ticktick.time.sleep", lambda s: slept.append(s),
    )
    return slept


@pytest.fixture
def no_jitter(monkeypatch):
    """Pin jitter to 1.0 so retry tests use exact schedule values."""
    monkeypatch.setattr(
        "ticktick_cli.ticktick.random.uniform", lambda lo, hi: 1.0,
    )


def test_get_retries_on_read_timeout_then_succeeds(
    httpx_mock, no_sleep, no_jitter,
):
    httpx_mock.add_exception(httpx.ReadTimeout("slow response"))
    httpx_mock.add_response(
        url="https://api.ticktick.com/open/v1/project", json=[{"id": "p1"}],
    )
    client = TickTickClient(auth=StubAuth())
    out = client.list_projects()
    assert out == [{"id": "p1"}]
    # Two attempts (one failure + one success); one sleep in between
    assert len(no_sleep) == 1
    assert no_sleep[0] == pytest.approx(0.5)


def test_post_does_not_retry_on_read_timeout(httpx_mock, no_sleep, no_jitter):
    httpx_mock.add_exception(httpx.ReadTimeout("slow server"))
    client = TickTickClient(auth=StubAuth())
    with pytest.raises(httpx.ReadTimeout):
        client.update_task("t1", project_id="p1", title="x")
    # POST + ReadTimeout = no retry; no sleeps
    assert no_sleep == []


def test_post_retries_on_connect_error(httpx_mock, no_sleep, no_jitter):
    httpx_mock.add_exception(httpx.ConnectError("dns fail"))
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={"id": "t1", "projectId": "p1"},
    )
    client = TickTickClient(auth=StubAuth())
    client.update_task("t1", project_id="p1", title="x")
    assert len(no_sleep) == 1


def test_429_with_retry_after_uses_header_value(
    httpx_mock, no_sleep, no_jitter,
):
    httpx_mock.add_response(
        url="https://api.ticktick.com/open/v1/project",
        status_code=429,
        headers={"Retry-After": "3"},
    )
    httpx_mock.add_response(
        url="https://api.ticktick.com/open/v1/project", json=[],
    )
    client = TickTickClient(auth=StubAuth())
    client.list_projects()
    assert len(no_sleep) == 1
    assert no_sleep[0] == 3.0  # not the 0.5s schedule


def test_retry_budget_exhaustion_raises_original(
    httpx_mock, no_sleep, no_jitter,
):
    # Four consecutive ConnectTimeouts — should exhaust 3 retries and raise
    for _ in range(4):
        httpx_mock.add_exception(httpx.ConnectTimeout("flaky"))
    client = TickTickClient(auth=StubAuth())
    with pytest.raises(httpx.ConnectTimeout):
        client.list_projects()
    # 3 retries = 3 sleeps
    assert len(no_sleep) == 3
    assert no_sleep == [pytest.approx(0.5), pytest.approx(2.0), pytest.approx(8.0)]


def test_get_retries_on_503(httpx_mock, no_sleep, no_jitter):
    httpx_mock.add_response(
        url="https://api.ticktick.com/open/v1/project", status_code=503,
    )
    httpx_mock.add_response(
        url="https://api.ticktick.com/open/v1/project", json=[],
    )
    client = TickTickClient(auth=StubAuth())
    client.list_projects()
    assert len(no_sleep) == 1


def test_post_does_not_retry_on_503(httpx_mock, no_sleep, no_jitter):
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task",
        status_code=503,
    )
    client = TickTickClient(auth=StubAuth())
    with pytest.raises(httpx.HTTPStatusError):
        client.create_task(project_id="p1", title="x")
    assert no_sleep == []


def test_4xx_not_retried(httpx_mock, no_sleep, no_jitter):
    httpx_mock.add_response(
        url="https://api.ticktick.com/open/v1/project", status_code=401,
    )
    client = TickTickClient(auth=StubAuth())
    with pytest.raises(httpx.HTTPStatusError):
        client.list_projects()
    assert no_sleep == []


def test_retry_warning_logged_to_stderr(
    httpx_mock, no_sleep, no_jitter, capsys,
):
    httpx_mock.add_exception(httpx.ConnectTimeout("tls"))
    httpx_mock.add_response(
        url="https://api.ticktick.com/open/v1/project", json=[],
    )
    client = TickTickClient(auth=StubAuth())
    client.list_projects()
    captured = capsys.readouterr()
    # One retry → one stderr line; stdout untouched
    assert "retry 1/3" in captured.err
    assert "ConnectTimeout" in captured.err
    assert captured.out == ""


def test_request_stops_retrying_when_wall_clock_cap_would_overrun(
    httpx_mock, no_sleep, no_jitter, monkeypatch,
):
    """Even with retries remaining on the attempt counter, _request must
    bail when the next computed delay would push cumulative elapsed
    past the wall-clock cap. We fake time.monotonic to simulate
    long-running attempts."""
    # Tight policy so the cap fires after exactly one retry, regardless
    # of the schedule values themselves. Wall-clock cap of 0.6s with a
    # schedule of (0.5, 2.0, 8.0): first delay (0.5) fits; the second
    # delay (2.0) cannot fit within 0.6s — abort.
    policy = _RetryPolicy(
        schedule=(0.5, 2.0, 8.0), jitter=0.25, wall_clock_cap=0.6,
    )

    # Three consecutive ConnectTimeouts would otherwise exhaust the
    # full retry budget; the cap should cut us off sooner. Register them
    # as optional so pytest-httpx does not fail when only two are consumed
    # (the cap aborts before the third attempt fires).
    for _ in range(3):
        httpx_mock.add_exception(httpx.ConnectTimeout("flaky"), is_optional=True)

    # Make time.monotonic advance enough between attempts that the
    # second retry's computed delay (2.0s base) + elapsed exceeds 0.6s.
    fake_clock = iter([0.0, 0.0, 0.1, 0.3, 0.5, 0.7, 0.9])
    monkeypatch.setattr(
        "ticktick_cli.ticktick.time.monotonic", lambda: next(fake_clock),
    )

    client = TickTickClient(auth=StubAuth())
    with pytest.raises(httpx.ConnectTimeout):
        client._request(
            "GET",
            "https://api.ticktick.com/open/v1/project",
            _policy=policy,
        )

    # Exactly one sleep (the first 0.5s retry); the second retry was
    # aborted by the wall-clock cap.
    assert len(no_sleep) == 1
    assert no_sleep[0] == pytest.approx(0.5)


def test_compute_delay_attempt_cap_applies_even_with_retry_after():
    """Spec §3.2: max retries cap fires regardless of Retry-After. A
    persistent stream of 429s with small Retry-After values must not
    extend the retry loop past len(schedule) attempts."""
    p = _RetryPolicy()
    # attempt=4 is past the 3-entry schedule; even with a tiny
    # Retry-After that would otherwise fit, return None to abort.
    assert _compute_delay(
        p, attempt=4, elapsed=0.0, retry_after=1.0,
    ) is None
    assert _compute_delay(
        p, attempt=5, elapsed=0.0, retry_after=0.1,
    ) is None


def test_429_with_retry_after_still_capped_by_attempt_count(
    httpx_mock, no_sleep, no_jitter,
):
    """Four consecutive 429s with Retry-After: 1 should NOT result in
    four retries — the schedule has 3 entries, so the 4th attempt
    failing exhausts the budget and raises the original HTTPStatusError."""
    for _ in range(4):
        httpx_mock.add_response(
            url="https://api.ticktick.com/open/v1/project",
            status_code=429,
            headers={"Retry-After": "1"},
        )
    client = TickTickClient(auth=StubAuth())
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        client.list_projects()
    assert exc_info.value.response.status_code == 429
    # 3 retries = 3 sleeps, each using the Retry-After value of 1.0s
    assert len(no_sleep) == 3
    assert no_sleep == [1.0, 1.0, 1.0]
