"""Requests per flag, on and off, for the SDLC tool's gradual releases."""

import json
import logging

import pytest
from openfeature.provider.in_memory_provider import InMemoryFlag, InMemoryProvider
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from mcp_server import cohorts, flags

KEY = "emr.emr-feat-3.referral-lookup"


def on(value: str = "on") -> None:
    flags.use_provider(InMemoryProvider({KEY: InMemoryFlag(value, {"on": True, "off": False})}))


async def ok(request):
    flags.enabled(KEY, user="nurse-1")
    flags.enabled(KEY, user="nurse-1")  # twice in one request: still one request
    return JSONResponse({"jsonrpc": "2.0", "id": 1, "result": {"content": [], "isError": False}})


async def tool_error(request):
    flags.enabled(KEY)
    return JSONResponse({"jsonrpc": "2.0", "id": 2, "result": {"content": [], "isError": True}})


async def rpc_error(request):
    flags.enabled(KEY)
    return JSONResponse({"jsonrpc": "2.0", "id": 3, "error": {"code": -32603, "message": "x"}})


async def boom(request):
    flags.enabled(KEY)
    raise RuntimeError("boom")


async def plain(request):
    return JSONResponse({"error": {"code": 1}})  # no flag evaluated: not counted


@pytest.fixture
def client():
    cohorts.drain()
    app = Starlette(
        routes=[
            Route(p, f, methods=["POST"])
            for p, f in (
                ("/ok", ok),
                ("/tool", tool_error),
                ("/rpc", rpc_error),
                ("/boom", boom),
                ("/plain", plain),
            )
        ]
    )
    yield TestClient(cohorts.CohortMiddleware(app), raise_server_exceptions=False)
    flags.use_provider(InMemoryProvider({}))


def test_mcp_errors_count_as_failed_though_they_come_with_a_200(client):
    on()
    assert client.post("/ok").status_code == 200
    assert client.post("/tool").status_code == 200
    assert client.post("/rpc").status_code == 200
    assert client.post("/boom").status_code == 500
    client.post("/plain")
    on("off")
    client.post("/ok")
    [line] = cohorts.drain()
    assert json.loads(line[len(cohorts.MARKER) :]) == {
        "flag": KEY,
        "on": {"requests": 4, "errors": 3},
        "off": {"requests": 1, "errors": 0},
    }


def test_reports_are_logged_and_outside_a_request_nothing_counts(caplog):
    on()
    flags.enabled(KEY)
    assert cohorts.drain() == []
    token = cohorts.begin()
    cohorts.seen(KEY, False)
    cohorts.seen("not a key!", True)
    cohorts.end(token, failed=True)
    with caplog.at_level(logging.INFO, logger="mcp_server.cohorts"):
        cohorts.report()
    assert [r.getMessage() for r in caplog.records] == [
        cohorts.MARKER
        + ' {"flag":"'
        + KEY
        + '","on":{"requests":0,"errors":0},"off":{"requests":1,"errors":1}}'
    ]
    cohorts.start_reporting(period_s=3600)
    cohorts.start_reporting(period_s=3600)
    flags.use_provider(InMemoryProvider({}))
