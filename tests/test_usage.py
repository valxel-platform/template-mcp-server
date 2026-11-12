"""Who calls which tool: a log line, span attributes and metrics per call, naming the OAuth client
and the calling app but never the person."""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from mcp_server import usage
from mcp_server.telemetry import JsonFormatter

from .conftest import Live


@pytest.fixture(autouse=True)
def fresh() -> None:
    usage.reset()


def metrics(live: Live) -> str:
    return httpx.get(live.url.removesuffix("/mcp") + "/metrics").text


def sample(text: str, name: str, **labels: str) -> float:
    want = ",".join(f'{k}="{v}"' for k, v in labels.items())
    total = 0.0
    for line in text.splitlines():
        if line.startswith(name + "{") and all(f'{k}="{v}"' in line for k, v in labels.items()):
            total += float(line.rsplit(" ", 1)[1])
    assert total or want, text
    return total


def test_each_call_counts_by_tool_client_app_and_outcome(
    live: Live, caplog: pytest.LogCaptureFixture
) -> None:
    before = metrics(live)
    caplog.set_level(logging.INFO, logger="mcp.calls")
    live.call("writer", "find_patient", {"name": "Ada Lovelace", "date_of_birth": "1985-12-10"})
    live.call("reader", "create_task", {"patient_id": "p-1", "title": "Call back"})  # no write scope: denied
    text = metrics(live)
    got = sample(text, "mcp_tool_calls_total", tool="find_patient", client="intake-agent", outcome="success")
    assert (
        got
        - sample(
            before, "mcp_tool_calls_total", tool="find_patient", client="intake-agent", outcome="success"
        )
        == 1
    )
    assert sample(text, "mcp_tool_calls_total", tool="create_task", client="nurse-7", outcome="denied") >= 1
    assert (
        sample(text, "mcp_tool_call_duration_seconds_count", tool="find_patient", client="intake-agent") >= 1
    )
    # The app is the calling software as it names itself (clientInfo): the MCP SDK's client here.
    assert 'app="mcp"' in text
    lines = [r for r in caplog.records if r.name == "mcp.calls"]
    assert [r.getMessage() for r in lines][-2:] == ["tools/call find_patient", "tools/call create_task"]
    fields = lines[-2].fields  # type: ignore[attr-defined]
    assert fields["mcp.client.id"] == "intake-agent" and fields["event.outcome"] == "success"
    assert fields["mcp.client.app"] == "mcp" and fields["mcp.client.version"] == "0.1.0"
    # Never the arguments or the person: no name, date of birth or subject in the line.
    rendered = JsonFormatter("care").format(lines[-2])
    assert (
        "Ada" not in rendered
        and "1985" not in rendered
        and json.loads(rendered)["mcp.tool"] == "find_patient"
    )


def test_connecting_clients_are_logged_with_their_versions(
    live: Live, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="mcp.clients")
    live.tools("reader")
    [rec] = [r for r in caplog.records if r.name == "mcp.clients"]
    fields = rec.fields  # type: ignore[attr-defined]
    assert rec.getMessage() == "server/discover mcp"
    assert fields["mcp.client.name"] == "mcp" and fields["mcp.client.version"] == "0.1.0"
    assert fields["mcp.protocol.version"] == "2026-07-28"


def test_client_info_from_either_protocol() -> None:
    new = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "find_patient",
                "arguments": {"name": "Ada"},
                "_meta": {
                    usage.CLIENT_INFO: {"name": "claude-code", "version": "2.1.4"},
                    usage.PROTOCOL: "2026-07-28",
                },
            },
        }
    )
    assert usage.client_info(new.encode()) == ("tools/call", "claude-code", "2.1.4", "2026-07-28")
    old = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": "Cursor", "version": "1.7"}},
        }
    )
    assert usage.client_info(old.encode()) == ("initialize", "Cursor", "1.7", "2025-06-18")
    assert usage.client_info(b"not json") == ("", "", "", "")
    assert usage.client_info(b"[1, 2]") == ("", "", "", "")


def test_metrics_and_health_are_not_traced_and_need_no_token(live: Live) -> None:
    r = httpx.get(live.url.removesuffix("/mcp") + "/metrics")
    assert r.status_code == 200 and "mcp_tool_calls" in r.text and "traceresponse" not in r.headers


def test_names_are_cleaned_and_bounded() -> None:
    assert usage.app_from_user_agent("claude-code/2.1.4 (darwin; arm64)") == "claude-code"
    assert usage.app_from_user_agent("Cursor/1.7.3") == "cursor"
    assert usage.app_from_user_agent("") == "unknown"
    assert usage.app_from_user_agent('<script>"x"</script>') == "script-x"
    assert usage.clean("A" * 100) == "a" * 40
    for n in range(usage.MAX_CLIENTS):
        usage.record_call("find_patient", f"client-{n}", "success", 0.01)
    usage.record_call("find_patient", "one-too-many", "success", 0.01)
    text = usage.metrics_body()[0].decode()
    assert 'client="other"' in text and "one-too-many" not in text
    usage.record_call("find_patient", "", "success", 0.01)
    assert 'client="none"' in usage.metrics_body()[0].decode()
