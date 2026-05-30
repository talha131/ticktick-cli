"""End-to-end tests for the argparse-dispatched CLI handlers.

These tests exercise the layer between argparse and the API/Store —
verifying error guards (exit codes), dry-run vs --apply branches, and
the partial-failure finally blocks. Unit-level concerns (HTTP shapes,
SQL queries, tag-helper logic) are tested in their own modules.

Style: each test builds a minimal cli_env (TICKTICK_CLI_HOME + faux
token + initialized Store) and monkeypatches Syncer.run() to a no-op
so we can pre-populate the mirror directly. The handlers' own
`update_task` / `move_task` / etc. calls go through httpx_mock so the
HTTP wire shape stays under test."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from ticktick_cli import cli
from ticktick_cli.store import Store
from ticktick_cli.sync import Syncer


# ---- Fixtures ---------------------------------------------------------------


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch):
    """Populate TICKTICK_CLI_HOME, OAuth secrets, and a far-future token
    so _build_client() doesn't try to run an auth flow."""
    monkeypatch.setenv("TICKTICK_CLI_HOME", str(tmp_path))
    monkeypatch.setenv("TICKTICK_CLIENT_ID", "test-cid")
    monkeypatch.setenv("TICKTICK_CLIENT_SECRET", "test-csec")
    (tmp_path / ".ticktick-auth").write_text(json.dumps({
        "access_token": "fake-token",
        "expires_at": int(time.time()) + 86400,
    }))
    return tmp_path


@pytest.fixture
def store(cli_env: Path) -> Store:
    """Open the same Store the handler will open, pre-init schema."""
    s = Store(cli_env / "cache" / "tasks.db")
    s.init_schema()
    return s


@pytest.fixture
def no_sync(monkeypatch):
    """Replace Syncer.run with a no-op so tests can pre-populate the
    mirror and not have to mock /project + /project/{id}/data for every
    pre/post sync the handler performs."""
    monkeypatch.setattr(Syncer, "run", lambda self: None)


def _seed_project(store: Store, project_id: str, name: str) -> None:
    store.conn.execute(
        "INSERT INTO projects(id, name, slug) VALUES (?,?,?)",
        (project_id, name, name.lower()),
    )


def _seed_task(
    store: Store,
    task_id: str,
    project_id: str,
    title: str = "Test task",
    tags: list[str] | None = None,
) -> None:
    store.conn.execute(
        "INSERT INTO tasks(id, project_id, title, status, tags, updated_at) "
        "VALUES (?, ?, ?, 0, ?, '2026-05-29T00:00:00')",
        (task_id, project_id, title, json.dumps(tags) if tags else None),
    )


def _run(subcommand_args: list[str]) -> int:
    """Parse and dispatch like main() does, return the handler's exit code."""
    parser = cli._build_parser()
    args = parser.parse_args(subcommand_args)
    return args.func(args)


# ---- cmd_move ---------------------------------------------------------------


def test_move_happy_path(store, no_sync, httpx_mock, capsys) -> None:
    _seed_project(store, "p1", "Work")
    _seed_project(store, "p2", "Personal")
    _seed_task(store, "t1", "p1")
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/move",
        json=[{"id": "t1", "etag": "abc"}],
    )

    assert _run(["move", "t1", "--to", "Personal"]) == 0
    body = json.loads(httpx_mock.get_request().content)
    assert body == [{"taskId": "t1",
                     "fromProjectId": "p1", "toProjectId": "p2"}]


def test_move_same_project_exits_2_without_api_call(
    store, no_sync, httpx_mock, capsys
) -> None:
    """Local guard short-circuits before hitting the API."""
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1")

    assert _run(["move", "t1", "--to", "Work"]) == 2
    assert "already in project" in capsys.readouterr().err
    # No HTTP call should have been made.
    assert httpx_mock.get_requests() == []


# ---- cmd_repeat -------------------------------------------------------------


def test_repeat_sets_rrule(store, no_sync, httpx_mock) -> None:
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1")
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={"id": "t1", "projectId": "p1",
              "repeatFlag": "RRULE:FREQ=DAILY;INTERVAL=1"},
    )

    assert _run(["repeat", "t1", "RRULE:FREQ=DAILY;INTERVAL=1"]) == 0
    body = json.loads(httpx_mock.get_request().content)
    assert body["repeatFlag"] == "RRULE:FREQ=DAILY;INTERVAL=1"


