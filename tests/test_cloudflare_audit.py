import json
import pathlib

import psycopg
import pytest
from caib_core import loader
from caib_pollers import framework
from caib_pollers.modules.cloudflare_audit import POLLER, envelope

FIXTURES = (
    pathlib.Path(__file__).resolve().parent.parent
    / "modules" / "cloudflare_audit" / "fixtures"
)
PAGE1 = json.loads((FIXTURES / "logs_audit_page1.json").read_text())
PAGE2 = json.loads((FIXTURES / "logs_audit_page2.json").read_text())


@pytest.fixture(autouse=True)
def cf_env(monkeypatch):
    monkeypatch.setenv("CAIB_CLOUDFLARE_API_TOKEN", "test-token")
    monkeypatch.setenv("CAIB_CLOUDFLARE_ACCOUNT_ID", "0123456789abcdef0123456789abcdef")


def test_envelope_mapping_user_actor():
    env = envelope(PAGE1["result"][0])
    assert env["event_time"] == "2026-08-23T09:15:04Z"
    assert env["event_id"] == "d5b0f326-1232-4452-8858-1089bd7168ef"
    assert env["actor"] == "alice@example.com"
    assert env["src_ip"] == "203.0.113.44"
    assert env["action"] == "token_create"
    assert env["target"] == "b8c1d2e3f4a5968778695a4b3c2d1e0f"
    assert env["outcome"] == "success"
    assert env["severity"] == "info"
    assert env["attrs"]["actor_context"] == "dash"
    assert env["attrs"]["resource_product"] == "iam"
    assert "zone_name" not in env["attrs"]  # empty fields omitted
    assert env["raw"] == PAGE1["result"][0]


def test_envelope_mapping_token_actor_falls_back_to_token_name():
    env = envelope(PAGE1["result"][1])
    assert env["actor"] == "terraform-dns"  # email empty -> token_name
    assert env["attrs"]["zone_name"] == "example.com"
    assert env["attrs"]["http_method"] == "DELETE"


def test_envelope_mapping_failed_login_is_failure():
    env = envelope(PAGE2["result"][0])
    assert env["action"] == "login"
    assert env["outcome"] == "failure"  # action.result: false


class PagedCtxStub:
    """Serves page1 until a page cursor is passed, then page2."""

    def __init__(self, backfill_start):
        self.backfill_start = backfill_start
        self.requests: list[dict] = []

    def get(self, url, *, params=None, headers=None):
        self.requests.append({"url": url, "params": params, "headers": headers})
        body = PAGE1 if "cursor" not in params else PAGE2

        class Resp:
            def __init__(self, body):
                self._body = body

            def json(self):
                return self._body

        return Resp(body)


def test_fetch_pages_through_window(dummy_ctx):
    ctx = PagedCtxStub(dummy_ctx.backfill_start)

    first = POLLER.fetch(None, ctx)
    assert len(first.envelopes) == 2
    assert first.has_more
    assert first.next_cursor["page_cursor"] == "dGhpc19pc19hX2N1cnNvcg"
    p = ctx.requests[0]["params"]
    assert p["since"] == "2026-08-23T00:00:00Z"  # backfill start
    assert p["direction"] == "asc"
    assert ctx.requests[0]["headers"]["Authorization"] == "Bearer test-token"

    second = POLLER.fetch(first.next_cursor, ctx)
    assert len(second.envelopes) == 1
    assert not second.has_more
    # Window sealed: next since = this window's before.
    assert second.next_cursor == {"since": first.next_cursor["before"]}
    assert ctx.requests[1]["params"]["cursor"] == "dGhpc19pc19hX2N1cnNvcg"


def test_end_to_end_poll_load_dedup(migrated_db, dummy_ctx):
    ctx = PagedCtxStub(dummy_ctx.backfill_start)

    class FixturePoller:
        module = "cloudflare_audit"
        interval_s = 300

        def fetch(self, cursor, _ctx):
            return POLLER.fetch(cursor, ctx)

    poller = FixturePoller()
    with psycopg.connect(migrated_db) as conn:
        for _ in range(2):  # page 1 (has_more) + page 2
            framework.run_cycle(conn, poller, dummy_ctx)
        assert loader.run_batch(conn, 100, drop_raw=False) == (3, 3, 0)

        # Poll the same fixtures again: everything dedups, nothing new lands.
        conn.execute("DELETE FROM ingest.cursors")
        conn.commit()
        for _ in range(2):
            framework.run_cycle(conn, poller, dummy_ctx)
        assert loader.run_batch(conn, 100, drop_raw=False) == (3, 0, 0)
        assert conn.execute("SELECT count(*) FROM caib.events").fetchone()[0] == 3
        modules = conn.execute(
            "SELECT DISTINCT module FROM caib.events"
        ).fetchall()
        assert modules == [("cloudflare_audit",)]
