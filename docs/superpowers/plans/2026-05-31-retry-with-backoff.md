# Retry-with-backoff Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add bounded retry-with-backoff to `TickTickClient`'s HTTP methods so transient TickTick edge failures (TLS handshake timeouts, DNS errors, HTTP 429) self-recover within a ~13-second budget per call instead of failing the calling command.

**Architecture:** All eight public methods of `TickTickClient` are refactored to delegate to a new private `_request(method, url, *, json=None, params=None)` helper. The helper applies a method-aware retry policy: GET/DELETE retry on both pre-send (`ConnectError`/`ConnectTimeout`) and post-send (`ReadTimeout`, 5xx) transient failures; POST retries only on pre-send. All methods retry on HTTP 429 with `Retry-After` honored. Backoff schedule is 0.5s/2s/8s ±25% jitter, max 3 retries, total wall-clock cap ~13s.

**Tech Stack:** Python 3.12, `httpx` (already a project dep), `pytest` + `pytest-httpx` (already used by `tests/test_ticktick.py`), stdlib `random`, `time`, `sys`, `email.utils` (for HTTP-date parsing in `Retry-After`).

**Spec:** `docs/superpowers/specs/2026-05-31-retry-with-backoff-design.md`

---

## File map

- **Modify** `src/ticktick_cli/ticktick.py` (currently ~302 lines) — add `_RetryPolicy`, `_classify`, `_compute_delay`, `_emit_retry_warning`, `TickTickClient._request`. Refactor 8 public methods to delegate.
- **Modify** `tests/test_ticktick.py` — add ~10 new tests; existing 28 tests must continue passing unchanged.
- **Modify** `README.md` — add a "Reliability" section explaining the retry budget and stderr warnings.
- **Modify** `CLAUDE.md` (project) — add a one-paragraph entry to "Known quirks" pointing at this spec, and a one-line note in the existing testing section about `time.sleep` monkeypatch for retry tests.
- **Modify** `memory/MEMORY.md` and `memory/debate_2026-05-31_retry_scoping.md` — flip "Next step" note from "pending" to "shipped" with commit refs.

No new modules, no new files.

---

## Commit 1 — Pure refactor: extract `_request`

### Task 1.1: Add `_request` helper without retry logic

**Files:**
- Modify: `src/ticktick_cli/ticktick.py:55-247` (the `TickTickClient` class body)

- [ ] **Step 1: Read the current class.** Open `src/ticktick_cli/ticktick.py:55-247`. Note that every public method follows the pattern: `r = httpx.<verb>(url, headers=self._headers(), [json=...])`, `r.raise_for_status()`, `return r.json()` (or no return for void endpoints).

- [ ] **Step 2: Add the helper method directly after `_headers`.** Insert this method at `ticktick.py:62` (i.e. right after `_headers` and before `list_projects`):

```python
    def _request(
        self,
        method: str,
        url: str,
        *,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Single HTTP entry point for the TickTick client.

        For now this is a pure refactor — all eight public methods
        funnel through here without behaviour change. The retry policy
        is layered on in a later commit; see
        ``docs/superpowers/specs/2026-05-31-retry-with-backoff-design.md``
        for the design."""
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
```

- [ ] **Step 3: Refactor `list_projects` to delegate.** Replace lines 63-66 with:

```python
    def list_projects(self) -> list[dict[str, Any]]:
        r = self._request("GET", f"{self.base_url}/project")
        return r.json()
```

- [ ] **Step 4: Refactor `get_project_data` to delegate.** Replace the body (lines 68-75) keeping the existing docstring:

```python
    def get_project_data(self, project_id: str) -> dict[str, Any]:
        """Returns {project, tasks, columns}. NOTE: per TickTick's API, this
        endpoint returns only ACTIVE tasks — historical completions are
        served from POST /open/v1/task/completed."""
        r = self._request("GET", f"{self.base_url}/project/{project_id}/data")
        return r.json()
```

- [ ] **Step 5: Refactor `create_task` to delegate.** Keep the payload-building logic, replace the `httpx.post` block (current line 111-114) with:

```python
        r = self._request("POST", f"{self.base_url}/task", json=payload)
        return r.json()
```

- [ ] **Step 6: Refactor `update_task` to delegate.** Replace the `httpx.post` block (current lines 157-163) with:

```python
        r = self._request(
            "POST", f"{self.base_url}/task/{task_id}", json=payload,
        )
        return r.json()
```

