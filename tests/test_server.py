from __future__ import annotations

import json

import httpx
import pytest

from .conftest import Live


def test_tools_declare_what_they_do(live: Live) -> None:
    tools = {t.name: t for t in live.tools("reader")}
    assert set(tools) == {"find_patient", "list_open_tasks", "create_task", "complete_task"}
    assert tools["find_patient"].annotations.read_only_hint
    assert not tools["create_task"].annotations.read_only_hint
    assert tools["complete_task"].annotations.destructive_hint
    assert "idempotency_key" in tools["create_task"].input_schema["properties"]
    assert tools["create_task"].output_schema is not None


def test_find_patient_returns_only_what_is_needed(live: Live) -> None:
    res = live.call("reader", "find_patient", {"name": "ana lopez", "date_of_birth": "1984-03-02"})
    assert not res.is_error
    assert res.structured_content == {"found": True, "patient_id": "p-102", "display_name": "Ana Lopez"}
    text = json.dumps(res.structured_content)
    assert "MRN" not in text and "555-0142" not in text
    miss = live.call("reader", "find_patient", {"name": "Sam Reed", "date_of_birth": "1990-07-11"})
    assert miss.structured_content["found"] is False


def test_input_is_validated(live: Live) -> None:
    res = live.call("reader", "find_patient", {"name": "Ana", "date_of_birth": "03/02/1984"})
    assert res.is_error


def test_writes_need_the_write_scope(live: Live) -> None:
    res = live.call("reader", "create_task", {"patient_id": "p-102", "title": "Echo"})
    assert res.is_error and "care:write" in res.content[0].text
    assert live.store.tasks == {}


def test_writes_are_idempotent_per_caller(live: Live) -> None:
    args = {
        "patient_id": "p-102",
        "title": "Echo in 6 weeks",
        "priority": "routine",
        "idempotency_key": "r1:tu_2",
    }
    first = live.call("writer", "create_task", args)
    again = live.call("writer", "create_task", args)
    assert first.structured_content == again.structured_content
    assert len(live.store.tasks) == 1
    # Same key, different input: refused, not a second write.
    clash = live.call("writer", "create_task", {**args, "title": "Something else"})
    assert clash.is_error and "different request" in clash.content[0].text
    # Keys are scoped per caller.
    live.call("writer-2", "create_task", args)
    assert len(live.store.tasks) == 2


def test_errors_say_what_to_do(live: Live) -> None:
    res = live.call("writer", "create_task", {"patient_id": "p-999", "title": "Echo"})
    assert res.is_error and "find_patient" in res.content[0].text
    res = live.call("writer", "complete_task", {"task_id": "t-404"})
    assert res.is_error


def test_complete_and_list(live: Live) -> None:
    t = live.call("writer", "create_task", {"patient_id": "p-102", "title": "Labs"}).structured_content
    listed = live.call("reader", "list_open_tasks", {"patient_id": "p-102"}).structured_content
    assert [x["task_id"] for x in listed["tasks"]] == [t["task_id"]]
    done = live.call("writer", "complete_task", {"task_id": t["task_id"], "idempotency_key": "k"})
    assert done.structured_content["status"] == "done"
    assert live.call("reader", "list_open_tasks", {"patient_id": "p-102"}).structured_content == {"tasks": []}
    assert live.call("reader", "list_open_tasks", {"patient_id": "p-999"}).is_error


def test_unauthenticated_and_unscoped_callers_are_refused(live: Live) -> None:
    r = httpx.post(
        live.url,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert r.status_code == 401
    assert "resource_metadata" in r.headers.get("www-authenticate", "")
    r = httpx.post(
        live.url,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Accept": "application/json, text/event-stream", "Authorization": "Bearer no-scopes"},
    )
    assert r.status_code == 403
    assert httpx.get(live.url.replace("/mcp", "/healthz")).json() == {"status": "ok"}


def test_rate_limit_per_caller(live: Live) -> None:
    results = [live.call("reader", "list_open_tasks", {"patient_id": "p-102"}) for _ in range(7)]
    assert any(r.is_error and "Rate limit" in r.content[0].text for r in results)
    assert not live.call("writer", "list_open_tasks", {"patient_id": "p-102"}).is_error


def test_every_call_is_audited_without_phi(live: Live) -> None:
    live.call("reader", "find_patient", {"name": "Ana Lopez", "date_of_birth": "1984-03-02"})
    live.call("reader", "create_task", {"patient_id": "p-102", "title": "Echo"})
    live.call("writer", "create_task", {"patient_id": "p-102", "title": "Echo", "idempotency_key": "a"})
    live.call("writer", "list_open_tasks", {"patient_id": "p-102"})
    data = [e["data"] for e in live.audit.events]
    assert [(d["action"], d["actor"]["id"], d["outcome"]) for d in data] == [
        ("tool.find_patient", "nurse-7", "success"),
        ("tool.create_task", "nurse-7", "denied"),
        ("tool.create_task", "intake-agent", "success"),
        ("tool.list_open_tasks", "intake-agent", "success"),
    ]
    # The patient a search found is audited by id, from the result.
    assert data[0]["resource"] == {"type": "patient", "ids": ["p-102"]}
    assert data[1]["reason"] == "This tool needs the care:write scope."
    task = data[2]["details"]["records"]["task"]  # the new task's id, from the result
    assert data[3]["details"]["records"] == {"patient": ["p-102"], "task": task}  # the tasks that were read
    e = live.audit.events[0]
    assert e["type"] == "vrixel.audit.v1" and e["source"] == "emr/care" and e["subject"] == "patient/p-102"
    assert data[0]["data_class"] == "phi" and data[0]["trace_id"]  # links to the request's trace
    assert "Ana Lopez" not in json.dumps(live.audit.events) and "1984-03-02" not in json.dumps(
        live.audit.events
    )


def test_a_call_that_cant_be_audited_returns_nothing(live: Live) -> None:
    live.audit.fail = True
    r = live.call("reader", "find_patient", {"name": "Ana Lopez", "date_of_birth": "1984-03-02"})
    assert r.is_error and "audit trail" in r.content[0].text and "p-102" not in str(r.content)
    r = live.call("writer", "create_task", {"patient_id": "p-102", "title": "Echo", "idempotency_key": "k1"})
    assert r.is_error and "same idempotency_key" in r.content[0].text
    live.audit.fail = False
    again = live.call(
        "writer", "create_task", {"patient_id": "p-102", "title": "Echo", "idempotency_key": "k1"}
    )
    assert not again.is_error and live.audit.events[-1]["data"]["action"] == "tool.create_task"


def test_deployed_smoke_check(live: Live, tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> None:
    from deployed.smoke import main

    monkeypatch.setenv("SMOKE_TOKEN", "reader")
    base = live.url.removesuffix("/mcp")
    assert main(["--base-url", base, "--junit", f"{tmp_path}/smoke.xml"]) == 0
    assert main(["--base-url", "http://127.0.0.1:9", "--junit", f"{tmp_path}/down.xml"]) == 1
