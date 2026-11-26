import datetime
import hashlib

import psycopg
from caib_core import loader
from psycopg.types.json import Jsonb

UTC = datetime.UTC

# Relative to now, never a literal date: a fixed date eventually ages past the
# 90-day retention policy, and the retention test then drops its "recent" row
# (it did, on 2026-11-21).
RECENT = (datetime.datetime.now(UTC) - datetime.timedelta(hours=1)).replace(microsecond=0)


def envelope(**overrides) -> dict:
    env = {
        "event_time": RECENT.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "event_id": "abc-123",
        "actor": "matija@example.com",
        "src_ip": "203.0.113.7",
        "action": "ConsoleLogin",
        "target": "arn:aws:iam::123456789012:role/x",
        "outcome": "success",
        "severity": "info",
        "attrs": {"user_type": "Root", "aws_region": "eu-central-1"},
        "raw": {"eventID": "abc-123", "eventName": "ConsoleLogin"},
    }
    env.update(overrides)
    return env


def land(conn: psycopg.Connection, envelopes: list[dict], module: str = "testmod") -> None:
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO ingest.landing (received_at, module, payload)"
            " VALUES (now(), %s, %s)",
            [(module, Jsonb(e)) for e in envelopes],
        )
    conn.commit()


def counts(conn: psycopg.Connection) -> tuple[int, int, int]:
    events = conn.execute("SELECT count(*) FROM caib.events").fetchone()[0]
    landing = conn.execute("SELECT count(*) FROM ingest.landing").fetchone()[0]
    dead = conn.execute("SELECT count(*) FROM ingest.dead_letter").fetchone()[0]
    return events, landing, dead


def test_envelope_lands_with_correct_columns(migrated_db: str) -> None:
    with psycopg.connect(migrated_db) as conn:
        land(conn, [envelope()])
        assert loader.run_batch(conn, 100, drop_raw=False) == (1, 1, 0)
        row = conn.execute(
            "SELECT event_time, module, dedup_hash, actor, host(src_ip), action,"
            "       target, outcome, severity, attrs, raw, search_text"
            " FROM caib.events"
        ).fetchone()

    expected_hash = hashlib.sha256(b"testmod:abc-123").digest()
    assert row[0] == RECENT
    assert row[1] == "testmod"
    assert bytes(row[2]) == expected_hash
    assert row[3] == "matija@example.com"
    assert row[4] == "203.0.113.7"
    assert row[5] == "ConsoleLogin"
    assert row[6] == "arn:aws:iam::123456789012:role/x"
    assert row[7] == "success"
    assert row[8] == "info"
    assert row[9] == {"user_type": "Root", "aws_region": "eu-central-1"}
    assert row[10] == {"eventID": "abc-123", "eventName": "ConsoleLogin"}
    assert "matija@example.com" in row[11]
    assert "consolelogin" in row[11]  # lowercased
    assert "root" in row[11]  # attrs values included
    assert "eventid" not in row[11]  # raw is never search-indexed


def test_reinsert_changes_nothing(migrated_db: str) -> None:
    with psycopg.connect(migrated_db) as conn:
        land(conn, [envelope(), envelope(event_id="other-456")])
        assert loader.run_batch(conn, 100, drop_raw=False) == (2, 2, 0)
        land(conn, [envelope(), envelope(event_id="other-456")])
        moved, inserted, dead = loader.run_batch(conn, 100, drop_raw=False)
        assert (moved, inserted, dead) == (2, 0, 0)
        assert counts(conn) == (2, 0, 0)


