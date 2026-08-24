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

TSDB_IMAGE = "timescale/timescaledb:2.29.2-pg18"
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


@pytest.fixture()
def migrated_db(pg_db_url: str, monkeypatch) -> str:
    monkeypatch.setenv("CAIB_PG_APP_PASSWORD", "app-test-pw")
    monkeypatch.setenv("CAIB_PG_RO_PASSWORD", "ro-test-pw")
    migrate.run(pg_db_url, MIGRATIONS_DIR)
    return pg_db_url


@pytest.fixture()
def dummy_ctx() -> Ctx:
    """A Ctx for pollers that never touch HTTP in the test."""
    return Ctx(
        module="testmod",
        backfill_start=datetime.datetime(2026, 8, 23, tzinfo=datetime.UTC),
        log=logging.getLogger("test"),
        http=None,
    )
