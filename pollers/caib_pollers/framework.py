"""Poller framework. Modules implement fetch(); the framework owns everything
else: cursor persistence, HTTP retries, envelope validation, and the one
transaction per batch (landing insert + cursor upsert) that makes a mid-batch
kill safe — either both commit or neither, and dedup downstream makes
at-least-once delivery harmless.
"""

import argparse
import dataclasses
import datetime
import importlib
import logging
import os
import random
import time
from typing import Any, Protocol

import httpx
import psycopg
from psycopg.types.json import Jsonb

HTTP_ATTEMPTS = 6
MAX_BACKOFF_S = 60.0


@dataclasses.dataclass
class FetchResult:
    envelopes: list[dict]  # envelope-contract dicts
    next_cursor: dict      # opaque, persisted verbatim
    has_more: bool = False # True -> loop immediately (backfill)


@dataclasses.dataclass
class Ctx:
    module: str
    backfill_start: datetime.datetime
    log: logging.Logger
    http: httpx.Client

    def get(self, url: str, *, params: dict | None = None, headers: dict | None = None) -> httpx.Response:
        """GET with exponential backoff + jitter; honors Retry-After on 429.
        Network errors and 5xx retry; other 4xx raise immediately (a bad token
        must crash loudly — the module's dead-man rule is the pager for that).
        After HTTP_ATTEMPTS the process crashes; compose restarts it and the
        cursor is unchanged."""
        delay = 1.0
        failure = ""
        for attempt in range(1, HTTP_ATTEMPTS + 1):
            wait = None
            try:
                resp = self.http.get(url, params=params, headers=headers)
                if resp.status_code == 429 or resp.status_code >= 500:
                    failure = f"HTTP {resp.status_code}"
                    retry_after = resp.headers.get("Retry-After", "")
                    wait = float(retry_after) if retry_after.isdigit() else delay
                else:
                    resp.raise_for_status()
                    return resp
            except httpx.TransportError as exc:
                failure = repr(exc)
                wait = delay
            if attempt == HTTP_ATTEMPTS:
                break
            sleep_s = min(wait, MAX_BACKOFF_S)
            self.log.warning(
                "GET %s failed (%s), retry %d/%d in %.1fs",
                url, failure, attempt, HTTP_ATTEMPTS, sleep_s,
            )
            time.sleep(sleep_s + random.uniform(0, sleep_s * 0.25))
            delay = min(delay * 2, MAX_BACKOFF_S)
        raise RuntimeError(f"{self.module}: giving up on GET {url} after {HTTP_ATTEMPTS} attempts ({failure})")


class Poller(Protocol):
    module: str
    interval_s: int

    def fetch(self, cursor: dict | None, ctx: Ctx) -> FetchResult: ...


def validate_envelope(env: Any) -> str | None:
    """Cheap contract check before insert; the loader re-validates the rest.
    Returns a reject reason or None."""
    if not isinstance(env, dict):
        return "envelope not an object"
    if not env.get("event_time"):
        return "missing event_time"
    if not env.get("action"):
        return "missing action"
    if "attrs" in env and env["attrs"] is not None and not isinstance(env["attrs"], dict):
        return "attrs not an object"
    return None


def load_cursor(conn: psycopg.Connection, module: str) -> dict | None:
    row = conn.execute(
        "SELECT cursor FROM ingest.cursors WHERE module = %s", (module,)
    ).fetchone()
    return row[0] if row else None


def run_cycle(conn: psycopg.Connection, poller: Poller, ctx: Ctx) -> tuple[FetchResult, int, int]:
    """One fetch + one transaction (landing + dead_letter + cursor)."""
    cursor = load_cursor(conn, poller.module)
    result = poller.fetch(cursor, ctx)

    valid: list[dict] = []
    invalid: list[tuple[dict, str]] = []
    for env in result.envelopes:
        if reason := validate_envelope(env):
            invalid.append((env, reason))
        else:
            valid.append(env)

    with conn.transaction():
        if valid:
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO ingest.landing (received_at, module, payload)"
                    " VALUES (now(), %s, %s)",
                    [(poller.module, Jsonb(env)) for env in valid],
                )
        for env, reason in invalid:
            conn.execute(
                "INSERT INTO ingest.dead_letter (module, payload, reason)"
                " VALUES (%s, %s, %s)",
                (poller.module, Jsonb(env if isinstance(env, dict) else {"value": env}), reason),
            )
        conn.execute(
            "INSERT INTO ingest.cursors (module, cursor, updated_at)"
            " VALUES (%s, %s, now())"
            " ON CONFLICT (module) DO UPDATE SET cursor = EXCLUDED.cursor, updated_at = now()",
            (poller.module, Jsonb(result.next_cursor)),
        )
    return result, len(valid), len(invalid)


def backfill_start(module: str) -> datetime.datetime:
    raw = os.environ.get(f"CAIB_{module.upper()}_BACKFILL")
    if raw:
        return datetime.datetime.fromisoformat(raw)
    return datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=24)


def run(poller: Poller, once: bool = False) -> None:
    log = logging.getLogger(f"caib.poller.{poller.module}")
    with (
        httpx.Client(timeout=30.0) as http,
        psycopg.connect(os.environ["CAIB_PG_URL"]) as conn,
    ):
        ctx = Ctx(
            module=poller.module,
            backfill_start=backfill_start(poller.module),
            log=log,
            http=http,
        )
        while True:
            t0 = time.monotonic()
            result, landed, dead = run_cycle(conn, poller, ctx)
            log.info(
                "module=%s fetched=%d landed=%d dead=%d has_more=%s duration=%.1fs",
                poller.module, len(result.envelopes), landed, dead,
                result.has_more, time.monotonic() - t0,
            )
            if result.has_more:
                continue
            if once:
                return
            time.sleep(poller.interval_s)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    parser = argparse.ArgumentParser(prog="caib-poller")
    parser.add_argument("--module", default=os.environ.get("CAIB_MODULE"))
    parser.add_argument("--once", action="store_true", help="drain to now, then exit")
    args = parser.parse_args()
    if not args.module:
        raise SystemExit("set CAIB_MODULE or pass --module")
    mod = importlib.import_module(f"caib_pollers.modules.{args.module}")
    run(mod.POLLER, once=args.once)
