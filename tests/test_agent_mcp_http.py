"""MCP over Streamable HTTP at /mcp: behind the API's token and Origin guard,
and speaking the protocol well enough for the official SDK client.

Tool semantics are tested once, in tests/test_query_core.py, and the tool
surface in tests/test_agent_mcp.py; this is the transport."""

import json
import socket
import threading
import time

import anyio
import httpx2
import pytest
import uvicorn
from caib_agentapi.app import create_app
from caib_agentapi.config import Settings
from fastapi.testclient import TestClient

TOKEN = "test-token-not-a-real-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
MCP_HEADERS = {
    **AUTH,
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
INITIALIZE = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
               "clientInfo": {"name": "test", "version": "0"}},
}


@pytest.fixture()
def client(seeded_db, agent_ro_url):
    settings = Settings(pg_url=agent_ro_url, token=TOKEN,
                        allowed_origins=frozenset({"http://localhost:6274"}))
    with TestClient(create_app(settings)) as client:
        yield client


def test_mcp_needs_the_token(client):
    for headers in ({"Content-Type": "application/json"},
                    {**MCP_HEADERS, "Authorization": "Bearer wrong"}):
        response = client.post("/mcp", headers=headers, json=INITIALIZE)
        assert response.status_code == 401
        assert response.json() == {"error": "unauthorized", "detail": "bearer token required"}
        assert response.headers["WWW-Authenticate"] == "Bearer"


def test_mcp_refuses_a_foreign_origin_even_with_the_token(client):
    response = client.post("/mcp", headers={**MCP_HEADERS, "Origin": "https://evil.example"},
                           json=INITIALIZE)
    assert response.status_code == 403
    allowed = client.post("/mcp", headers={**MCP_HEADERS, "Origin": "http://localhost:6274"},
                          json=INITIALIZE)
    assert allowed.status_code == 200


def test_mcp_answers_without_a_localhost_host_header(client):
    """Behind a reverse proxy the Host is the proxy's name. The SDK's default
    DNS-rebinding check pins Host to localhost and would 421 this."""
    response = client.post("/mcp", headers={**MCP_HEADERS, "Host": "caib.example.internal"},
                           json=INITIALIZE)
    assert response.status_code == 200, response.text


def test_mcp_json_round_trip(client):
    init = client.post("/mcp", headers=MCP_HEADERS, json=INITIALIZE)
    assert init.status_code == 200
    assert init.headers["content-type"].startswith("application/json")
    result = init.json()["result"]
    assert result["serverInfo"]["name"] == "caib"
    assert "READ-ONLY" in result["instructions"]

    listed = client.post("/mcp", headers=MCP_HEADERS,
                         json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert len(listed.json()["result"]["tools"]) == 7

    called = client.post("/mcp", headers=MCP_HEADERS, json={
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "alert_summary", "arguments": {}},
    }).json()["result"]
    assert called["isError"] is False
    assert json.loads(called["content"][0]["text"])["total"] == 3


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def live_url(seeded_db, agent_ro_url):
    """A real uvicorn listener: the SDK client below speaks real HTTP."""
    port = _free_port()
    config = uvicorn.Config(create_app(Settings(pg_url=agent_ro_url, token=TOKEN)),
                            host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


def test_the_official_sdk_client_works_against_mcp(live_url):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async def go():
        async with (
            httpx2.AsyncClient(headers=AUTH) as http_client,
            streamable_http_client(f"{live_url}/mcp", http_client=http_client) as (r, w),
            ClientSession(r, w) as session,
        ):
            await session.initialize()
            tools = await session.list_tools()
            ok = await session.call_tool("get_alerts", {"status": "open"})
            bad = await session.call_tool("search_events", {"severity": "warn"})
            return tools, ok, bad

    tools, ok, bad = anyio.run(go)
    assert len(tools.tools) == 7
    assert ok.is_error is False
    assert json.loads(ok.content[0].text)["count"] == 1
    assert bad.is_error is True
    assert "info|low|medium|high|critical" in bad.content[0].text