def test_repeat_clear_sends_empty_string(store, no_sync, httpx_mock) -> None:
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1")
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={"id": "t1", "projectId": "p1"},
    )

    assert _run(["repeat", "t1", "--clear"]) == 0
    body = json.loads(httpx_mock.get_request().content)
    assert body["repeatFlag"] == ""


def test_repeat_rrule_and_clear_together_exits_2(
    store, no_sync, httpx_mock, capsys
) -> None:
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1")

    assert _run(["repeat", "t1", "RRULE:FREQ=DAILY", "--clear"]) == 2
    assert "either an RRULE or --clear" in capsys.readouterr().err
    assert httpx_mock.get_requests() == []


def test_repeat_no_rrule_no_clear_exits_2(
    store, no_sync, httpx_mock, capsys
) -> None:
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1")

    assert _run(["repeat", "t1"]) == 2
    assert "Pass an RRULE" in capsys.readouterr().err
    assert httpx_mock.get_requests() == []


# ---- cmd_edit ---------------------------------------------------------------


def test_edit_title_sends_title_in_payload(store, no_sync, httpx_mock) -> None:
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1")
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={"id": "t1", "projectId": "p1", "title": "Renamed"},
    )

    assert _run(["edit", "t1", "--title", "Renamed"]) == 0
    body = json.loads(httpx_mock.get_request().content)
    assert body["title"] == "Renamed"
    assert body["id"] == "t1"
    assert body["projectId"] == "p1"


def test_edit_priority_accepts_name(store, no_sync, httpx_mock) -> None:
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1")
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={"id": "t1", "projectId": "p1"},
    )
    assert _run(["edit", "t1", "--priority", "high"]) == 0
    body = json.loads(httpx_mock.get_request().content)
    assert body["priority"] == 5


def test_edit_priority_accepts_numeric(store, no_sync, httpx_mock) -> None:
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1")
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={"id": "t1", "projectId": "p1"},
    )
    assert _run(["edit", "t1", "--priority", "5"]) == 0
    body = json.loads(httpx_mock.get_request().content)
    assert body["priority"] == 5


def test_edit_priority_rejects_invalid(store, no_sync, capsys) -> None:
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1")
    # argparse's `type=` callable raises → SystemExit(2)
    with pytest.raises(SystemExit) as exc_info:
        _run(["edit", "t1", "--priority", "ultra"])
    assert exc_info.value.code == 2


def test_edit_due_accepts_iso(store, no_sync, httpx_mock) -> None:
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1")
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={"id": "t1", "projectId": "p1"},
    )
    assert _run(["edit", "t1", "--due", "2026-06-15T15:00:00+0000"]) == 0
    body = json.loads(httpx_mock.get_request().content)
    assert body["dueDate"] == "2026-06-15T15:00:00+0000"


def test_edit_due_accepts_relative(store, no_sync, httpx_mock) -> None:
    """Relative spec goes through dates.parse_when. We assert the
    request was made with a non-empty dueDate; exact value depends on
    `now` and isn't worth pinning here (parse_when is tested directly
    in test_dates.py)."""
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1")
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={"id": "t1", "projectId": "p1"},
    )
    assert _run(["edit", "t1", "--due", "+7d"]) == 0
    body = json.loads(httpx_mock.get_request().content)
    assert "dueDate" in body
    # Loose shape check: 4-digit year, contains 'T', ends with offset.
    assert body["dueDate"][:4].isdigit()
    assert "T" in body["dueDate"]


def test_edit_clear_due_sends_empty_string(store, no_sync, httpx_mock) -> None:
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1")
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={"id": "t1", "projectId": "p1"},
    )
    assert _run(["edit", "t1", "--clear-due"]) == 0
    body = json.loads(httpx_mock.get_request().content)
    assert body["dueDate"] == ""


def test_edit_clear_start_sends_empty_string(store, no_sync, httpx_mock) -> None:
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1")
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={"id": "t1", "projectId": "p1"},
    )
    assert _run(["edit", "t1", "--clear-start"]) == 0
    body = json.loads(httpx_mock.get_request().content)
    assert body["startDate"] == ""


def test_edit_due_and_clear_due_conflict(store, no_sync, capsys) -> None:
    """Can't both set and clear in the same invocation."""
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1")
    assert _run(["edit", "t1", "--due", "+7d", "--clear-due"]) == 2
    assert "either --due or --clear-due" in capsys.readouterr().err.lower()


def test_edit_no_flags_errors(store, no_sync, capsys) -> None:
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1")
    assert _run(["edit", "t1"]) == 2
    assert "at least one" in capsys.readouterr().err.lower()


