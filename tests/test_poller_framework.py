import logging

import httpx
import psycopg
import pytest
from caib_core import loader
from caib_pollers import framework
from caib_pollers.framework import Ctx, FetchResult

log = logging.getLogger("test")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(framework.time, "sleep", lambda s: None)


def make_ctx(handler) -> tuple[Ctx, httpx.Client]:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    import datetime

    ctx = Ctx(
        module="testmod",
        backfill_start=datetime.datetime(2026, 8, 23, tzinfo=datetime.UTC),
        log=log,
        http=client,
    )
    return ctx, client


def test_get_retries_429_with_retry_after_then_succeeds():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"ok": True})

    ctx, client = make_ctx(handler)
    with client:
        assert ctx.get("https://api.example.com/x").json() == {"ok": True}
    assert len(calls) == 3


def test_get_4xx_raises_immediately():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(401, json={"error": "bad token"})

    ctx, client = make_ctx(handler)
    with client, pytest.raises(httpx.HTTPStatusError):
        ctx.get("https://api.example.com/x")
    assert len(calls) == 1


def test_get_gives_up_after_six_attempts():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503)

    ctx, client = make_ctx(handler)
    with client, pytest.raises(RuntimeError, match="giving up"):
        ctx.get("https://api.example.com/x")
    assert len(calls) == framework.HTTP_ATTEMPTS


class FakePoller:
    module = "testmod"
    interval_s = 1

    def __init__(self, result: FetchResult):
        self.result = result

    def fetch(self, cursor, ctx):
        self.last_cursor = cursor
        return self.result


GOOD_ENVELOPE = {
    "event_time": "2026-08-23T10:00:00Z",
    "event_id": "ev-1",
    "action": "thing_happened",
    "outcome": "success",
    "severity": "info",
    "attrs": {},
}


def test_run_cycle_commits_landing_and_cursor_atomically(migrated_db, dummy_ctx):

    with psycopg.connect(migrated_db) as conn:
        poller = FakePoller(
            FetchResult([GOOD_ENVELOPE], {"since": "2026-08-23T10:00:00Z"})
        )
        _result, landed, dead = framework.run_cycle(conn, poller, dummy_ctx)
        assert (landed, dead) == (1, 0)
        assert poller.last_cursor is None  # first run
        assert conn.execute("SELECT count(*) FROM ingest.landing").fetchone()[0] == 1
        assert conn.execute(
            "SELECT cursor FROM ingest.cursors WHERE module = 'testmod'"
        ).fetchone()[0] == {"since": "2026-08-23T10:00:00Z"}


def test_crash_mid_batch_loses_neither_rows_nor_cursor(migrated_db, dummy_ctx):

    with psycopg.connect(migrated_db) as conn:
        # Seed a known-good cursor first.
        framework.run_cycle(
            conn,
            FakePoller(FetchResult([], {"since": "seeded"})),
            dummy_ctx,
        )
        # This cycle dies INSIDE the transaction (unserializable cursor) after
        # the landing insert - the batch must roll back as one unit.
        bad = FakePoller(FetchResult([GOOD_ENVELOPE], {"oops": {1, 2}}))
        with pytest.raises(TypeError):
            framework.run_cycle(conn, bad, dummy_ctx)
        assert conn.execute("SELECT count(*) FROM ingest.landing").fetchone()[0] == 0
        assert conn.execute(
            "SELECT cursor FROM ingest.cursors WHERE module = 'testmod'"
        ).fetchone()[0] == {"since": "seeded"}

        # Recovery run: same envelope lands exactly once.
        ok = FakePoller(FetchResult([GOOD_ENVELOPE], {"since": "recovered"}))
        framework.run_cycle(conn, ok, dummy_ctx)
        assert conn.execute("SELECT count(*) FROM ingest.landing").fetchone()[0] == 1


def test_invalid_envelopes_dead_letter_without_blocking_valid(migrated_db, dummy_ctx):

    with psycopg.connect(migrated_db) as conn:
        poller = FakePoller(
            FetchResult(
                [
                    GOOD_ENVELOPE,
                    {"event_id": "no-time", "action": "x"},
                    {"event_time": "2026-08-23T10:00:00Z"},
                ],
                {"since": "s"},
            )
        )
        _result, landed, dead = framework.run_cycle(conn, poller, dummy_ctx)
        assert (landed, dead) == (1, 2)
        reasons = {r[0] for r in conn.execute("SELECT reason FROM ingest.dead_letter")}
        assert reasons == {"missing event_time", "missing action"}
        # The batch made it to caib.events through the loader.
        assert loader.run_batch(conn, 100, drop_raw=False) == (1, 1, 0)
