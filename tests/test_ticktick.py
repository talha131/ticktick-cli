import httpx
from ticktick_cli.ticktick import TickTickClient


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