def test_edit_unknown_task_exits_2(store, no_sync, capsys) -> None:
    """_lookup_project_id exits 2 if the task isn't in the mirror."""
    _seed_project(store, "p1", "Work")
    # No task seeded.
    with pytest.raises(SystemExit) as exc_info:
        _run(["edit", "missing-id", "--title", "x"])
    assert exc_info.value.code == 2


def test_edit_rejects_garbage_date(store, no_sync, capsys) -> None:
    """Malformed --due / --start should exit 2 with a clean stderr,
    not a Python traceback. Mirrors cmd_punt's parse error handling."""
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1")
    assert _run(["edit", "t1", "--due", "flarble"]) == 2
    assert "cannot parse" in capsys.readouterr().err.lower()


def test_edit_resyncs_after_write(store, monkeypatch, httpx_mock) -> None:
    """A successful edit triggers exactly one Syncer.run() afterwards,
    matching the discipline of cmd_remind / cmd_repeat."""
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1")
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={"id": "t1", "projectId": "p1"},
    )
    sync_calls = []
    monkeypatch.setattr(Syncer, "run",
                        lambda self: sync_calls.append(1))
    assert _run(["edit", "t1", "--title", "Renamed"]) == 0
    assert len(sync_calls) == 1


def test_edit_dry_run_prints_payload_without_api_call_or_sync(
    store, monkeypatch, httpx_mock, capsys
) -> None:
    """--dry-run prints the PATCH body as JSON to stdout and short-
    circuits before any HTTP call or post-write Syncer.run(). The
    absence of httpx_mock.add_response() means any escaped request
    would raise — that's the no-API-call assertion."""
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1")
    sync_calls = []
    monkeypatch.setattr(Syncer, "run", lambda self: sync_calls.append(1))

    assert _run(["edit", "t1", "--title", "Renamed", "--dry-run"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"id": "t1", "projectId": "p1", "title": "Renamed"}
    assert sync_calls == []


def test_edit_full_prints_entire_task_response(
    store, no_sync, httpx_mock, capsys
) -> None:
    """--full prints the full TickTick task object instead of the
    abridged {id, title, due_date, start_date, priority} summary.
    Verified by asserting that response fields the summary omits
    (e.g. projectId, content, modifiedTime) appear in stdout."""
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1")
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={
            "id": "t1", "projectId": "p1", "title": "Renamed",
            "content": "extra notes", "modifiedTime": "2026-05-30T12:00:00+0000",
        },
    )
    assert _run(["edit", "t1", "--title", "Renamed", "--full"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["projectId"] == "p1"
    assert out["content"] == "extra notes"
    assert out["modifiedTime"] == "2026-05-30T12:00:00+0000"


def test_edit_dry_run_wins_over_full(
    store, monkeypatch, httpx_mock, capsys
) -> None:
    """When both --dry-run and --full are passed, dry-run wins — there
    is no API response to print full of, so stdout is the PATCH body."""
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1")
    monkeypatch.setattr(Syncer, "run", lambda self: None)

    assert _run([
        "edit", "t1", "--title", "Renamed", "--dry-run", "--full",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"id": "t1", "projectId": "p1", "title": "Renamed"}


# ---- cmd_punt ---------------------------------------------------------------


def test_punt_sets_start_date(store, no_sync, httpx_mock) -> None:
    """`punt t1 7d` should send a startDate update with a non-empty
    value and leave dueDate untouched."""
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1")
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={"id": "t1", "projectId": "p1"},
    )
    assert _run(["punt", "t1", "7d"]) == 0
    body = json.loads(httpx_mock.get_request().content)
    assert "startDate" in body and body["startDate"]
    assert "dueDate" not in body


def test_punt_accepts_weekday_name(store, no_sync, httpx_mock) -> None:
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1")
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={"id": "t1", "projectId": "p1"},
    )
    assert _run(["punt", "t1", "monday"]) == 0
    body = json.loads(httpx_mock.get_request().content)
    assert "startDate" in body


def test_punt_rejects_garbage(store, no_sync, capsys) -> None:
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1")
    # parse_when raises ValueError → handler turns it into exit 2.
    assert _run(["punt", "t1", "flarble"]) == 2
    assert "cannot parse" in capsys.readouterr().err.lower()


def test_punt_unknown_task_exits_2(store, no_sync, capsys) -> None:
    """_lookup_project_id exits 2 if the task isn't in the mirror —
    mirrors test_edit_unknown_task_exits_2."""
    _seed_project(store, "p1", "Work")
    # No task seeded.
    with pytest.raises(SystemExit) as exc_info:
        _run(["punt", "missing-id", "7d"])
    assert exc_info.value.code == 2


def test_punt_resyncs_after_write(store, monkeypatch, httpx_mock) -> None:
    """A successful punt triggers exactly one Syncer.run() afterwards,
    matching the discipline of cmd_edit / cmd_remind / cmd_repeat."""
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1")
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={"id": "t1", "projectId": "p1"},
    )
    sync_calls = []
    monkeypatch.setattr(Syncer, "run",
                        lambda self: sync_calls.append(1))
    assert _run(["punt", "t1", "7d"]) == 0
    assert len(sync_calls) == 1


