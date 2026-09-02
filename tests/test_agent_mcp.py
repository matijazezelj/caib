"""The MCP transport: the tool surface an agent reads, and a round trip.

Semantics are covered once in tests/test_query_core.py. What matters here is
that all seven tools are actually registered with usable schemas — an agent
picks a tool by reading these, so a missing tool or an empty schema is a real
defect — and that a call comes back as JSON rather than as a crash.
"""

import json

import anyio
import pytest
from caib_agentapi.config import Settings, make_pool
from caib_agentapi.mcp_server import create_server
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

EXPECTED_TOOLS = {
    "search_events",
    "get_alerts",
    "alert_summary",
    "top_actors",
    "actor_activity",
    "module_health",
    "list_rules",
}


@pytest.fixture()
def server(seeded_db, agent_ro_url):
    pool = make_pool(Settings(pg_url=agent_ro_url))
    pool.open()
    try:
        yield create_server(pool)
    finally:
        pool.close()


def call(server, name: str, arguments: dict | None = None) -> dict:
    """One tool call, returning the parsed JSON payload."""

    async def go():
        return await server.call_tool(name, arguments or {})

    result = anyio.run(go)
    assert result.is_error is False
    return json.loads(result.content[0].text)


def tools(server) -> dict:
    async def go():
        return await server.list_tools()

    return {tool.name: tool for tool in anyio.run(go)}


# --- the surface an agent reads -----------------------------------------


def test_all_seven_tools_are_registered(server):
    assert set(tools(server)) == EXPECTED_TOOLS


def test_every_tool_has_a_description_worth_reading(server):
    for name, tool in tools(server).items():
        assert tool.description, f"{name} has no description"
        # Short enough to be a label is not a description; an agent chooses
        # between seven of these on the text alone.
        assert len(tool.description) > 120, f"{name}'s description is too thin"


def test_every_tool_has_a_typed_object_input_schema(server):
    for name, tool in tools(server).items():
        schema = tool.input_schema
        assert schema["type"] == "object", name
        for param, spec in schema.get("properties", {}).items():
            assert spec.get("description"), f"{name}.{param} has no description"


def test_the_parameters_are_the_ones_the_query_core_takes(server):
    registered = tools(server)
    assert set(registered["search_events"].input_schema["properties"]) == {
        "q", "module", "actor", "action", "outcome", "severity",
        "since", "until", "limit", "include_raw",
    }
    assert set(registered["get_alerts"].input_schema["properties"]) == {
        "status", "severity", "rule_id", "since", "limit",
    }
    assert set(registered["actor_activity"].input_schema["properties"]) == {
        "actor", "since", "until", "limit",
    }
    # The two that take nothing take nothing.
    assert registered["module_health"].input_schema.get("properties", {}) == {}
    assert registered["list_rules"].input_schema.get("properties", {}) == {}


def test_actor_is_required_and_the_rest_are_not(server):
    schema = tools(server)["actor_activity"].input_schema
    assert schema.get("required") == ["actor"]


def test_the_server_instructions_say_it_is_read_only(server):
    async def go():
        return server.instructions

    instructions = anyio.run(go)
    assert "READ-ONLY" in instructions


# --- round trips --------------------------------------------------------


def test_search_events_round_trips(server):
    payload = call(server, "search_events", {"q": "bar-staging"})
    assert payload["count"] == 2
    assert {e["actor"] for e in payload["events"]} == {"bob@example.com"}
    assert "raw" not in payload["events"][0]
    assert payload["window"]["max_lookback_days"] == 90


def test_every_tool_round_trips_to_json(server):
    """Cheap, but it is the check that catches a tool wired to the wrong core
    function or returning something that will not serialise."""
    payloads = {
        "search_events": call(server, "search_events"),
        "get_alerts": call(server, "get_alerts"),
        "alert_summary": call(server, "alert_summary"),
        "top_actors": call(server, "top_actors"),
        "actor_activity": call(server, "actor_activity", {"actor": "alice@example.com"}),
        "module_health": call(server, "module_health"),
        "list_rules": call(server, "list_rules"),
    }
    assert set(payloads) == EXPECTED_TOOLS
    assert payloads["alert_summary"]["total"] == 3
    assert payloads["actor_activity"]["by_action"][0]["events"] == 4
    assert {m["module"] for m in payloads["module_health"]["modules"]} == {
        "aws_cloudtrail", "gcp_audit"
    }
    assert payloads["list_rules"]["count"] > 0


def test_the_limit_cap_holds_over_mcp_too(server):
    assert call(server, "search_events", {"limit": 100_000})["limit"] == 500


def test_raw_is_opt_in_over_mcp_too(server):
    default = call(server, "search_events", {"module": "gcp_audit"})
    assert "raw" not in default["events"][0]
    opted_in = call(server, "search_events", {"module": "gcp_audit", "include_raw": True})
    assert "protoPayload" in opted_in["events"][0]["raw"]


def test_a_rejected_filter_reaches_the_model_as_a_readable_error(server):
    """A QueryError has to arrive as a ToolError, whose text the model sees. If
    it escaped as anything else the SDK would replace it with a generic 'Error
    executing tool', and the agent would have no idea what it got wrong."""

    async def go():
        return await server.call_tool("search_events", {"severity": "warn"})

    with pytest.raises(ToolError) as excinfo:
        anyio.run(go)
    assert not isinstance(excinfo.value, UnexpectedToolError)
    assert "info|low|medium|high|critical" in str(excinfo.value)


def test_there_are_no_write_tools(server):
    """v1 is read-only by design; a tool named like a write is a regression."""
    names = set(tools(server))
    for forbidden in ("ack", "close", "acknowledge", "create", "update", "delete", "silence"):
        assert not any(forbidden in name for name in names), forbidden
