# Debate — scoping the retry-with-backoff fix (2026-05-31)

## Context

The 2026-05-30 / 2026-05-31 operating window surfaced four reliability
issues. Two were already shipped before this debate (post-write mirror
sync softened to a stderr warning via `_resync_mirror_safe`; SQLite WAL
enabled at `store.py:75`). The remaining three open items:

- **#2** retry-with-backoff: `ticktick.py` has 8 bare httpx calls with
  zero retry logic. Operator observed ~40% per-call failure rate on
  batches of 6–8 tag ops, clustering pattern (first call succeeds, later
  fail) with TLS handshake timeouts + DNS errors. Suggests TickTick edge
  throttling.
- **#3b** `BEGIN IMMEDIATE` retry in `sync.py:44` to handle the brief
  contention window WAL doesn't fully close.
- **#4** Batch tag operations — `cmd_tag_add` / `cmd_tag_remove` take
  one task id; operator's pattern (`for id in $IDS; do …`) amplifies
  N tag changes into N×3 API calls (pre-sync + write + post-sync).

The debate question: ship #2 alone, bundle all three as a hardening
initiative, do #4 first to cut call volume, or instrument first?

## Participants

Three-voice debate (Codex hit a rate limit at dispatch time):

- **Gemini CLI** — Position A, HIGH confidence
- **Sonnet (Agent)** — Position A, HIGH confidence
- **Opus (Claude)** — Position A, HIGH confidence (synthesis)

Raw responses archived at
`~/.claude-octopus/debates/2026-05-31-retry-scoping/`.

## Conclusion

**Ship #2 alone as the next initiative.** Two independent voices (Gemini,
Sonnet) plus the synthesis converged on Position A. Retry is the only
correctness fix; #3b is unobserved contention; #4 is UX/perf. Smaller
scope means cleaner bisect if retry causes regressions.

Sequence the work as three commits per the project's commit discipline:

1. Pure refactor — extract `TickTickClient._request(method, url, **kw)`,
   route all eight call sites through it, zero behavior change.
2. Behavior change — add the retry loop inside `_request`, with the
   shape and idempotency rules below.
3. Tests — `pytest-httpx` cassettes covering the retry-able exception
   types, the POST pre-send-only retry rule, and `Retry-After` honor.

What would change this: a measurement showing the failures cluster
within a single batch only (i.e., #4 fixes >80% of cases). Then C
becomes correct. Worth instrumenting after retry lands to gather that
data for the eventual #4 decision.

## Design answers (synthesized)

### (a) Decorator location — single `_request` helper

`_request(method, url, **kw)` inside `TickTickClient`, with a pure-refactor
commit first. Per-method decoration duplicates state across eight call
sites. `httpx.HTTPTransport(retries=…)` only covers `ConnectError`,
missing `ReadTimeout` and 429 — the failure modes the operator
observed.

### (b) POST idempotency — pre-send retry only

Retry POSTs **only** on pre-send connection failures
(`httpx.ConnectError`, `httpx.ConnectTimeout`). On `httpx.ReadTimeout`
or any `HTTPStatusError` other than 429, surface the exception
unchanged. GET and DELETE retry on both pre-send and ReadTimeout (they
are spec-idempotent).

Rationale: aligns with `memory/feedback_honest_api_gaps.md` — TickTick's
behavior on retried `POST /open/v1/task/{taskId}` is undocumented, so
we don't guess. A pre-send failure means the server never received the
request; retrying is safe. A ReadTimeout means the server might have
processed it; retrying could duplicate a side-effect we don't have
spec'd handling for.

This is stricter than the original report proposed and stricter than
Gemini's "trust effective idempotency" position. Sonnet's framing wins
on repo-norm grounds.

### (c) Backoff shape — 0.5s / 2s / 8s ±25% jitter, max 3 attempts

Compromise between the original 1s/4s/16s (~21s budget, too long for
interactive CLI) and Gemini's 0.5s/2s/5s (~7.5s, undershoots edge-
throttle recovery).

- Honor `Retry-After` header on HTTP 429 (overrides backoff).
- Total budget cap ~13s in the worst case.
- Three attempts is the ceiling — four would push past 20s and break
  interactive feel.

### (d) Logging channel — stderr, single line, no flag

Format mirrors the existing `_resync_mirror_safe` warning style:
`warning: retry 1/3 after 0.5s — ConnectTimeout`. No `--quiet` flag
yet — speculative. If a caller later complains, `TICKTICK_CLI_RETRY_QUIET=1`
env opt-out is trivial to add. Read subcommands still emit JSON on
stdout untouched.

## Hidden risk verdict

A bounded retry budget (≤13s) is strictly better than fail-fast for
this CLI. The shell-loop alternative — `&&` chain aborts on first
flake, leaving a partial tag sweep with some tasks updated and some
not — is harder to recover from than a slow success. The stderr retry
warning is non-negotiable; it converts an unexplained 8-second pause
into an observable event, which is the difference between perceived
hang and perceived working.

The deeper rule (from Sonnet, understated by Gemini): in a shell `&&`
chain, slow success leaves a coherent system; fast failure leaves
partial state. For a tool whose callers are scripts mutating a remote
source-of-truth, the former is the safer default.

## Next step

Feed this conclusion into a brainstorming / spec design session for
the retry implementation. The spec only needs to be a few pages —
the design space is now narrow.