def test_punt_dry_run_prints_payload_without_api_call_or_sync(
    store, monkeypatch, httpx_mock, capsys
) -> None:
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1")
    sync_calls = []
    monkeypatch.setattr(Syncer, "run", lambda self: sync_calls.append(1))

    assert _run(["punt", "t1", "7d", "--dry-run"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload.keys()) == {"id", "projectId", "startDate"}
    assert payload["id"] == "t1"
    assert payload["projectId"] == "p1"
    assert payload["startDate"]  # non-empty parsed ISO date
    assert sync_calls == []


def test_punt_full_prints_entire_task_response(
    store, no_sync, httpx_mock, capsys
) -> None:
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1")
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={
            "id": "t1", "projectId": "p1", "title": "Test task",
            "startDate": "2026-06-06T00:00:00+0000",
            "modifiedTime": "2026-05-30T12:00:00+0000",
        },
    )
    assert _run(["punt", "t1", "7d", "--full"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["projectId"] == "p1"
    assert out["modifiedTime"] == "2026-05-30T12:00:00+0000"


# ---- cmd_bump ---------------------------------------------------------------


def test_bump_high_sends_priority_5(store, no_sync, httpx_mock) -> None:
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1")
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={"id": "t1", "projectId": "p1"},
    )
    assert _run(["bump", "t1", "high"]) == 0
    body = json.loads(httpx_mock.get_request().content)
    assert body["priority"] == 5


def test_bump_none_sends_priority_0(store, no_sync, httpx_mock) -> None:
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1")
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={"id": "t1", "projectId": "p1"},
    )
    assert _run(["bump", "t1", "none"]) == 0
    body = json.loads(httpx_mock.get_request().content)
    assert body["priority"] == 0


def test_bump_rejects_invalid_level(store, no_sync) -> None:
    """`_parse_priority` rejects anything outside name + canonical-int
    set; argparse surfaces the ArgumentTypeError as SystemExit(2)."""
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1")
    with pytest.raises(SystemExit) as exc_info:
        _run(["bump", "t1", "ultra"])
    assert exc_info.value.code == 2


def test_bump_accepts_numeric_priority(store, no_sync, httpx_mock) -> None:
    """`bump t1 5` should produce the same payload as `bump t1 high` —
    the parser type-coerces the int input through _parse_priority."""
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1")
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={"id": "t1", "projectId": "p1"},
    )
    assert _run(["bump", "t1", "5"]) == 0
    body = json.loads(httpx_mock.get_request().content)
    assert body["priority"] == 5


def test_bump_numeric_and_name_produce_equivalent_dry_run(
    store, no_sync, capsys
) -> None:
    """Whether you pass `high` or `5`, the PATCH body is identical."""
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1")

    assert _run(["bump", "t1", "high", "--dry-run"]) == 0
    by_name = json.loads(capsys.readouterr().out)

    assert _run(["bump", "t1", "5", "--dry-run"]) == 0
    by_number = json.loads(capsys.readouterr().out)

    assert by_name == by_number == {
        "id": "t1", "projectId": "p1", "priority": 5,
    }


def test_bump_rejects_non_canonical_numeric(store, no_sync) -> None:
    """`_parse_priority` only accepts 0/1/3/5 numerically — 2/4/6/7 etc.
    are rejected with SystemExit(2)."""
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1")
    with pytest.raises(SystemExit) as exc_info:
        _run(["bump", "t1", "2"])
    assert exc_info.value.code == 2