- [ ] **Step 7: Refactor `complete_task` to delegate.** Replace the body (lines 165-172) keeping the existing docstring:

```python
    def complete_task(self, project_id: str, task_id: str) -> None:
        """POST /open/v1/project/{project_id}/task/{task_id}/complete.
        TickTick returns 200 with empty body on success."""
        self._request(
            "POST",
            f"{self.base_url}/project/{project_id}/task/{task_id}/complete",
        )
```

- [ ] **Step 8: Refactor `delete_task` to delegate.** Replace the body (lines 174-186) keeping the existing docstring:

```python
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
```

- [ ] **Step 9: Refactor `list_completed_tasks` to delegate.** Replace the `httpx.post` block (current lines 215-221) with:

```python
        r = self._request(
            "POST", f"{self.base_url}/task/completed", json=payload,
        )
        return r.json()
```

- [ ] **Step 10: Refactor `move_task` to delegate.** Replace the `httpx.post` block (current lines 239-246) with:

```python
        r = self._request(
            "POST", f"{self.base_url}/task/move", json=payload,
        )
        data = r.json()
        return data[0] if data else {}
```

- [ ] **Step 11: Run the existing test suite to verify zero behavior change.**

Run: `uv run pytest tests/test_ticktick.py -v`

Expected: all 28 existing tests pass. **If any test fails, fix the refactor — DO NOT modify the test.** The behavior-preserving guarantee is the whole point of this commit.

- [ ] **Step 12: Run the full test suite as a smoke check.**

Run: `uv run pytest -q`

Expected: full suite passes (no test in any other file depends on TickTickClient internals).

- [ ] **Step 13: Commit.**

```bash
git add src/ticktick_cli/ticktick.py
git commit -m "$(cat <<'EOF'
Extract TickTickClient._request helper

Pure mechanical refactor: route all eight public methods through a
single private _request(method, url, *, json, params) helper that
applies headers and raise_for_status. Zero behaviour change — the
existing test suite passes unchanged. Sets up the next commit, which
layers retry-with-backoff onto _request without touching the public
surface or any call site outside this file.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
git push origin master
```

---

## Commit 2 — Add retry loop with TDD

This commit is TDD-shaped: write each test, run it red, implement the minimum to make it green, repeat. The retry helpers (`_RetryPolicy`, `_classify`, `_compute_delay`) are tested directly via their underscore-prefixed names (Python allows importing private symbols by name; the leading underscore is convention, not a hard barrier).

### Task 2.1: Define `_RetryPolicy` constants and `_classify`

**Files:**
- Modify: `src/ticktick_cli/ticktick.py` — add module-private symbols above `class TickTickClient`
- Modify: `tests/test_ticktick.py` — add classification tests

- [ ] **Step 1: Write the classify test cases first.** Append to `tests/test_ticktick.py`:

Make sure the top of `tests/test_ticktick.py` has `import pytest` — it doesn't currently. Add `import pytest` near the existing `import httpx` line.

Then append:

```python
from ticktick_cli.ticktick import _classify


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
```

- [ ] **Step 2: Run the test — should fail because `_classify` is not defined.**

Run: `uv run pytest tests/test_ticktick.py::test_classify_decides_whether_to_retry -v`

Expected: ImportError / collection error: `cannot import name '_classify'`.

- [ ] **Step 3: Implement `_classify` in `ticktick.py`.** Insert above `class _AuthLike(Protocol):` (around line 50):

```python
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
```

- [ ] **Step 4: Run the classify test — should be green now.**

Run: `uv run pytest tests/test_ticktick.py::test_classify_decides_whether_to_retry -v`

Expected: PASS (all 23 parametrized cases green).

### Task 2.2: Define `_RetryPolicy` and `_compute_delay`

**Files:**
- Modify: `src/ticktick_cli/ticktick.py` — add `_RetryPolicy` dataclass and `_compute_delay`
- Modify: `tests/test_ticktick.py` — add delay tests

- [ ] **Step 1: Write the `_RetryPolicy` and `_compute_delay` tests.** Append to `tests/test_ticktick.py`:

```python
from ticktick_cli.ticktick import _RetryPolicy, _compute_delay


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
```

Note: `import pytest` is needed at the top of `test_ticktick.py` if not already present — the file currently has no `import pytest` because it doesn't use the `pytest` namespace. Add `import pytest` at the top of the file if missing.

- [ ] **Step 2: Run the new tests — they should fail (symbols not defined).**

