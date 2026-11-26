"""CAIB_EVENTS_RETENTION_DAYS / CAIB_CAGG_RETENTION_DAYS, applied by migrate."""

import datetime
import pathlib

import psycopg
import pytest
from caib_core import events_retention, loader, migrate
from caib_core.events_retention import RetentionConfigError, current_policy
from test_loader import counts, envelope, land

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parent.parent / "db" / "migrations"
UTC = datetime.UTC
RELS = ("caib.events", "caib.events_5m", "caib.actors_1h")


def policies(url: str) -> dict[str, tuple[int, float] | None]:
    with psycopg.connect(url) as conn:
        return {rel: current_policy(conn, rel) for rel in RELS}


def days(url: str) -> dict[str, float | None]:
    return {rel: (p[1] if p else None) for rel, p in policies(url).items()}


def remigrate(url: str, monkeypatch, **env: str) -> None:
    for var in (events_retention.EVENTS_VAR, events_retention.CAGG_VAR):
        monkeypatch.delenv(var, raising=False)
    for var, value in env.items():
        monkeypatch.setenv(var, value)
    migrate.run(url, MIGRATIONS_DIR)


def test_default_is_unchanged(migrated_db: str) -> None:
    """No knobs set: the migrations' 90 / 400 days, exactly as before."""
    assert days(migrated_db) == {
        "caib.events": 90.0, "caib.events_5m": 400.0, "caib.actors_1h": 400.0,
    }


def test_custom_days_applied_in_place(migrated_db: str, monkeypatch) -> None:
    before = policies(migrated_db)
    remigrate(migrated_db, monkeypatch,
              CAIB_EVENTS_RETENTION_DAYS="365", CAIB_CAGG_RETENTION_DAYS="730")
    after = policies(migrated_db)
    assert days(migrated_db) == {
        "caib.events": 365.0, "caib.events_5m": 730.0, "caib.actors_1h": 730.0,
    }
    # alter_job, not remove+add: same jobs, still paused by the fixture.
    assert {r: p[0] for r, p in after.items()} == {r: p[0] for r, p in before.items()}
    with psycopg.connect(migrated_db) as conn:
        assert not conn.execute(
            "SELECT bool_or(scheduled) FROM timescaledb_information.jobs"
            " WHERE proc_name = 'policy_retention'"
        ).fetchone()[0]


def test_zero_removes_the_policies(migrated_db: str, monkeypatch) -> None:
    remigrate(migrated_db, monkeypatch,
              CAIB_EVENTS_RETENTION_DAYS="0", CAIB_CAGG_RETENTION_DAYS="0")
    assert days(migrated_db) == dict.fromkeys(RELS)
    # And back: a policy is re-added where none exists, owned like the others.
    remigrate(migrated_db, monkeypatch, CAIB_EVENTS_RETENTION_DAYS="120")
    assert days(migrated_db) == {
        "caib.events": 120.0, "caib.events_5m": None, "caib.actors_1h": None,
    }
    with psycopg.connect(migrated_db) as conn:
        owner = conn.execute(
            "SELECT owner FROM timescaledb_information.jobs"
            " WHERE proc_name = 'policy_retention' AND hypertable_name = 'events'"
        ).fetchone()[0]
    assert owner == "caib_app"


def test_events_zero_raises_caggs_to_never(migrated_db: str, monkeypatch) -> None:
    """Raw kept forever but caggs still at 400 days would blank the dashboards
    for older ranges, so unset caggs follow raw up."""
    remigrate(migrated_db, monkeypatch, CAIB_EVENTS_RETENTION_DAYS="0")
    assert days(migrated_db) == dict.fromkeys(RELS)


def test_long_events_raise_unset_caggs(migrated_db: str, monkeypatch) -> None:
    remigrate(migrated_db, monkeypatch, CAIB_EVENTS_RETENTION_DAYS="2555")
    assert days(migrated_db) == dict.fromkeys(RELS, 2555.0)


