# Retry-with-backoff for `TickTickClient` — design

**Date:** 2026-05-31
**Status:** approved, ready for implementation plan
**Predecessor context:** [memory/debate_2026-05-31_retry_scoping.md](../../../memory/debate_2026-05-31_retry_scoping.md)

## 1. Goal and scope

Add bounded retry-with-backoff to `TickTickClient`'s HTTP methods so
transient TickTick edge failures (TLS handshake timeouts, DNS errors,
HTTP 429) self-recover within a ~13-second wall-clock budget per call,
instead of failing the calling CLI command.

**In scope:** `src/ticktick_cli/ticktick.py` only.

**Out of scope (deferred, not abandoned):**

- Batch tag operations (#4 in the source incident). Re-evaluate after
  retry lands and we have post-fix failure-rate data.
- `BEGIN IMMEDIATE` retry in `sync.py` (#3b). Unobserved contention;
  WAL is already on. Defer until "database is locked" appears in
  practice.
- Instrumentation pipeline. The per-retry stderr warnings serve as the
  initial signal; no metrics emission.
- Opt-out env vars (`TICKTICK_CLI_NO_RETRY`, `TICKTICK_CLI_RETRY_QUIET`).
  Always-on, single behavior. Re-evaluate if a caller reports pain.

## 2. Public API after refactor

`TickTickClient` retains the same eight public methods. Each delegates
to a single new private helper:

```python
def _request(
    self,
    method: str,   # "GET" | "POST" | "DELETE"
    url: str,
    *,
    json: dict | None = None,
    params: dict | None = None,
) -> httpx.Response:
    """Single HTTP entry point. Applies auth headers, retry policy
    appropriate to `method`, calls raise_for_status(), returns the
    response."""
```

The retry policy is selected by `method`: GET and DELETE retry on both
pre-send and post-send transient failures; POST retries only on
pre-send. Detail in §3.

Each existing method shrinks to a one-line delegation, e.g.:

```python
def list_projects(self) -> list[dict[str, Any]]:
    r = self._request("GET", f"{self.base_url}/project")
    return r.json()
```

## 3. Retry semantics

### 3.1 Exception classification

| httpx exception                                | Pre-send / post-send | GET/DELETE retry | POST retry |
|------------------------------------------------|----------------------|------------------|------------|
| `httpx.ConnectError`, `httpx.ConnectTimeout`   | pre-send             | yes              | yes        |
| `httpx.ReadTimeout`, `httpx.WriteTimeout`      | post-send (ambiguous) | yes             | **no**     |
| `httpx.HTTPStatusError` with `status == 429`   | server-side          | yes (honor `Retry-After`) | yes (honor `Retry-After`) |
| `httpx.HTTPStatusError` with `5xx` (500/502/503/504) | server-side    | yes              | **no**     |
| `httpx.HTTPStatusError` with `4xx` other than 429 | server-side       | **no**           | **no**     |
| any other exception                            | n/a                  | **no** (re-raise) | **no**    |

The "POST does not retry on ReadTimeout/5xx" rule preserves the
project's documented stance (`memory/feedback_honest_api_gaps.md`): if
the server might have processed the write, we don't replay it without
spec'd idempotency guarantees from TickTick. Pre-send failures (DNS,
TLS handshake) prove the server never saw the request, so replay is
safe.

429 is treated as "server is asking us to wait" — retrying is the
correct response regardless of method, and `Retry-After` (when
present) overrides the backoff schedule.

### 3.2 Backoff schedule

- **Base delays:** 0.5s, 2s, 8s — applied between attempts 1→2, 2→3,
  and 3→4 respectively.
- **Jitter:** each delay is multiplied by a random factor uniformly
  drawn from [0.75, 1.25] (±25%).
- **Cap:** 3 retries (4 total attempts) **OR** the next computed delay
  would push cumulative elapsed wall-clock past 13.0s — whichever
  fires first. The wall-clock cap is the operator-comfort guarantee;
  the attempt cap is the simple-to-test ceiling. In practice the
  attempt cap fires first under normal jitter.
- **429 `Retry-After`:** if the header is present and parseable as an
  integer or HTTP-date, use that exact duration as the next delay
  (override the schedule, but still subject to the wall-clock cap).
  If the cap would be exceeded, do not retry; raise the original
  `HTTPStatusError`.

### 3.3 Final-failure behavior

When the retry budget is exhausted, raise the **original exception**
unchanged. Callers' existing exception handling stays correct: e.g.,
`cmd_edit`'s try/except around `client.update_task(...)` still sees
the same `httpx.ReadTimeout` it would have seen without retries,
just later in time.

The post-write `_resync_mirror_safe` path is unaffected — it already
absorbs any exception from the post-write sync.

## 4. Logging

Each retry attempt emits one line to stderr in the format:

```
warning: retry 1/3 after 0.5s — ConnectTimeout: <short message>
```

- One line per retry, not per attempt (no log for the first attempt).
- Message comes from `type(exc).__name__` and `str(exc)`, truncated to
  120 chars to avoid blowing the line.
- For 429-with-`Retry-After`, the format swaps `after 0.5s` for
  `after 7s (server Retry-After)`.

Read subcommands continue emitting JSON to stdout untouched. Stderr is
the diagnostic channel; scripted callers can suppress with `2>/dev/null`
if needed (no in-CLI flag for that).

This mirrors the existing `_resync_mirror_safe` warning style (see
`cli.py:604` and onward).

## 5. Module structure inside `ticktick.py`

The retry helper lives in the same file (no new module):

```
build_update_payload(...)         # existing, unchanged
class _AuthLike(Protocol): ...    # existing, unchanged
class _RetryPolicy:               # NEW — small dataclass
    method: str
    schedule: tuple[float, ...]   # (0.5, 2.0, 8.0)
    jitter: float                 # 0.25
    wall_clock_cap: float         # 13.0
def _classify(exc, method) -> bool  # NEW — retry-this-exception predicate
def _compute_delay(...) -> float    # NEW — schedule × jitter × Retry-After
class TickTickClient:
    def _request(self, method, url, **kw) -> httpx.Response: ...   # NEW
    def list_projects(self): ...  # delegates to _request
    # ... etc, all eight existing methods now one-line delegations
```

The `_RetryPolicy`, `_classify`, and `_compute_delay` symbols are
module-private (leading underscore) and tested directly via
`from ticktick_cli.ticktick import _classify`. They are not part of
the public surface.

## 6. Testing

`tests/test_ticktick.py` gets new cases. All use `httpx_mock` from
`pytest-httpx`. No live API calls.

- **`_classify`** — table-driven test covering every row of §3.1.
- **`_compute_delay`** — verify jitter range, 429 `Retry-After`
  override (both integer-seconds and HTTP-date forms), wall-clock cap
  enforcement.
- **GET retry** — `httpx_mock` first response raises `ReadTimeout`,
  second returns 200; assert request was made twice and returned
  payload is correct.
- **POST retry on pre-send only** — first response raises
  `ConnectTimeout`, second returns 200; assert two requests, success.
  Separately: first response raises `ReadTimeout`; assert ONE request,
  exception surfaced unchanged.
- **429 with Retry-After** — first response is 429 with `Retry-After:
  2`; second is 200. Monkeypatch `time.sleep` to a recording stub;
  assert it was called with ~2.0s (not the 0.5s schedule).
- **5xx retry on GET, not on POST** — first response is 503, second is
  200. For GET: assert two requests, success. For POST: assert ONE
  request, `HTTPStatusError` surfaced unchanged. Mirrors the
  POST/ReadTimeout test.
- **Retry budget exhaustion** — three consecutive `ConnectTimeout`s;
  assert three attempts made, original exception raised.
- **Wall-clock cap** — fake clock advanced past 13s; assert no further
  retry attempted even though attempt count would allow it.
- **Stderr warning format** — capture stderr, assert one line per
  retry with the exact format shape in §4.

Existing tests (`test_ticktick.py` covering the eight public methods)
must continue to pass unchanged after the refactor commit, with no
test-side changes. That's the no-behavior-change check on the
refactor.

## 7. Commit plan

Three commits, in order, each independently revertable:

1. **Refactor: extract `_request` helper in `TickTickClient`.**
   Mechanical change. All eight methods delegate. No retry logic.
   Existing tests pass unchanged. Diff size target: ~80–120 lines.

2. **Add retry-with-backoff to `_request`.** Introduces
   `_RetryPolicy`, `_classify`, `_compute_delay`, the retry loop, and
   the stderr warning. New tests in this same commit (the behavior
   change without tests is meaningless).

3. **Document retry policy in README and CLAUDE.md.** Brief — three
   paragraphs in README under a new "Reliability" section, two
   sentences in CLAUDE.md's "Known quirks" block pointing at this
   spec. Sync `memory/debate_2026-05-31_retry_scoping.md`'s next-step
   note to "shipped".

Note on commit 2: per the project's commit discipline, behavior change
and its tests usually warrant separation. They're bundled here because
retry-loop code without retry-loop tests is unverifiable in CI — the
loop's correctness IS what the tests assert, so they're one logical
change. The pure refactor in commit 1 carries the no-behavior-change
guarantee.

## 8. Risks and mitigations

| Risk | Mitigation |
|------|------------|
| 13s stall feels like a hang during interactive use | Per-retry stderr warning makes the wait observable; ceiling is bounded |
| 429 `Retry-After` value is hostile (e.g., 60s) | Wall-clock cap prevents observance past 13s; raises original 429 |
| POST + ReadTimeout is exactly the failure mode we *can't* retry, and that's the noisy class | Accepted. Honest about API spec; better to surface than to risk duplicate side-effects |
| Refactor commit accidentally changes behavior (e.g., header order, content-type) | Existing tests pass unchanged is the gate; if any flake, fix the refactor, not the tests |
| Time module used in retry loop makes tests slow | Use `monkeypatch.setattr("time.sleep", recording_stub)` in retry tests so they don't actually wait |

## 9. Open questions

None. All design knobs were resolved in the 2026-05-31 debate and
the follow-up clarifying questions. Implementation can start.

## 10. Next step

Invoke `superpowers:writing-plans` to produce the implementation plan,
keyed to the three-commit sequence in §7.