Run: `uv run pytest tests/test_ticktick.py -k "retry_policy or compute_delay" -v`

Expected: ImportError.

- [ ] **Step 3: Implement `_RetryPolicy` and `_compute_delay`.** Add a `random` import at the top of `ticktick.py` (next to the existing imports) and insert above `_classify`:

```python
import random
from dataclasses import dataclass, field


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
    schedule but is still subject to the wall_clock_cap.
    """
    if retry_after is not None:
        candidate = float(retry_after)
    else:
        if attempt < 1 or attempt > len(policy.schedule):
            return None
        base = policy.schedule[attempt - 1]
        candidate = base * random.uniform(1.0 - policy.jitter, 1.0 + policy.jitter)
    if elapsed + candidate > policy.wall_clock_cap:
        return None
    return candidate
```

- [ ] **Step 4: Run the delay tests — should be green.**

Run: `uv run pytest tests/test_ticktick.py -k "retry_policy or compute_delay" -v`

Expected: PASS (6 tests).

### Task 2.3: Implement `Retry-After` parsing

**Files:**
- Modify: `src/ticktick_cli/ticktick.py` — add `_parse_retry_after`
- Modify: `tests/test_ticktick.py` — add parsing tests

- [ ] **Step 1: Write the test.** Append to `tests/test_ticktick.py`:

```python
from ticktick_cli.ticktick import _parse_retry_after


def test_parse_retry_after_integer_seconds():
    assert _parse_retry_after("7") == 7.0
    assert _parse_retry_after("0") == 0.0
    assert _parse_retry_after("  3  ") == 3.0  # tolerate whitespace


def test_parse_retry_after_http_date(monkeypatch):
    # Freeze time so the "seconds until" math is deterministic
    import ticktick_cli.ticktick as ttmod
    monkeypatch.setattr(ttmod.time, "time", lambda: 1717000000.0)
    # HTTP-date 5 seconds after our frozen now: 2024-05-29T15:06:45 UTC
    # (1717000005 → see https://www.epochconverter.com)
    val = _parse_retry_after("Wed, 29 May 2024 15:06:45 GMT")
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
```

- [ ] **Step 2: Run the test — should fail.**

Run: `uv run pytest tests/test_ticktick.py -k "parse_retry_after" -v`

Expected: ImportError.

- [ ] **Step 3: Implement `_parse_retry_after`.** Add imports at the top of `ticktick.py`:

```python
import time
from email.utils import parsedate_to_datetime
```

Then insert above `_classify`:

```python
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
```

- [ ] **Step 4: Run the parsing tests — should be green.**

Run: `uv run pytest tests/test_ticktick.py -k "parse_retry_after" -v`

Expected: PASS (4 tests).

### Task 2.4: Wire the retry loop into `_request`

**Files:**
- Modify: `src/ticktick_cli/ticktick.py` — extend `_request` with the retry loop
- Modify: `tests/test_ticktick.py` — add end-to-end retry tests

- [ ] **Step 1: Write the retry-loop tests.** Append to `tests/test_ticktick.py`:

```python
import time as _time_for_retry_tests


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


def test_get_retries_on_connect_timeout_then_succeeds(
    httpx_mock, no_sleep, no_jitter,
):
    httpx_mock.add_exception(httpx.ConnectTimeout("tls handshake"))
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
```

- [ ] **Step 2: Run the new tests — they should fail because `_request` still has no retry loop.**

Run: `uv run pytest tests/test_ticktick.py -k "retry or 429 or 503 or 4xx or warning" -v`

Expected: most fail with `httpx.ConnectTimeout` / `httpx.HTTPStatusError` propagating directly because there's no loop yet.

- [ ] **Step 3: Implement the retry loop and stderr warning.** Add a `sys` import at the top of `ticktick.py` (if not already). Replace `TickTickClient._request` with:

```python
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
            except (httpx.HTTPError, httpx.HTTPStatusError) as exc:
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
```

Also add the warning helper above `class TickTickClient`:

```python
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
```

Add `import sys` at the top of the file if not present.

- [ ] **Step 4: Run the new retry tests — they should be green now.**

Run: `uv run pytest tests/test_ticktick.py -k "retry or 429 or 503 or 4xx or warning" -v`

Expected: all pass.

- [ ] **Step 5: Run the full ticktick test file to verify no regression.**

Run: `uv run pytest tests/test_ticktick.py -v`

Expected: ALL tests pass — the original 28, plus the new classification, delay, parsing, and retry tests (~20 new tests; final count should be in the ~48-50 range).

