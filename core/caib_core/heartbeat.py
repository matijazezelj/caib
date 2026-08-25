"""Liveness beacons for the long-running loops, and the check that reads them.

`caib-core healthcheck --service detect --max-age 120` exits non-zero when the
service's last beat is older than --max-age, which is what makes it usable as a
compose healthcheck. Note what a healthcheck can and cannot do: Docker marks
the container unhealthy, it does not restart it and it does not page anyone.
Point an external monitor at it (see docs/DEPLOY.md).
"""

import argparse
import os

import psycopg

BEAT_SQL = """
INSERT INTO ops.heartbeat (service, beat_at) VALUES (%s, now())
ON CONFLICT (service) DO UPDATE SET beat_at = now()
"""


def beat(conn: psycopg.Connection, service: str) -> None:
    with conn.transaction():
        conn.execute(BEAT_SQL, (service,))


def age_s(conn: psycopg.Connection, service: str) -> float | None:
    """Seconds since the service's last beat, or None if it never beat."""
    row = conn.execute(
        "SELECT extract(epoch FROM now() - beat_at) FROM ops.heartbeat WHERE service = %s",
        (service,),
    ).fetchone()
    return float(row[0]) if row else None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="caib-core healthcheck")
    parser.add_argument("--service", required=True, help="loader | detect")
    parser.add_argument("--max-age", type=float, default=120.0, help="seconds (default 120)")
    args = parser.parse_args(argv)

    with psycopg.connect(os.environ["CAIB_PG_URL"], connect_timeout=5) as conn:
        age = age_s(conn, args.service)
    if age is None:
        raise SystemExit(f"{args.service}: no heartbeat recorded")
    if age > args.max_age:
        raise SystemExit(
            f"{args.service}: last beat {age:.0f}s ago (max {args.max_age:.0f}s)"
        )
