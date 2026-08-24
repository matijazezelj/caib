"""Phase 8 analyst search: the trgm index accelerates ILIKE over search_text
on the hot (uncompressed) window, and migration 0005 applies cleanly to a
database that already has compressed chunks — the real upgrade path."""

import datetime
import json
import pathlib
import shutil

import psycopg
from caib_core import loader, migrate
from psycopg.types.json import Jsonb

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parent.parent / "db" / "migrations"
UTC = datetime.UTC


def land_event(conn, *, event_id: str, target: str, minutes_ago: int = 5,
               days_ago: int = 0) -> None:
    ts = datetime.datetime.now(UTC) - datetime.timedelta(days=days_ago, minutes=minutes_ago)
    payload = {
        "event_time": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "event_id": event_id,
        "actor": "alice@example.com",
        "action": "GetObject",
        "target": target,
        "outcome": "success",
        "severity": "info",
        "attrs": {"aws_region": "eu-central-1"},
    }
    conn.execute(
        "INSERT INTO ingest.landing (received_at, module, payload) VALUES (now(), %s, %s)",
        ("aws_cloudtrail", Jsonb(payload)),
    )
    conn.commit()


SEARCH_SQL = (
    "SELECT event_time, actor, target FROM caib.events"
    " WHERE search_text ILIKE '%%' || %(q)s || '%%'"
    " ORDER BY event_time DESC"
)


def test_search_finds_and_uses_trgm_index(migrated_db):
    with psycopg.connect(migrated_db) as conn:
        land_event(conn, event_id="hit-1", target="arn:aws:s3:::foo-prod/config.json")
        land_event(conn, event_id="hit-2", target="arn:aws:s3:::foo-prod/creds.json")
        land_event(conn, event_id="miss-1", target="arn:aws:s3:::bar-staging/x")
        loader.run_batch(conn, 100, drop_raw=False)

        rows = conn.execute(SEARCH_SQL, {"q": "foo-prod"}).fetchall()
        assert len(rows) == 2
        assert all("foo-prod" in r[2] for r in rows)

        # Prove the trgm index is usable for the ILIKE plan (tiny tables would
        # otherwise seqscan, hiding a broken index).
        conn.execute("SET enable_seqscan = off")
        plan = json.dumps(
            conn.execute(
                "EXPLAIN (FORMAT JSON) " + SEARCH_SQL, {"q": "foo-prod"}
            ).fetchone()[0]
        )
        assert "search_trgm" in plan, plan


def test_0005_applies_over_existing_compressed_chunks(pg_db_url, monkeypatch, tmp_path):
    monkeypatch.setenv("CAIB_PG_APP_PASSWORD", "app-test-pw")
    monkeypatch.setenv("CAIB_PG_RO_PASSWORD", "ro-test-pw")

    staged = tmp_path / "migrations"
    staged.mkdir()
    for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if not f.name.startswith("0005"):
            shutil.copy(f, staged / f.name)
    migrate.run(pg_db_url, staged)

    with psycopg.connect(pg_db_url, autocommit=True) as conn:
        land_event(conn, event_id="old-1", target="arn:aws:s3:::foo-prod/old", days_ago=30)
        loader.run_batch(conn, 100, drop_raw=False)
        assert conn.execute(
            "SELECT count(compress_chunk(c)) FROM show_chunks('caib.events') c"
        ).fetchone()[0] == 1

    # The upgrade: 0005 lands on a DB whose hypertable has a compressed chunk.
    shutil.copy(MIGRATIONS_DIR / "0005_search_trgm.sql", staged / "0005_search_trgm.sql")
    assert migrate.run(pg_db_url, staged) == 1

    with psycopg.connect(pg_db_url) as conn:
        assert conn.execute(
            "SELECT count(*) FROM pg_indexes"
            " WHERE schemaname = 'caib' AND indexname = 'events_search_trgm'"
        ).fetchone()[0] == 1
        # Compressed history is still searchable (columnstore scan, no index).
        rows = conn.execute(SEARCH_SQL, {"q": "foo-prod"}).fetchall()
        assert len(rows) == 1