def test_dedup_against_compressed_chunk(migrated_db: str) -> None:
    old_time = (
        datetime.datetime.now(UTC) - datetime.timedelta(days=30)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        land(conn, [envelope(event_time=old_time)])
        assert loader.run_batch(conn, 100, drop_raw=False) == (1, 1, 0)

        compressed = conn.execute(
            "SELECT count(compress_chunk(c)) FROM show_chunks('caib.events') c"
        ).fetchone()[0]
        assert compressed == 1
        assert conn.execute(
            "SELECT count(*) FROM timescaledb_information.chunks"
            " WHERE hypertable_name = 'events' AND is_compressed"
        ).fetchone()[0] == 1

        land(conn, [envelope(event_time=old_time)])
        moved, inserted, dead = loader.run_batch(conn, 100, drop_raw=False)
        assert (moved, inserted, dead) == (1, 0, 0)
        assert counts(conn)[0] == 1


def test_bad_event_time_goes_to_dead_letter(migrated_db: str) -> None:
    with psycopg.connect(migrated_db) as conn:
        land(conn, [envelope(event_time="not-a-timestamp"), envelope(event_time=None)])
        assert loader.run_batch(conn, 100, drop_raw=False) == (2, 0, 2)
        reasons = [
            r[0] for r in conn.execute("SELECT reason FROM ingest.dead_letter")
        ]
        assert reasons == ["unparseable event_time"] * 2
        assert counts(conn) == (0, 0, 2)


def test_contract_violations_dead_letter_instead_of_poisoning_batch(migrated_db: str) -> None:
    with psycopg.connect(migrated_db) as conn:
        land(
            conn,
            [
                envelope(),  # good row must still land
                envelope(event_id="bad-1", outcome="partial"),
                envelope(event_id="bad-2", severity="urgent"),
                envelope(event_id="bad-3", attrs=["not", "an", "object"]),
            ],
        )
        assert loader.run_batch(conn, 100, drop_raw=False) == (4, 1, 3)
        reasons = {
            r[0] for r in conn.execute("SELECT reason FROM ingest.dead_letter")
        }
        assert reasons == {"invalid outcome", "invalid severity", "attrs not an object"}


def test_drop_raw(migrated_db: str) -> None:
    with psycopg.connect(migrated_db) as conn:
        land(conn, [envelope()])
        loader.run_batch(conn, 100, drop_raw=True)
        assert conn.execute("SELECT raw FROM caib.events").fetchone()[0] is None


def test_retention_policy_drops_aged_chunk(migrated_db: str) -> None:
    aged = (
        datetime.datetime.now(UTC) - datetime.timedelta(days=200)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        land(conn, [envelope(event_time=aged), envelope(event_id="recent-1")])
        loader.run_batch(conn, 100, drop_raw=False)
        assert conn.execute(
            "SELECT count(*) FROM show_chunks('caib.events')"
        ).fetchone()[0] == 2

        job_id = conn.execute(
            "SELECT job_id FROM timescaledb_information.jobs"
            " WHERE proc_name = 'policy_retention' AND hypertable_name = 'events'"
        ).fetchone()[0]
        conn.execute("CALL run_job(%s)", (job_id,))

        assert conn.execute(
            "SELECT count(*) FROM show_chunks('caib.events')"
        ).fetchone()[0] == 1
        assert counts(conn)[0] == 1


def test_caggs_answer_after_refresh(migrated_db: str) -> None:
    base = datetime.datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    envelopes = [
        envelope(
            event_id=f"e-{i}",
            event_time=(base - datetime.timedelta(minutes=7 * i)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            outcome="failure" if i % 3 == 0 else "success",
        )
        for i in range(20)
    ]
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        land(conn, envelopes)
        loader.run_batch(conn, 100, drop_raw=False)
        conn.execute("CALL refresh_continuous_aggregate('caib.events_5m', NULL, NULL)")
        conn.execute("CALL refresh_continuous_aggregate('caib.actors_1h', NULL, NULL)")

        assert conn.execute("SELECT sum(n) FROM caib.events_5m").fetchone()[0] == 20
        actor_row = conn.execute(
            "SELECT sum(n), sum(failures) FROM caib.actors_1h WHERE actor = %s",
            ("matija@example.com",),
        ).fetchone()
        assert actor_row == (20, 7)
