---
name: Feature request — edit / punt / bump-priority subcommand
description: Historical — shipped 2026-05-30 in commits 9c3bbce..bb56493. Kept as a record of the workspace-agent triage flow that motivated extending `update_task` and adding `edit`/`punt`/`bump`.
type: feature_request
status: shipped
shipped_on: 2026-05-30
shipped_in: 9c3bbce..bb56493
---

> **Status: shipped 2026-05-30.** The `update_task` extension, `edit`,
> `punt`, and `bump` subcommands all landed in commits
> `9c3bbce..bb56493`. The rest of this document is preserved verbatim
> as the spec that drove the work — useful for "why did we pick this
> shape?" questions.

Originated from a 2026-05-30 review of the workspace at
`~/Documents/Tasks/`. The workspace agent needs to execute three
conversational triage verbs:

- **"drop X"** — already works via existing `ticktick-cli delete --apply`
- **"punt X for Nd"** — blocked. Needs `startDate` mutation.
- **"bump X to high"** / **"deprioritize X"** — blocked. Needs
  `priority` mutation.

Both blocked verbs rely on the same underlying capability: passing
`startDate` and/or `priority` through `TickTickClient.update_task`.
The TickTick Open API supports this — see `docs/ticktick-openapi.md`
under "Update Task". Our wrapper currently only sends `reminders`,
which is why the verbs are blocked.

The workspace already has this gap captured in its own
`memory/feedback_triage_via_ticktick_fields.md`, including the
fallback ("for now do this in TickTick mobile"). When this feature
ships, that workspace memory entry should be updated to remove the
"blocked" annotation — that's the workspace agent's job, not this
repo's. But the workspace agent will only know to do it if Talha
mentions in a workspace session that the CLI was updated.

## Proposed spec

### 1. Extend `TickTickClient.update_task`

```python
def update_task(
    self,
    task_id: str,
    *,
    project_id: str,
    reminders: list[str] | None = None,
    start_date: str | None = None,    # ISO 8601, e.g. "2026-06-15T15:00:00+0000"
    due_date: str | None = None,
    priority: int | None = None,      # TickTick scale: 0 None / 1 Low / 3 Medium / 5 High
    title: str | None = None,
    content: str | None = None,
) -> dict[str, Any]:
```

Required fields (`id`, `projectId`) stay required. Optional fields
only land in the payload when not-None — same convention as the
existing `create_task` and `update_task` methods. **Do not send
nulls** — TickTick's update endpoint replaces fields wholesale and
a stray null clobbers existing server-side values.

### 2. New CLI subcommand `ticktick-cli edit`

```
ticktick-cli edit <task_id> [--start ISO] [--due ISO] \
       [--priority {0,1,3,5}] [--title TEXT] [--content TEXT]
```

- All flags mutually optional, but require at least one (argparse
  enforces; reject with usage error if all are None).
- Resolve `project_id` from local mirror (reuse `_lookup_project_id`
  helper).
- Re-sync after the write so subsequent reads see the new state.
- Same `_resync_mirror()` discipline as the tag commands: sync
  before reading from the mirror, sync after writing, to prevent
  stale-mirror overwrites — same rationale as `tag add`/`tag
  remove`. Two `Syncer.run()` calls per edit is the cost of
  correctness.

### 3. Optional sugar `ticktick-cli punt`

```
ticktick-cli punt <task_id> <duration>
```

Where `duration` reuses the existing `parse_duration` (`5d`, `1w`,
`2h`, bare-int-as-minutes, `at-due` is meaningless here so reject
it). Computes `start_date = now + duration` (UTC, ISO 8601 formatted
to TickTick's `yyyy-MM-dd'T'HH:mm:ssZ` shape), then calls the same
`update_task` path as `edit --start`.

Worth shipping `edit` alone first and adding `punt` in a follow-up
if `edit --start <iso>` feels clunky in practice. Sugar shouldn't
ship before the load-bearing primitive.

### 4. Tests

- `tests/test_ticktick.py` (existing): add payload-shape assertions
  for each new optional `update_task` field. Mirror the pattern in
  `test_update_task_*` tests — assert sent payload contains the
  field when passed, asserts it's absent when None.
- `tests/test_cli_commands.py` (existing): argparse coverage for
  `edit`. Confirm "at least one flag required" error. Test that
  `edit --start ISO` reaches `update_task` with `start_date=ISO`
  (use the existing `no_sync` fixture pattern + `httpx_mock`).
- For `punt`: duration parsing + the now+duration computation.
  Freeze time with `freezegun` or `monkeypatch.setattr` on
  `datetime.now`.
- Cassette updates if needed (likely not — payload-shape tests use
  `httpx_mock` directly).

### 5. Docs to update

- **README.md**: subcommand table gets `edit` and `punt` rows.
  Examples section gets a couple of triage examples.
- **This CLAUDE.md**: "Subcommand surface" table; move the gap
  from "Documented but not yet wrapped" — it'll be wrapped.
- **`memory/MEMORY.md`**: optionally update the pointer to this
  file once shipped, noting "shipped on YYYY-MM-DD in commit X".

## Acceptance

- `ticktick-cli edit <id> --priority 5` raises priority to High on
  TickTick (verify with a follow-up `sync` + `candidates`).
- `ticktick-cli punt <id> 5d` (if shipped) advances `startDate` by
  5 days; task disappears from TickTick's normal views for those 5
  days; reappears when the date passes.
- Existing 60+ tests still pass; new tests cover the new field
  paths.
- The workspace agent's "triage verbs" become fully executable from
  conversation, without falling back to "go open TickTick mobile."

## Constraints

- One logical change per commit. The split most natural here is:
  1. `update_task` field expansion (with tests)
  2. `edit` subcommand (with tests, README, CLAUDE.md updates)
  3. `punt` sugar (with tests, README, CLAUDE.md updates)

  Three commits, each independently revertable, each passing tests
  on its own.

- GPG-signed commits — never `--no-gpg-sign`.

- **Don't add field-coupling validations** (e.g. "startDate must
  be ≤ dueDate"). TickTick enforces what it cares about
  server-side; client-side checks become lint that goes stale.

- **Don't move `reminders` out of `update_task`** when you add
  the other fields — keep one method that handles arbitrary task
  updates, not a method per field.
