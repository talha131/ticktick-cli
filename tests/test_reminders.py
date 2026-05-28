"""Tests for reminder duration parsing, iCal TRIGGER formatting, and the
TickTick update_task method."""

from __future__ import annotations

import json
import pytest
from ticktick_cli.ticktick import (
    TickTickClient,
    format_trigger,
    parse_duration,
)


# ---- format_trigger --------------------------------------------------------


def test_format_trigger_minutes() -> None:
    assert format_trigger(15) == "TRIGGER:-PT15M"
    assert format_trigger(90) == "TRIGGER:-PT90M"


def test_format_trigger_at_due() -> None:
    assert format_trigger(0) == "TRIGGER:PT0S"


def test_format_trigger_rejects_negative() -> None:
    with pytest.raises(ValueError, match=">= 0"):
        format_trigger(-15)


# ---- parse_duration --------------------------------------------------------


def test_parse_duration_minutes() -> None:
    assert parse_duration("15m") == 15
    assert parse_duration("90m") == 90


def test_parse_duration_hours() -> None:
    assert parse_duration("1h") == 60
    assert parse_duration("2h") == 120


def test_parse_duration_days() -> None:
    assert parse_duration("1d") == 1440
    assert parse_duration("2d") == 2880


def test_parse_duration_at_due() -> None:
    assert parse_duration("at-due") == 0
    assert parse_duration("0") == 0


def test_parse_duration_bare_int_is_minutes() -> None:
    assert parse_duration("30") == 30


def test_parse_duration_handles_uppercase_and_whitespace() -> None:
    assert parse_duration("  15M  ") == 15
    assert parse_duration("1H") == 60


def test_parse_duration_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="Invalid duration"):
        parse_duration("abc")
    with pytest.raises(ValueError, match="Invalid duration"):
        parse_duration("1y")  # year not supported


# ---- TickTickClient.update_task -------------------------------------------


class _StubAuth:
    def get_access_token_sync(self) -> str:
        return "fake-token"


def test_update_task_sends_reminders(httpx_mock) -> None:
    """A `remind` call PUTs ... well, POSTs ... a task update with the
    reminders array and the required id+projectId."""
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={"id": "t1", "projectId": "p1",
              "reminders": ["TRIGGER:-PT15M"]},
    )
    client = TickTickClient(auth=_StubAuth())
    result = client.update_task(
        "t1", project_id="p1", reminders=["TRIGGER:-PT15M"]
    )
    assert result["reminders"] == ["TRIGGER:-PT15M"]

    sent = json.loads(httpx_mock.get_request().content)
    assert sent == {
        "id": "t1",
        "projectId": "p1",
        "reminders": ["TRIGGER:-PT15M"],
    }


def test_update_task_clears_reminders_with_empty_list(httpx_mock) -> None:
    """Sending reminders=[] (not None) explicitly clears them."""
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={"id": "t1", "projectId": "p1", "reminders": []},
    )
    client = TickTickClient(auth=_StubAuth())
    client.update_task("t1", project_id="p1", reminders=[])
    sent = json.loads(httpx_mock.get_request().content)
    assert sent["reminders"] == []


def test_update_task_omits_unset_optional_fields(httpx_mock) -> None:
    """If reminders is None (not passed), it must NOT appear in the payload —
    sending null would clobber TickTick's server-side value."""
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task/t1",
        json={"id": "t1", "projectId": "p1"},
    )
    client = TickTickClient(auth=_StubAuth())
    client.update_task("t1", project_id="p1")
    sent = json.loads(httpx_mock.get_request().content)
    assert "reminders" not in sent
    assert sent == {"id": "t1", "projectId": "p1"}


def test_create_task_includes_reminders(httpx_mock) -> None:
    """The reminders kwarg on create_task lands in the POST body."""
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task",
        json={"id": "new-id", "projectId": "p1", "title": "X",
              "reminders": ["TRIGGER:-PT15M", "TRIGGER:PT0S"]},
    )
    client = TickTickClient(auth=_StubAuth())
    client.create_task(
        project_id="p1", title="X",
        reminders=["TRIGGER:-PT15M", "TRIGGER:PT0S"],
    )
    sent = json.loads(httpx_mock.get_request().content)
    assert sent["reminders"] == ["TRIGGER:-PT15M", "TRIGGER:PT0S"]


def test_create_task_omits_reminders_when_none(httpx_mock) -> None:
    httpx_mock.add_response(
        method="POST",
        url="https://api.ticktick.com/open/v1/task",
        json={"id": "x", "projectId": "p1", "title": "X"},
    )
    client = TickTickClient(auth=_StubAuth())
    client.create_task(project_id="p1", title="X")
    sent = json.loads(httpx_mock.get_request().content)
    assert "reminders" not in sent
