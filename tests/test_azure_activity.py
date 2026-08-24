import json
import pathlib

import httpx
import psycopg
import pytest
from caib_core import loader
from caib_pollers import framework
from caib_pollers.framework import OAuth2ClientCredentials
from caib_pollers.modules.azure_activity import POLLER, envelope

FIXTURES = (
    pathlib.Path(__file__).resolve().parent.parent
    / "modules" / "azure_activity" / "fixtures"
)
PAGE1 = json.loads((FIXTURES / "activity_page1.json").read_text())
PAGE2 = json.loads((FIXTURES / "activity_page2.json").read_text())


@pytest.fixture(autouse=True)
def azure_env(monkeypatch):
    monkeypatch.setenv("CAIB_AZURE_TENANT_ID", "test-tenant")
    monkeypatch.setenv("CAIB_AZURE_CLIENT_ID", "test-client")
    monkeypatch.setenv("CAIB_AZURE_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv(
        "CAIB_AZURE_SUBSCRIPTION_ID", "00000000-0000-0000-0000-000000000001"
    )


def test_envelope_mapping_role_assignment():
    env = envelope(PAGE1["value"][0])
    assert env["event_time"] == "2026-08-23T12:05:33.0000000Z"
    assert env["event_id"] == "3f5a9b12-77cd-4e01-9a2b-c1d2e3f4a5b6"
    assert env["actor"] == "alice@example.com"
    assert env["src_ip"] == "203.0.113.88"
    assert env["action"] == "Microsoft.Authorization/roleAssignments/write"
    assert env["target"].endswith("roleAssignments/aaaa1111-bbbb-2222-cccc-333344445555")
    assert env["outcome"] == "success"
    assert env["attrs"]["category"] == "Administrative"
    assert env["attrs"]["resource_group"] == "prod-rg"
    assert env["attrs"]["status"] == "Succeeded"


def test_envelope_mapping_failed_op():
    env = envelope(PAGE2["value"][1])
    assert env["outcome"] == "failure"
    assert env["attrs"]["sub_status"] == "Forbidden"


def test_envelope_started_phase_is_unknown():
    record = dict(PAGE1["value"][0], status={"value": "Started"})
    assert envelope(record)["outcome"] == "unknown"


class ArmCtxStub:
    """First call returns page1 (with nextLink), nextLink call returns page2.
    Token endpoint is handled by the poller's own OAuth client, not this stub;
    tests inject a pre-authorized poller instead."""

    def __init__(self, backfill_start):
        self.backfill_start = backfill_start
        self.requests: list[dict] = []

    def get(self, url, *, params=None, headers=None):
        self.requests.append({"url": url, "params": params, "headers": headers})
        body = PAGE2 if "skipToken" in url else PAGE1

        class Resp:
            def __init__(self, body):
                self._body = body

            def json(self):
                return self._body

        return Resp(body)


@pytest.fixture()
def authed_poller(monkeypatch):
    monkeypatch.setattr(
        type(POLLER), "_auth_headers", lambda self, ctx: {"Authorization": "Bearer t"}
    )
    return POLLER


def test_fetch_pages_through_window(dummy_ctx, authed_poller):
    ctx = ArmCtxStub(dummy_ctx.backfill_start)

    first = authed_poller.fetch(None, ctx)
    assert len(first.envelopes) == 2
    assert first.has_more
    assert "skipToken" in first.next_cursor["next_link"]
    params = ctx.requests[0]["params"]
    assert params["api-version"] == "2015-04-01"
    assert "eventTimestamp ge '2026-08-23T00:00:00Z'" in params["$filter"]

    second = authed_poller.fetch(first.next_cursor, ctx)
    assert len(second.envelopes) == 2
    assert not second.has_more
    assert second.next_cursor == {"since": first.next_cursor["before"]}
    # nextLink is used verbatim, no params re-sent.
    assert ctx.requests[1]["params"] is None


def test_end_to_end_poll_load_dedup(migrated_db, dummy_ctx, authed_poller):
    ctx = ArmCtxStub(dummy_ctx.backfill_start)

    class FixturePoller:
        module = "azure_activity"
        interval_s = 300

        def fetch(self, cursor, _ctx):
            return authed_poller.fetch(cursor, ctx)

    poller = FixturePoller()
    with psycopg.connect(migrated_db) as conn:
        for _ in range(2):
            framework.run_cycle(conn, poller, dummy_ctx)
        assert loader.run_batch(conn, 100, drop_raw=False) == (4, 4, 0)

        conn.execute("DELETE FROM ingest.cursors")
        conn.commit()
        for _ in range(2):
            framework.run_cycle(conn, poller, dummy_ctx)
        assert loader.run_batch(conn, 100, drop_raw=False) == (4, 0, 0)
        assert conn.execute(
            "SELECT count(*) FROM caib.events WHERE module = 'azure_activity'"
        ).fetchone()[0] == 4


def test_oauth_token_cached_and_refreshed_early(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200, json={"access_token": f"tok-{len(calls)}", "expires_in": 3600}
        )

    oauth = OAuth2ClientCredentials(
        "https://login.example.com/token",
        data={"grant_type": "client_credentials", "client_id": "x", "client_secret": "y"},
    )
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        assert oauth.token(http) == "tok-1"
        assert oauth.token(http) == "tok-1"  # cached
        assert len(calls) == 1

        # Move the clock to inside the early-refresh window: refetches.
        oauth._expires_at = framework.time.monotonic() + 100
        assert oauth.token(http) == "tok-2"
        assert len(calls) == 2


def test_oauth_failure_raises(monkeypatch):
    def handler(request):
        return httpx.Response(401, json={"error": "invalid_client"})

    oauth = OAuth2ClientCredentials(
        "https://login.example.com/token", data={"grant_type": "client_credentials"}
    )
    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        pytest.raises(httpx.HTTPStatusError),
    ):
        oauth.token(http)
