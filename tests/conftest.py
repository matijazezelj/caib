"""Test database plumbing.

CI sets CAIB_TEST_PG_URL to a service container. Locally we start a throwaway
timescaledb container for the session and remove it afterwards.
"""

import datetime
import logging
import os
import pathlib
import subprocess
import time
import uuid

import psycopg
import pytest
from caib_core import migrate
from caib_pollers.framework import Ctx

TSDB_IMAGE = "timescale/timescaledb:2.30.0-pg18"
MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parent.parent / "db" / "migrations"


def _wait_ready(url: str, timeout_s: float = 90.0) -> None:
    deadline = time.monotonic() + timeout_s
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(url, connect_timeout=3) as conn:
                conn.execute("SELECT 1")
            return
        except psycopg.Error as exc:
            last_err = exc
            time.sleep(0.5)
    raise RuntimeError(f"postgres not ready after {timeout_s}s: {last_err}")


@pytest.fixture(scope="session")
def pg_server_url() -> str:
    if url := os.environ.get("CAIB_TEST_PG_URL"):
        _wait_ready(url)
        yield url
        return

    name = f"caib-test-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        [
            "docker", "run", "-d", "--rm", "--name", name,
            "-e", "POSTGRES_PASSWORD=test",
            "-e", "POSTGRES_DB=caib",
            "-p", "127.0.0.1:0:5432",
            TSDB_IMAGE,
        ],
        check=True,
        capture_output=True,
    )
    try:
        out = subprocess.run(
            ["docker", "port", name, "5432/tcp"], check=True, capture_output=True, text=True
        ).stdout
        port = out.strip().splitlines()[0].rsplit(":", 1)[1]
        url = f"postgresql://postgres:test@127.0.0.1:{port}/caib"
        _wait_ready(url)
        yield url
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


@pytest.fixture()
def pg_db_url(pg_server_url: str) -> str:
    """A fresh database on the shared server, dropped after the test."""
    dbname = f"caib_t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(pg_server_url, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{dbname}"')
    base, _, _ = pg_server_url.rpartition("/")
    url = f"{base}/{dbname}"
    yield url
    with psycopg.connect(pg_server_url, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE "{dbname}" WITH (FORCE)')


AGENT_RO_PASSWORD = "agent-ro-test-pw"


@pytest.fixture()
def migrated_db(pg_db_url: str, monkeypatch) -> str:
    monkeypatch.setenv("CAIB_PG_APP_PASSWORD", "app-test-pw")
    monkeypatch.setenv("CAIB_PG_RO_PASSWORD", "ro-test-pw")
    monkeypatch.setenv("CAIB_AGENT_RO_PASSWORD", AGENT_RO_PASSWORD)
    migrate.run(pg_db_url, MIGRATIONS_DIR)
    _pause_policy_jobs(pg_db_url)
    return pg_db_url


def _pause_policy_jobs(url: str, timeout_s: float = 30.0) -> None:
    """Stop TimescaleDB's scheduler from running policies under a test.

    Migrations add compression, retention and continuous-aggregate policies,
    and the background scheduler starts running them on the fresh database
    within seconds. A test that then refreshes a cagg or runs a policy itself
    races that job: CI failed intermittently with LockNotAvailable ("concurrent
    refresh") in test_caggs_answer_after_refresh and DeadlockDetected in
    test_retention_policy_drops_aged_chunk, on PRs that touched no Python.

    Unscheduling keeps every job defined (tests can still inspect them and
    `CALL run_job(...)` explicitly); then wait out any run that had already
    started before the pause.
    """
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            "SELECT alter_job(job_id, scheduled => false)"
            " FROM timescaledb_information.jobs WHERE job_id >= 1000"
        )
        deadline = time.monotonic() + timeout_s
        while conn.execute(
            "SELECT count(*) FROM timescaledb_information.job_stats"
            " WHERE job_status = 'Running'"
        ).fetchone()[0]:
            if time.monotonic() > deadline:
                raise RuntimeError("TimescaleDB policy job still running after pause")
            time.sleep(0.1)


@pytest.fixture()
def agent_ro_url(migrated_db: str) -> str:
    """The same database as `migrated_db`, as the agent query layer sees it:
    caib_agent_ro, five readable tables, read-only by role default."""
    base, _, dbname = migrated_db.rpartition("/")
    host = base.rpartition("@")[2]
    return f"postgresql://caib_agent_ro:{AGENT_RO_PASSWORD}@{host}/{dbname}"