def test_bump_numeric_input_emits_name_in_level_field(
    store, no_sync, httpx_mock, capsys
) -> None:
    """Output schema is {id, priority, level} where `level` is always
    the name (high/medium/low/none). Numeric input still yields the
    name via reverse map so callers get a consistent shape."""
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1")
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={"id": "t1", "projectId": "p1"},
    )
    assert _run(["bump", "t1", "5"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["priority"] == 5
    assert out["level"] == "high"


def test_bump_resyncs_after_write(store, monkeypatch, httpx_mock) -> None:
    """A successful bump triggers exactly one Syncer.run() afterwards,
    matching the discipline of cmd_edit / cmd_punt / cmd_remind."""
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1")
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={"id": "t1", "projectId": "p1"},
    )
    sync_calls = []
    monkeypatch.setattr(Syncer, "run",
                        lambda self: sync_calls.append(1))
    assert _run(["bump", "t1", "high"]) == 0
    assert len(sync_calls) == 1


def test_bump_unknown_task_exits_2(store, no_sync, capsys) -> None:
    """_lookup_project_id exits 2 if the task isn't in the mirror —
    mirrors test_edit_unknown_task_exits_2 / test_punt_unknown_task_exits_2."""
    _seed_project(store, "p1", "Work")
    # No task seeded.
    with pytest.raises(SystemExit) as exc_info:
        _run(["bump", "missing-id", "high"])
    assert exc_info.value.code == 2


def test_bump_dry_run_prints_payload_without_api_call_or_sync(
    store, monkeypatch, httpx_mock, capsys
) -> None:
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1")
    sync_calls = []
    monkeypatch.setattr(Syncer, "run", lambda self: sync_calls.append(1))

    assert _run(["bump", "t1", "high", "--dry-run"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"id": "t1", "projectId": "p1", "priority": 5}
    assert sync_calls == []


def test_bump_full_prints_entire_task_response(
    store, no_sync, httpx_mock, capsys
) -> None:
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1")
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={
            "id": "t1", "projectId": "p1", "title": "Test task",
            "priority": 5, "modifiedTime": "2026-05-30T12:00:00+0000",
        },
    )
    assert _run(["bump", "t1", "high", "--full"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["projectId"] == "p1"
    assert out["modifiedTime"] == "2026-05-30T12:00:00+0000"


# ---- cmd_delete -------------------------------------------------------------


def test_delete_dry_run_makes_no_api_call(
    store, no_sync, httpx_mock, capsys
) -> None:
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1", title="Important thing")

    assert _run(["delete", "t1"]) == 0
    err = capsys.readouterr().err
    assert "Would delete t1" in err
    assert "Important thing" in err
    assert "--apply" in err
    assert httpx_mock.get_requests() == []


def test_delete_apply_calls_api(store, no_sync, httpx_mock) -> None:
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1")
    httpx_mock.add_response(
        method="DELETE",
        url="https://api.ticktick.com/open/v1/project/p1/task/t1",
        status_code=200,
    )

    assert _run(["delete", "t1", "--apply"]) == 0
    assert httpx_mock.get_request().method == "DELETE"


# ---- cmd_tag_add ------------------------------------------------------------


def test_tag_add_merges_with_existing(store, no_sync, httpx_mock) -> None:
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1", tags=["urgent"])
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={"id": "t1", "projectId": "p1"},
    )

    assert _run(["tag", "add", "t1", "waiting", "blocked"]) == 0
    body = json.loads(httpx_mock.get_request().content)
    # Existing tag preserved, new ones appended.
    assert body["tags"] == ["urgent", "waiting", "blocked"]


def test_tag_add_unchanged_makes_no_api_call(
    store, no_sync, httpx_mock, capsys
) -> None:
    """If every requested tag is already on the task, skip the API."""
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1", tags=["urgent", "blocked"])

    assert _run(["tag", "add", "t1", "urgent"]) == 0
    out = capsys.readouterr().out
    assert '"unchanged": true' in out
    assert httpx_mock.get_requests() == []


# ---- cmd_tag_remove ---------------------------------------------------------


def test_tag_remove_set_difference(store, no_sync, httpx_mock) -> None:
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1", tags=["urgent", "blocked", "waiting"])
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={"id": "t1", "projectId": "p1"},
    )

    assert _run(["tag", "remove", "t1", "blocked"]) == 0
    body = json.loads(httpx_mock.get_request().content)
    assert body["tags"] == ["urgent", "waiting"]


def test_tag_remove_ignore_case(store, no_sync, httpx_mock) -> None:
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1", tags=["Urgent", "blocked"])
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={"id": "t1", "projectId": "p1"},
    )

    assert _run(["tag", "remove", "t1", "URGENT", "--ignore-case"]) == 0
    body = json.loads(httpx_mock.get_request().content)
    assert body["tags"] == ["blocked"]


def test_tag_remove_unchanged_makes_no_api_call(
    store, no_sync, httpx_mock, capsys
) -> None:
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1", tags=["urgent"])

    assert _run(["tag", "remove", "t1", "nonexistent"]) == 0
    out = capsys.readouterr().out
    assert '"unchanged": true' in out
    assert httpx_mock.get_requests() == []


