"""Product events: counted for the SDLC tool's outcome loop, never about people."""

import json

import pytest
from openfeature.provider.in_memory_provider import InMemoryFlag, InMemoryProvider
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from mcp_server import cohorts, events, flags

KEY = "emr.emr-feat-3.record-summary"


@pytest.fixture(autouse=True)
def fresh():
    events.reset()
    cohorts.drain()
    yield
    events.reset()
    flags.use_provider(InMemoryProvider({}))


def test_events_are_counted_into_one_summary_line_a_minute():
    events.record("record_summarized")
    events.record("record_summarized", record_type="lab")
    events.record("record.flagged")
    [line] = events.drain()
    assert line.startswith(events.MARKER + " ")
    assert json.loads(line[len(events.MARKER) + 1 :]) == {"record.flagged": 1, "record_summarized": 2}
    assert events.drain() == []


@pytest.mark.parametrize("attribute", ["email", "patient_id", "last_name", "user_id", "mrn", "member_name"])
def test_attributes_never_reach_a_person(attribute):
    with pytest.raises(ValueError, match="person"):
        events.record("record_summarized", **{attribute: "x"})


def test_names_are_lowercase_words_and_values_short_categories():
    with pytest.raises(ValueError, match="lowercase"):
        events.record("RecordSummarized")
    with pytest.raises(ValueError, match="plain name"):
        events.record("record_summarized", **{"event": "x"})
    with pytest.raises(ValueError, match="short category"):
        events.record("record_summarized", record_type="")
    events.record("record_summarized", record_type="lab")


async def summarize(request):
    if flags.enabled(KEY, user="nurse-1"):
        events.record("record_summarized")
    return JSONResponse({"jsonrpc": "2.0", "id": 1, "result": {"content": [], "isError": False}})


def test_a_requests_events_count_on_the_side_of_each_flag_it_evaluated():
    flags.use_provider(InMemoryProvider({KEY: InMemoryFlag("on", {"on": True, "off": False})}))
    client = TestClient(
        cohorts.CohortMiddleware(Starlette(routes=[Route("/mcp", summarize, methods=["POST"])]))
    )
    client.post("/mcp")
    client.post("/mcp")
    [line] = cohorts.drain()
    got = json.loads(line[len(cohorts.MARKER) + 1 :])
    assert got["on"]["events"] == {"record_summarized": 2} and got["off"]["events"] == {
        "record_summarized": 0
    }
    events.record("record_summarized")  # outside a request: only the summary line counts it
    assert cohorts.drain() == []