# Seed data for the agent query layer tests. Counts are load-bearing — the
# assertions in tests/test_query_core.py name them — so change the numbers and
# the tests together. Everything goes in through ingest.landing and the real
# loader rather than straight into caib.events, so search_text, the dedup hash
# and the outcome/severity checks are all built the way production builds them.
SEED_EVENTS = (
    # (module, actor, action, outcome, severity, target, minutes_ago, raw)
    ("aws_cloudtrail", "alice@example.com", "GetObject", "success", "info",
     "arn:aws:s3:::foo-prod/a", 5, None),
    ("aws_cloudtrail", "alice@example.com", "GetObject", "success", "info",
     "arn:aws:s3:::foo-prod/b", 10, None),
    ("aws_cloudtrail", "alice@example.com", "GetObject", "success", "info",
     "arn:aws:s3:::foo-prod/c", 15, None),
    ("aws_cloudtrail", "alice@example.com", "GetObject", "failure", "medium",
     "arn:aws:s3:::foo-prod/d", 20, None),
    ("aws_cloudtrail", "bob@example.com", "DeleteBucket", "failure", "high",
     "arn:aws:s3:::bar-staging", 25, None),
    ("aws_cloudtrail", "bob@example.com", "DeleteBucket", "failure", "high",
     "arn:aws:s3:::bar-staging", 30, None),
    ("gcp_audit", "svc@proj.iam.gserviceaccount.com", "SetIamPolicy", "success", "high",
     "projects/proj", 35, {"protoPayload": {"secret": "unsanitised-vendor-blob"}}),
    # Outside the default 24h window, inside the 90d retention floor: proves the
    # window filter bites and gives the wider-window assertions something to find.
    ("aws_cloudtrail", "carol@example.com", "GetObject", "success", "info",
     "arn:aws:s3:::foo-prod/old", 40 * 24 * 60, None),
)

SEED_ALERTS = (
    # (rule_id, dedup_key, severity, title, status, last_seen_minutes_ago)
    ("aws_cloudtrail.admin_policy_attached", "k1", "high", "Admin policy attached",
     "open", 10),
    ("aws_cloudtrail.admin_policy_attached", "k2", "low", "Admin policy attached",
     "closed", 60),
    ("gcp_audit.iam_policy_changed", "k3", "critical", "IAM policy changed", "ack", 5),
)


@pytest.fixture()
def seeded_db(migrated_db: str) -> str:
    """`migrated_db` with a known set of events, alerts and heartbeats."""
    from caib_core import loader
    from psycopg.types.json import Jsonb

    now = datetime.datetime.now(datetime.UTC)
    with psycopg.connect(migrated_db) as conn:
        for i, (module, actor, action, outcome, severity, target, ago, raw) in enumerate(
            SEED_EVENTS
        ):
            payload = {
                "event_time": (now - datetime.timedelta(minutes=ago)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                "event_id": f"seed-{i}",
                "actor": actor,
                "action": action,
                "target": target,
                "src_ip": "203.0.113.5",
                "outcome": outcome,
                "severity": severity,
                "attrs": {"aws_region": "eu-central-1"},
            }
            if raw is not None:
                payload["raw"] = raw
            conn.execute(
                "INSERT INTO ingest.landing (received_at, module, payload)"
                " VALUES (now(), %s, %s)",
                (module, Jsonb(payload)),
            )
        conn.commit()
        moved, inserted, dead = loader.run_batch(conn, 500, drop_raw=False)
        assert (moved, inserted, dead) == (len(SEED_EVENTS), len(SEED_EVENTS), 0)

        for rule_id, dedup_key, severity, title, status, ago in SEED_ALERTS:
            conn.execute(
                "INSERT INTO detect.alerts (rule_id, dedup_key, severity, title,"
                " first_seen, last_seen, event_count, status)"
                " VALUES (%s, %s, %s, %s, now() - interval '2 hours',"
                "         now() - make_interval(mins => %s), 3, %s)",
                (rule_id, dedup_key, severity, title, ago, status),
            )
        conn.execute("INSERT INTO ops.heartbeat (service) VALUES ('loader'), ('detect')")
        conn.commit()
    return migrated_db


@pytest.fixture()
def webhook_capture():
    """Local HTTP server capturing JSON POSTs: yields (url, received_list)."""
    import http.server
    import json
    import threading

    received: list[dict] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append(json.loads(body))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/hook", received
    server.shutdown()


@pytest.fixture()
def dummy_ctx() -> Ctx:
    """A Ctx for pollers that never touch HTTP in the test."""
    return Ctx(
        module="testmod",
        backfill_start=datetime.datetime(2026, 8, 23, tzinfo=datetime.UTC),
        log=logging.getLogger("test"),
        http=None,
    )