- [ ] **Step 6: Run the full suite to make sure nothing else broke.**

Run: `uv run pytest -q`

Expected: full project test suite passes.

- [ ] **Step 7: Commit.**

```bash
git add src/ticktick_cli/ticktick.py tests/test_ticktick.py
git commit -m "$(cat <<'EOF'
Retry transient TickTick failures with bounded backoff

TickTickClient._request now retries on connection-layer flakes
(ConnectError/ConnectTimeout), GET/DELETE post-send timeouts
(ReadTimeout/WriteTimeout), HTTP 429 with Retry-After honoured, and
GET/DELETE 5xx. POSTs retry only on pre-send failures — TickTick's
behaviour on a replayed POST /task/{id} is undocumented, so we don't
guess. Schedule is 0.5/2/8s ±25% jitter, max 3 retries, ~13s
wall-clock cap. Each retry prints one stderr warning mirroring the
_resync_mirror_safe style so operators can see what's happening in
shell loops without polluting stdout JSON.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
git push origin master
```

---

## Commit 3 — Documentation

### Task 3.1: README "Reliability" section

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Read the existing README to find a sensible insertion point.**

Run: `grep -n "^##" README.md`

Pick the section that comes after general usage but before "Known quirks" or troubleshooting (if there's no obvious spot, add it at the end before any LICENSE / Contributing section).

- [ ] **Step 2: Add the Reliability section.** Insert this block at the chosen location:

```markdown
## Reliability

Every TickTick API call goes through a bounded retry loop in
`TickTickClient._request`. Transient failures (TLS handshake timeouts,
DNS errors, HTTP 429 with `Retry-After`) self-recover within ~13
seconds without surfacing to the calling command.

- **GET and DELETE**: retry on `ConnectError`, `ConnectTimeout`,
  `ReadTimeout`, `WriteTimeout`, HTTP 429 (with `Retry-After`
  honoured), and HTTP 5xx.
- **POST**: retry only on pre-send connection failures
  (`ConnectError`, `ConnectTimeout`) and HTTP 429. POST-send timeouts
  and 5xx are surfaced unchanged — TickTick's behaviour on a replayed
  task update is undocumented, so we don't guess.
- **Schedule**: 0.5s / 2s / 8s ±25% jitter, max 3 retries (4 total
  attempts), wall-clock cap ~13s. The cap fires first if `Retry-After`
  on a 429 would push past it; the original exception is raised.

Each retry attempt prints one line to stderr in the form
`warning: retry 1/3 after 0.5s — ConnectTimeout: <message>`. Stdout
(JSON output from `candidates` / `recent`) is untouched. To silence
retry chatter in scripts, redirect with `2>/dev/null`; there is no
in-CLI flag.

If the retry budget is exhausted, the original exception is surfaced
to the caller — the post-write `_resync_mirror_safe` path still
absorbs sync-side failures (see Known quirks).
```

### Task 3.2: CLAUDE.md "Known quirks" addition

**Files:**
- Modify: `CLAUDE.md` (the project file at repo root)

- [ ] **Step 1: Open `CLAUDE.md` and find the "Known quirks" section.**

Run: `grep -n "Known quirks" CLAUDE.md`

- [ ] **Step 2: Add the retry-policy quirk.** Insert this as a new bullet within "Known quirks" (the order doesn't matter much; put it near the other HTTP/API quirks):

```markdown
**`TickTickClient._request` retries transient HTTP failures.** All
eight client methods funnel through `_request`, which applies a
method-aware retry policy: GET/DELETE retry on pre-send AND post-send
transient failures; POST retries only on pre-send (`ConnectError` /
`ConnectTimeout`) because TickTick's behaviour on a replayed task
update is undocumented. 429 is honoured on every method via
`Retry-After`. Schedule: 0.5/2/8s ±25% jitter, max 3 retries,
~13s wall-clock cap. Each retry emits a stderr warning mirroring the
`_resync_mirror_safe` style. Full design in
`docs/superpowers/specs/2026-05-31-retry-with-backoff-design.md`.
```

- [ ] **Step 3: Find the testing section and add the time.sleep note.**

Run: `grep -n "Tests:" CLAUDE.md` or scan for the testing-conventions bullets.

- [ ] **Step 4: Add a one-line note about `time.sleep` monkeypatch.** Insert as a new sub-bullet under the testing conventions list:

```markdown
- Retry tests in `tests/test_ticktick.py` monkeypatch
  `ticktick_cli.ticktick.time.sleep` to a recording stub so they don't
  actually wait. Use the `no_sleep` / `no_jitter` fixtures defined in
  that file; reach for them whenever a new test exercises code that
  goes through `TickTickClient._request`.
```

### Task 3.3: Sync the memory file with the shipped state

**Files:**
- Modify: `memory/debate_2026-05-31_retry_scoping.md`

- [ ] **Step 1: Open the memory file.** Find the "## Next step" section.

- [ ] **Step 2: Replace the "pending" framing with shipped commit refs.** Change the "## Next step" content to:

```markdown
## Next step

**Shipped** in commits `<refactor-sha>..<docs-sha>` on 2026-05-31:

1. `<refactor-sha>` — Extract `TickTickClient._request` helper (pure refactor).
2. `<retry-sha>` — Retry transient TickTick failures with bounded backoff.
3. `<docs-sha>` — Document retry policy in README and CLAUDE.md.

Implementation plan: `docs/superpowers/plans/2026-05-31-retry-with-backoff.md`.

Followups (not blocked, not scheduled): re-evaluate #4 (batch tag ops)
and #3b (`BEGIN IMMEDIATE` retry) once a week of post-fix data is
available. Track them in the same memory file with a status update if
they ever happen.
```

Replace the `<...-sha>` placeholders with the actual short SHAs from `git log --oneline -3` after the previous commits land.

### Task 3.4: Verify and commit docs

- [ ] **Step 1: Run a final full-suite check.**

Run: `uv run pytest -q`

Expected: full suite passes (docs changes shouldn't touch any test, but verify).

- [ ] **Step 2: Run the CLI smoke check.**

Run: `uv run ticktick-cli --help`

Expected: help text renders without import errors. (This catches any accidentally broken import in the docs edits — e.g., a stray code-block edit that broke `ticktick.py`.)

- [ ] **Step 3: Commit.**

```bash
git add README.md CLAUDE.md memory/debate_2026-05-31_retry_scoping.md
git commit -m "$(cat <<'EOF'
Document retry-with-backoff policy

Add a Reliability section to README explaining what retries on which
methods, the backoff schedule, and the stderr warning convention. Add
a quirks bullet in CLAUDE.md pointing at the design spec, plus a
testing note about the no_sleep/no_jitter fixtures so future tests of
TickTickClient-bound code don't accidentally wait through real
backoff. Mark the debate memory file as shipped with commit refs.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
git push origin master
```

---

## Post-implementation verification

- [ ] **Step 1: Confirm git history is clean and pushed.**

Run: `git log --oneline -5 && git status`

Expected: three new commits (refactor, retry, docs), working tree clean, branch up to date with `origin/master`.

- [ ] **Step 2: Confirm the new tests are runnable in isolation.**

Run: `uv run pytest tests/test_ticktick.py -v --tb=short`

Expected: ~50 tests, all pass, no warnings about un-consumed `httpx_mock` responses.

- [ ] **Step 3: Smoke test against the real CLI surface (no API call needed).**

Run: `uv run ticktick-cli --help`

Expected: help text renders.

- [ ] **Step 4: Manual real-world check (optional but recommended).** Pick a low-stakes write op and run it twice in quick succession to confirm retry behaviour isn't disrupting normal operation:

Run: `uv run ticktick-cli sync && uv run ticktick-cli candidates --limit 5 | head`

Expected: sync completes normally, candidates JSON prints. If retry kicks in for any reason during sync, you should see one or more `warning: retry N/3 ...` lines on stderr without the command failing.

---

## Self-review checklist (run before declaring done)

- [ ] All three commits exist and are pushed.
- [ ] No commit bundles a refactor with a behavior change (commit 1 is pure refactor; commit 2 is behavior + tests).
- [ ] Existing 28 ticktick tests still pass unmodified.
- [ ] New tests cover every row of the §3.1 classification table.
- [ ] 429 + `Retry-After` honoured, wall-clock cap enforced (test exists for both).
- [ ] `_request` raises the original exception on budget exhaustion (test exists).
- [ ] Stderr warning is one line, matches the format in spec §4 (test asserts the format).
- [ ] README has a Reliability section explaining what retries when.
- [ ] CLAUDE.md "Known quirks" mentions the policy and points at the spec.
- [ ] Memory file `debate_2026-05-31_retry_scoping.md` reflects shipped state with real commit SHAs.
