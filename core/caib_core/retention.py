"""Retention for the plain tables a Timescale policy cannot cover.

`caib.events` is a hypertable with an `add_retention_policy`. `detect.outbox`,
`detect.alerts` and `ingest.dead_letter` are plain heaps — `add_retention_policy`
does not apply to them, and nothing else bounded them, so on a busy deployment
all three grow forever.

The sweep is three DELETEs, each in its own transaction, run hourly by the
detect loop and on demand by `caib-core retention --once`. Any knob set to 0
disables its table's sweep (keep everything).

Deliberately conservative about alerts: only `closed` ones are eligible.
Deleting an open or acked alert throws away someone's open case, and it also
un-dedups the rule — the next tick would re-open it as brand new and notify
again.
"""

import argparse
import dataclasses
import logging
import os

import psycopg

log = logging.getLogger("caib.retention")

# The detect loop sweeps at most this often (monotonic clock).
SWEEP_INTERVAL_S = 3600.0


@dataclasses.dataclass
class Cfg:
    outbox_days: int = 30
    alerts_closed_days: int = 90
    dead_letter_days: int = 30

    @classmethod
    def from_env(cls) -> "Cfg":
        return cls(
            outbox_days=int(os.environ.get("CAIB_RETENTION_OUTBOX_DAYS", "30")),
            alerts_closed_days=int(
                os.environ.get("CAIB_RETENTION_ALERTS_CLOSED_DAYS", "90")
            ),
            dead_letter_days=int(os.environ.get("CAIB_RETENTION_DEAD_LETTER_DAYS", "30")),
        )


# (result key, Cfg field, statement). %(age)s binds as a text interval.
SWEEPS = (
    (
        "outbox",
        "outbox_days",
        (
            "DELETE FROM detect.outbox"
            " WHERE sent_at IS NOT NULL AND sent_at < now() - %(age)s::interval"
        ),
    ),
    (
        "alerts",
        "alerts_closed_days",
        (
            "DELETE FROM detect.alerts"
            " WHERE status = 'closed' AND last_seen < now() - %(age)s::interval"
        ),
    ),
    (
        "dead_letter",
        "dead_letter_days",
        "DELETE FROM ingest.dead_letter WHERE received_at < now() - %(age)s::interval",
    ),
)


def sweep(conn: psycopg.Connection, cfg: "Cfg | None" = None) -> dict[str, int]:
    """Delete aged-out rows from the three unbounded plain tables. Returns rows
    deleted per table; a table whose knob is 0 (or negative) is skipped and
    reports 0. One transaction per table: a large DELETE must not hold locks
    across the others."""
    if cfg is None:
        cfg = Cfg.from_env()
    deleted: dict[str, int] = {}
    for name, field, stmt in SWEEPS:
        days = getattr(cfg, field)
        if days <= 0:
            deleted[name] = 0
            continue
        with conn.transaction():
            cur = conn.execute(stmt, {"age": f"{days} days"})
        deleted[name] = cur.rowcount
    return deleted


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    parser = argparse.ArgumentParser(prog="caib-core retention")
    parser.add_argument(
        "--once",
        action="store_true",
        help="run one sweep and exit — the only mode; the detect loop sweeps hourly",
    )
    parser.parse_args(argv)

    cfg = Cfg.from_env()
    with psycopg.connect(os.environ["CAIB_PG_URL"]) as conn:
        deleted = sweep(conn, cfg)
    log.info("swept %s", " ".join(f"{k}={v}" for k, v in deleted.items()))