# ---- cmd_tag_rename ---------------------------------------------------------


def test_tag_rename_identical_old_new_exits_2(
    store, no_sync, httpx_mock, capsys
) -> None:
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1", tags=["foo"])

    assert _run(["tag", "rename", "foo", "foo"]) == 2
    assert "identical" in capsys.readouterr().err
    assert httpx_mock.get_requests() == []


def test_tag_rename_dry_run_makes_no_api_call(
    store, no_sync, httpx_mock, capsys
) -> None:
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1", title="A", tags=["old"])
    _seed_task(store, "t2", "p1", title="B", tags=["old", "other"])

    assert _run(["tag", "rename", "old", "new"]) == 0
    err = capsys.readouterr().err
    assert "Would rename" in err
    assert "t1" in err and "t2" in err
    assert httpx_mock.get_requests() == []


def test_tag_rename_apply_iterates(store, no_sync, httpx_mock) -> None:
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1", tags=["old"])
    _seed_task(store, "t2", "p1", tags=["old", "other"])
    # Two affected tasks → two update_task calls.
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={"id": "t1", "projectId": "p1"},
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t2",
        json={"id": "t2", "projectId": "p1"},
    )

    assert _run(["tag", "rename", "old", "new", "--apply"]) == 0
    bodies = [json.loads(r.content) for r in httpx_mock.get_requests()]
    # Each request replaces the task's tag list.
    tag_lists = sorted([b["tags"] for b in bodies], key=lambda x: x[0])
    assert tag_lists == [["new"], ["new", "other"]]


def test_tag_rename_finally_resyncs_on_mid_loop_failure(
    store, cli_env, monkeypatch, httpx_mock, capsys
) -> None:
    """If update_task raises on the second iteration, the Syncer.run()
    in the finally block must still execute and the exception must
    propagate. Without the finally, the mirror would silently lag
    behind the partial server state."""
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1", tags=["old"])
    _seed_task(store, "t2", "p1", tags=["old"])
    # First update succeeds, second 500s.
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={"id": "t1", "projectId": "p1"},
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t2",
        status_code=500,
    )

    sync_calls = {"n": 0}

    def counting_run(self):
        sync_calls["n"] += 1

    monkeypatch.setattr(Syncer, "run", counting_run)

    with pytest.raises(Exception):
        _run(["tag", "rename", "old", "new", "--apply"])
    # Pre-sync + post-sync-in-finally = 2 calls even though we crashed.
    assert sync_calls["n"] == 2


# ---- cmd_tag_delete ---------------------------------------------------------


def test_tag_delete_dry_run_makes_no_api_call(
    store, no_sync, httpx_mock, capsys
) -> None:
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1", title="A", tags=["doomed"])

    assert _run(["tag", "delete", "doomed"]) == 0
    err = capsys.readouterr().err
    assert "Would remove" in err
    assert httpx_mock.get_requests() == []


def test_tag_delete_apply_strips_tag(store, no_sync, httpx_mock) -> None:
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1", tags=["doomed", "keep"])
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={"id": "t1", "projectId": "p1"},
    )

    assert _run(["tag", "delete", "doomed", "--apply"]) == 0
    body = json.loads(httpx_mock.get_request().content)
    assert body["tags"] == ["keep"]


# ---- Emoji tags across CLI handlers -----------------------------------------
#
# Tags with emojis are real TickTick data — the UI lets you create them and
# the API accepts them. These tests pin the argv → mirror → HTTP body path
# end-to-end, so a future change to encoding/decoding doesn't silently
# corrupt them. Storage-level and matching-level guarantees are covered in
# test_tags.py; what this section adds is the *handler* layer, where argv
# parsing, mirror reads, payload assembly, and httpx serialization all
# meet.


def test_tag_add_emoji_to_existing_task(store, no_sync, httpx_mock) -> None:
    """Adding an emoji tag from argv lands in the API payload as a
    literal code point (json.loads on the request body reconstructs it),
    and the merge with existing tags preserves order."""
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1", tags=["work"])
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={"id": "t1", "projectId": "p1"},
    )

    assert _run(["tag", "add", "t1", "🔥urgent"]) == 0
    body = json.loads(httpx_mock.get_request().content)
    assert body["tags"] == ["work", "🔥urgent"]


