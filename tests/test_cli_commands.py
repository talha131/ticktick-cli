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
