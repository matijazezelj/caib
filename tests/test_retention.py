"""Retention for the three plain tables Timescale's policies don't reach."""

import psycopg
import pytest
from caib_core import retention
from psycopg.types.json import Jsonb

PAYLOAD = Jsonb({"alert_id": 1, "kind": "new"})


def seed(conn: psycopg.Connection) -> None:
    """One aged row and one young row in each swept table, plus an aged alert
    in every status."""
    conn.execute(
        "INSERT INTO detect.outbox (created_at, alert_id, kind, payload, sent_at) VALUES"
        " (now() - interval '90 days', 1, 'new', %(p)s, now() - interval '90 days'),"
        " (now(), 2, 'new', %(p)s, now()),"
        # Aged but never delivered: the drain still owes this one.
        " (now() - interval '90 days', 3, 'new', %(p)s, NULL)",
        {"p": PAYLOAD},
    )
    for i, (status, age) in enumerate(
        [("closed", "200 days"), ("open", "200 days"), ("ack", "200 days"),
         ("closed", "1 day")]
    ):
        conn.execute(
            "INSERT INTO detect.alerts"
            " (rule_id, dedup_key, severity, title, first_seen, last_seen, status)"
            f" VALUES ('x.r', %s, 'low', 't', now() - interval '{age}',"
            f" now() - interval '{age}', %s)",
            (f"k{i}", status),
        )
    conn.execute(
        "INSERT INTO ingest.dead_letter (received_at, module, payload, reason) VALUES"
        " (now() - interval '90 days', 'x', '{}'::jsonb, 'old'),"
        " (now(), 'x', '{}'::jsonb, 'fresh')"
    )
    conn.commit()


def counts(conn: psycopg.Connection) -> tuple[int, int, int]:
    return tuple(
        conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        for t in ("detect.outbox", "detect.alerts", "ingest.dead_letter")
    )


def test_sweep_deletes_only_aged_out_rows(migrated_db):
    with psycopg.connect(migrated_db) as conn:
        seed(conn)
        assert counts(conn) == (3, 4, 2)

        deleted = retention.sweep(conn, retention.Cfg())
        conn.commit()
        assert deleted == {"outbox": 1, "alerts": 1, "dead_letter": 1}
        assert counts(conn) == (2, 3, 1)

        # The aged undelivered outbox row survives: retention must never eat a
        # notification the drain hasn't sent yet.
        assert conn.execute(
            "SELECT count(*) FROM detect.outbox WHERE sent_at IS NULL"
        ).fetchone()[0] == 1
        # Only the closed alert went; an old open case is still an open case.
        assert sorted(
            r[0] for r in conn.execute("SELECT status FROM detect.alerts")
        ) == ["ack", "closed", "open"]

        # Idempotent: a second sweep has nothing left to do.
        assert retention.sweep(conn, retention.Cfg()) == {
            "outbox": 0, "alerts": 0, "dead_letter": 0
        }
        conn.commit()


@pytest.mark.parametrize(
    "cfg",
    [
        retention.Cfg(outbox_days=0, alerts_closed_days=0, dead_letter_days=0),
        retention.Cfg(outbox_days=-1, alerts_closed_days=-1, dead_letter_days=-1),
    ],
)
def test_zero_disables_every_sweep(migrated_db, cfg):
    with psycopg.connect(migrated_db) as conn:
        seed(conn)
        assert retention.sweep(conn, cfg) == {
            "outbox": 0, "alerts": 0, "dead_letter": 0
        }
        conn.commit()
        assert counts(conn) == (3, 4, 2)


def test_cfg_from_env_reads_every_knob(monkeypatch):
    for var in (
        "CAIB_RETENTION_OUTBOX_DAYS",
        "CAIB_RETENTION_ALERTS_CLOSED_DAYS",
        "CAIB_RETENTION_DEAD_LETTER_DAYS",
    ):
        monkeypatch.delenv(var, raising=False)
    assert retention.Cfg.from_env() == retention.Cfg(30, 90, 30)

    monkeypatch.setenv("CAIB_RETENTION_OUTBOX_DAYS", "7")
    monkeypatch.setenv("CAIB_RETENTION_ALERTS_CLOSED_DAYS", "0")
    monkeypatch.setenv("CAIB_RETENTION_DEAD_LETTER_DAYS", "14")
    assert retention.Cfg.from_env() == retention.Cfg(7, 0, 14)


def test_sweep_defaults_to_the_environment(migrated_db, monkeypatch):
    """sweep(conn) with no Cfg is what `caib-core retention --once` runs."""
    monkeypatch.setenv("CAIB_RETENTION_OUTBOX_DAYS", "1")
    monkeypatch.setenv("CAIB_RETENTION_ALERTS_CLOSED_DAYS", "0")
    monkeypatch.setenv("CAIB_RETENTION_DEAD_LETTER_DAYS", "1")
    with psycopg.connect(migrated_db) as conn:
        seed(conn)
        assert retention.sweep(conn) == {"outbox": 1, "alerts": 0, "dead_letter": 1}
        conn.commit()
        assert counts(conn) == (2, 4, 1)