def test_tag_remove_emoji_from_task(store, no_sync, httpx_mock) -> None:
    """Removing an emoji tag picks the right one out of the existing
    list — the matched tag is identified by literal-string equality."""
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1", tags=["work", "🔥urgent", "📅today"])
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={"id": "t1", "projectId": "p1"},
    )

    assert _run(["tag", "remove", "t1", "🔥urgent"]) == 0
    body = json.loads(httpx_mock.get_request().content)
    assert body["tags"] == ["work", "📅today"]


def test_tag_rename_text_to_emoji_via_sweep(store, no_sync, httpx_mock) -> None:
    """Renaming a text tag to an emoji tag (or vice versa) sweeps every
    matching task. Each request body carries the substituted tag list
    with the emoji intact."""
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1", tags=["urgent"])
    _seed_task(store, "t2", "p1", tags=["urgent", "work"])
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={"id": "t1", "projectId": "p1"},
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t2",
        json={"id": "t2", "projectId": "p1"},
    )

    assert _run(["tag", "rename", "urgent", "🔥urgent", "--apply"]) == 0
    bodies = [json.loads(r.content) for r in httpx_mock.get_requests()]
    tag_lists = sorted([b["tags"] for b in bodies], key=lambda x: len(x))
    assert tag_lists == [["🔥urgent"], ["🔥urgent", "work"]]


def test_tag_delete_emoji_strips_only_that_tag(store, no_sync, httpx_mock) -> None:
    """Deleting an emoji tag sweeps every task carrying it and leaves
    other tags — including other emoji tags — untouched."""
    _seed_project(store, "p1", "Work")
    _seed_task(store, "t1", "p1", tags=["🔥urgent", "📅today"])
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={"id": "t1", "projectId": "p1"},
    )

    assert _run(["tag", "delete", "🔥urgent", "--apply"]) == 0
    body = json.loads(httpx_mock.get_request().content)
    assert body["tags"] == ["📅today"]


def test_add_task_with_emoji_tag_propagates_to_api(
    store, no_sync, httpx_mock
) -> None:
    """The `add` command's --tag accepts emoji argv and the literal code
    point reaches the create-task payload. Closes the loop on `ticktick-cli
    add ... --tag 🔥urgent` actually creating a task with that tag."""
    _seed_project(store, "p1", "Work")
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task",
        json={"id": "new1", "projectId": "p1", "title": "Pay bills",
              "tags": ["🔥urgent", "💰finance"]},
    )

    assert _run(["add", "Pay bills", "--project", "Work",
                 "--tag", "🔥urgent", "--tag", "💰finance"]) == 0
    body = json.loads(httpx_mock.get_request().content)
    assert body["tags"] == ["🔥urgent", "💰finance"]


# ---- cmd_recent -------------------------------------------------------------
#
# Wire-shape tests for the recent handler. The cache + per-day batching
# logic is covered in test_recent.py; here we focus on argv → HTTP body
# and abridged-vs-full output shape.