def test_rerun_is_idempotent(migrated_db: str, monkeypatch) -> None:
    env = {"CAIB_EVENTS_RETENTION_DAYS": "180", "CAIB_CAGG_RETENTION_DAYS": "0"}
    remigrate(migrated_db, monkeypatch, **env)
    first = policies(migrated_db)
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        assert events_retention.sync_retention(conn, env) == {
            "caib.events": False, "caib.events_5m": False, "caib.actors_1h": False,
        }
    remigrate(migrated_db, monkeypatch, **env)
    assert policies(migrated_db) == first


def test_unset_leaves_a_hand_tuned_policy_alone(migrated_db: str, monkeypatch) -> None:
    """Deployments that followed the old docs raised retention by hand. A
    migrate with the knob unset must not cut that back to 90 days."""
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        conn.execute("SELECT remove_retention_policy('caib.events')")
        conn.execute("SELECT add_retention_policy('caib.events', drop_after => INTERVAL '200 days')")
    remigrate(migrated_db, monkeypatch)
    assert days(migrated_db)["caib.events"] == 200.0
    remigrate(migrated_db, monkeypatch, CAIB_EVENTS_RETENTION_DAYS="")
    assert days(migrated_db)["caib.events"] == 200.0


def test_changed_value_takes_effect_on_existing_db(migrated_db: str, monkeypatch) -> None:
    """Under the default 90 days a 60-day-old chunk survives the policy; after
    lowering to 30 on the same database, the same job drops it."""
    aged = (datetime.datetime.now(UTC) - datetime.timedelta(days=60)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        land(conn, [envelope(event_time=aged), envelope(event_id="recent-1")])
        loader.run_batch(conn, 100, drop_raw=False)
        job_id = current_policy(conn, "caib.events")[0]
        conn.execute("CALL run_job(%s)", (job_id,))
        assert counts(conn)[0] == 2

    remigrate(migrated_db, monkeypatch,
              CAIB_EVENTS_RETENTION_DAYS="30", CAIB_CAGG_RETENTION_DAYS="400")
    with psycopg.connect(migrated_db, autocommit=True) as conn:
        assert current_policy(conn, "caib.events") == (job_id, 30.0)
        conn.execute("CALL run_job(%s)", (job_id,))
        assert counts(conn)[0] == 1


@pytest.mark.parametrize(("env", "match"), [
    ({"CAIB_EVENTS_RETENTION_DAYS": "ninety"}, "whole number of days"),
    ({"CAIB_EVENTS_RETENTION_DAYS": "90.5"}, "whole number of days"),
    ({"CAIB_EVENTS_RETENTION_DAYS": "-1"}, "between 0"),
    ({"CAIB_EVENTS_RETENTION_DAYS": "365000"}, "between 0"),
    ({"CAIB_EVENTS_RETENTION_DAYS": "3"}, "at least 7 days"),
    ({"CAIB_CAGG_RETENTION_DAYS": "abc"}, "whole number of days"),
    # Shorter than raw: dashboards would go blank over stored data.
    ({"CAIB_EVENTS_RETENTION_DAYS": "365", "CAIB_CAGG_RETENTION_DAYS": "200"}, "shorter than"),
    ({"CAIB_EVENTS_RETENTION_DAYS": "0", "CAIB_CAGG_RETENTION_DAYS": "400"}, "shorter than"),
    # Unset events: checked against the database's current 90 days.
    ({"CAIB_CAGG_RETENTION_DAYS": "30"}, "shorter than"),
])
def test_nonsense_is_refused_and_changes_nothing(migrated_db: str, monkeypatch, env, match):
    before = policies(migrated_db)
    with pytest.raises(RetentionConfigError, match=match):
        remigrate(migrated_db, monkeypatch, **env)
    assert policies(migrated_db) == before


def test_partial_schema_is_skipped(pg_db_url: str, monkeypatch, tmp_path) -> None:
    """migrate run before caib.events exists (tests do this) doesn't fail."""
    monkeypatch.setenv("CAIB_EVENTS_RETENTION_DAYS", "365")
    migrate.run(pg_db_url, tmp_path)
