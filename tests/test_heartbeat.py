import psycopg
import pytest
from caib_core import heartbeat


def test_beat_is_an_upsert(migrated_db):
    with psycopg.connect(migrated_db) as conn:
        heartbeat.beat(conn, "detect")
        first = conn.execute("SELECT beat_at FROM ops.heartbeat").fetchone()[0]
        heartbeat.beat(conn, "detect")
        rows = conn.execute("SELECT service, beat_at FROM ops.heartbeat").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "detect"
    assert rows[0][1] >= first


def test_age_is_none_until_the_service_beats(migrated_db):
    with psycopg.connect(migrated_db) as conn:
        assert heartbeat.age_s(conn, "detect") is None
        heartbeat.beat(conn, "detect")
        assert heartbeat.age_s(conn, "detect") < 5


def test_healthcheck_passes_while_fresh_and_fails_once_stale(migrated_db, monkeypatch):
    monkeypatch.setenv("CAIB_PG_URL", migrated_db)
    with psycopg.connect(migrated_db) as conn:
        heartbeat.beat(conn, "loader")
        conn.commit()

    heartbeat.main(["--service", "loader", "--max-age", "120"])  # exits 0

    with psycopg.connect(migrated_db) as conn:
        conn.execute(
            "UPDATE ops.heartbeat SET beat_at = now() - interval '10 minutes'"
            " WHERE service = 'loader'"
        )
        conn.commit()
    with pytest.raises(SystemExit, match="last beat"):
        heartbeat.main(["--service", "loader", "--max-age", "120"])


def test_healthcheck_fails_when_the_service_never_ran(migrated_db, monkeypatch):
    monkeypatch.setenv("CAIB_PG_URL", migrated_db)
    with pytest.raises(SystemExit, match="no heartbeat"):
        heartbeat.main(["--service", "detect", "--max-age", "120"])


def test_grafana_role_can_read_heartbeats(migrated_db):
    """The Overview panel reads ops.heartbeat as caib_ro."""
    with psycopg.connect(migrated_db) as conn:
        heartbeat.beat(conn, "detect")
        conn.commit()
    base, _, dbname = migrated_db.rpartition("/")
    host = base.rpartition("@")[2]
    with psycopg.connect(f"postgresql://caib_ro:ro-test-pw@{host}/{dbname}") as ro:
        assert ro.execute("SELECT count(*) FROM ops.heartbeat").fetchone()[0] == 1