def test_recent_emits_abridged_shape_by_default(
    store, no_sync, httpx_mock, capsys,
) -> None:
    """Default output matches the spec's abridged shape exactly —
    {id, title, project, priority, tags, completed_at, due_date, start_date}.
    Project id is replaced with its human-readable name."""
    _seed_project(store, "p1", "Work")
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/completed",
        json=[{
            "id": "a", "projectId": "p1", "title": "Wrote tests",
            "status": 2, "priority": 5,
            "tags": ["work", "🔥urgent"],
            "completedTime": "2026-05-30T13:00:00+0000",
            "dueDate": "2026-05-30T00:00:00+0000",
            "startDate": "2026-05-29T00:00:00+0000",
            "content": "extra notes",
            "modifiedTime": "2026-05-30T13:00:01+0000",
        }],
    )

    assert _run(["recent"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == [{
        "id": "a",
        "title": "Wrote tests",
        "project": "Work",
        "priority": 5,
        "tags": ["work", "🔥urgent"],
        "completed_at": "2026-05-30T13:00:00+0000",
        "due_date": "2026-05-30T00:00:00+0000",
        "start_date": "2026-05-29T00:00:00+0000",
    }]


def test_recent_full_prints_entire_task_object(
    store, no_sync, httpx_mock, capsys,
) -> None:
    """--full prints raw TickTick task bodies including fields the
    abridged shape drops (content, modifiedTime, etag, etc.)."""
    _seed_project(store, "p1", "Work")
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/completed",
        json=[{
            "id": "a", "projectId": "p1", "title": "Wrote tests",
            "status": 2, "priority": 5,
            "completedTime": "2026-05-30T13:00:00+0000",
            "content": "extra notes",
            "modifiedTime": "2026-05-30T13:00:01+0000",
            "etag": "abc123",
        }],
    )

    assert _run(["recent", "--full"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert len(out) == 1
    assert out[0]["content"] == "extra notes"
    assert out[0]["modifiedTime"] == "2026-05-30T13:00:01+0000"
    assert out[0]["etag"] == "abc123"


def test_recent_project_filter_by_name_resolves_to_id(
    store, no_sync, httpx_mock, capsys,
) -> None:
    """--project Work → API gets projectIds=[p1], not the literal name."""
    _seed_project(store, "p1", "Work")
    _seed_project(store, "p2", "Personal")
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/completed",
        json=[],
    )

    assert _run(["recent", "--project", "Work"]) == 0
    body = json.loads(httpx_mock.get_request().content)
    assert body["projectIds"] == ["p1"]


def test_recent_project_filter_by_id_passes_through(
    store, no_sync, httpx_mock, capsys,
) -> None:
    """A raw project id on --project is accepted verbatim."""
    _seed_project(store, "p1", "Work")
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/completed",
        json=[],
    )

    assert _run(["recent", "--project", "p1"]) == 0
    body = json.loads(httpx_mock.get_request().content)
    assert body["projectIds"] == ["p1"]


def test_recent_unknown_project_exits_2(store, no_sync, capsys) -> None:
    """--project that matches neither id nor name fails fast at exit 2,
    mirroring _resolve_project_id's error handling for other handlers."""
    _seed_project(store, "p1", "Work")
    with pytest.raises(SystemExit) as exc_info:
        _run(["recent", "--project", "Nonexistent"])
    assert exc_info.value.code == 2


def test_recent_empty_results_print_empty_list(
    store, no_sync, httpx_mock, capsys,
) -> None:
    """No completions in the window → stdout is `[]` (valid JSON), exit 0."""
    _seed_project(store, "p1", "Work")
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/completed",
        json=[],
    )

    assert _run(["recent"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_recent_default_days_is_seven(
    store, no_sync, httpx_mock, capsys,
) -> None:
    """Default --days is 7. The API call's startDate should be ~6 days
    before today (7-day inclusive window)."""
    _seed_project(store, "p1", "Work")
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/completed",
        json=[],
    )

    assert _run(["recent"]) == 0
    body = json.loads(httpx_mock.get_request().content)
    # Compute today vs startDate diff — should be 6 days.
    from datetime import datetime, timezone
    start = datetime.strptime(body["startDate"], "%Y-%m-%dT%H:%M:%S%z")
    now = datetime.now(timezone.utc)
    delta_days = (now.date() - start.date()).days
    assert delta_days == 6


def test_recent_custom_days_widens_window(
    store, no_sync, httpx_mock, capsys,
) -> None:
    """--days 14 → API startDate is 13 days before today."""
    _seed_project(store, "p1", "Work")
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/completed",
        json=[],
    )

    assert _run(["recent", "--days", "14"]) == 0
    body = json.loads(httpx_mock.get_request().content)
    from datetime import datetime, timezone
    start = datetime.strptime(body["startDate"], "%Y-%m-%dT%H:%M:%S%z")
    now = datetime.now(timezone.utc)
    delta_days = (now.date() - start.date()).days
    assert delta_days == 13


def test_recent_limit_caps_result_count(
    store, no_sync, httpx_mock, capsys,
) -> None:
    """--limit applies AFTER cross-project sort — the most-recent N
    survive even when project mix varies."""
    _seed_project(store, "p1", "Work")
    _seed_project(store, "p2", "Personal")
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/completed",
        json=[
            {"id": "old1", "projectId": "p1", "title": "old1", "status": 2,
             "completedTime": "2026-05-25T10:00:00+0000"},
            {"id": "new1", "projectId": "p2", "title": "new1", "status": 2,
             "completedTime": "2026-05-30T12:00:00+0000"},
            {"id": "mid1", "projectId": "p1", "title": "mid1", "status": 2,
             "completedTime": "2026-05-28T10:00:00+0000"},
        ],
    )

    assert _run(["recent", "--limit", "2"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [t["id"] for t in out] == ["new1", "mid1"]


def test_recent_does_not_call_syncer(
    store, monkeypatch, httpx_mock, capsys,
) -> None:
    """Unlike write handlers, `recent` is a pure read — no Syncer.run().
    The cache is its own state, independent of the main tasks mirror."""
    _seed_project(store, "p1", "Work")
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/completed",
        json=[],
    )
    sync_calls: list[int] = []
    monkeypatch.setattr(Syncer, "run", lambda self: sync_calls.append(1))

    assert _run(["recent"]) == 0
    assert sync_calls == []
