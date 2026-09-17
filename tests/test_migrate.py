import pathlib
import uuid

import psycopg
import pytest
from caib_core import migrate


@pytest.fixture()
def role_passwords(monkeypatch):
    monkeypatch.setenv("CAIB_PG_APP_PASSWORD", "app-test-pw")
    monkeypatch.setenv("CAIB_PG_RO_PASSWORD", "ro-test-pw")
    monkeypatch.setenv("CAIB_AGENT_RO_PASSWORD", "agent-ro-test-pw")


def test_migrate_bootstrap_and_idempotency(pg_db_url: str, role_passwords, tmp_path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_smoke.sql").write_text(
        "CREATE SCHEMA IF NOT EXISTS ingest;"
        "CREATE TABLE ingest.smoke (id int);"
    )

    assert migrate.run(pg_db_url, migrations) == 1
    # Re-run: nothing to apply, nothing breaks.
    assert migrate.run(pg_db_url, migrations) == 0

    with psycopg.connect(pg_db_url) as conn:
        versions = [r[0] for r in conn.execute("SELECT version FROM caib.schema_migrations")]
        assert versions == ["0001_smoke.sql"]
        owner = conn.execute(
            "SELECT tableowner FROM pg_tables"
            " WHERE schemaname = 'ingest' AND tablename = 'smoke'"
        ).fetchone()[0]
        assert owner == "caib_app"


def test_roles_can_log_in_with_synced_passwords(pg_db_url: str, role_passwords) -> None:
    migrate.run(pg_db_url, pathlib.Path("/nonexistent"))
    base = pg_db_url.rsplit("@", 1)[1]
    roles = (
        ("caib_app", "app-test-pw"),
        ("caib_ro", "ro-test-pw"),
        ("caib_agent_ro", "agent-ro-test-pw"),
    )
    for role, pw in roles:
        with psycopg.connect(f"postgresql://{role}:{pw}@{base}") as conn:
            assert conn.execute("SELECT current_user").fetchone()[0] == role


def test_agent_role_session_defaults_are_set(pg_db_url: str, role_passwords) -> None:
    """The caps that hold for anything holding caib_agent_ro's credentials,
    not just for the code in caib_agentapi. Bootstrap re-applies them on every
    migrate, so an operator who resets them gets them back."""
    migrate.run(pg_db_url, pathlib.Path("/nonexistent"))
    base = pg_db_url.rsplit("@", 1)[1]
    with psycopg.connect(f"postgresql://caib_agent_ro:agent-ro-test-pw@{base}") as conn:
        settings = {
            guc: conn.execute(f"SHOW {guc}").fetchone()[0]
            for guc, _ in migrate.AGENT_ROLE_SETTINGS
        }
    assert settings == {
        "statement_timeout": "15s",
        "idle_in_transaction_session_timeout": "30s",
        "default_transaction_read_only": "on",
    }


def test_duplicate_migration_numbers_rejected(pg_db_url: str, role_passwords, tmp_path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_a.sql").write_text("SELECT 1;")
    (migrations / "0001_b.sql").write_text("SELECT 1;")
    with pytest.raises(SystemExit):
        migrate.run(pg_db_url, migrations)


def test_migrate_updates_an_older_timescaledb_extension(pg_server_url: str, role_passwords):
    """A volume created on an older image stays on the older extension after
    an image bump until ALTER EXTENSION ... UPDATE runs as the first statement
    of a session. migrate must do that, and stay a no-op once current."""
    dbname = f"caib_t_ext_{uuid.uuid4().hex[:10]}"
    base, _, _ = pg_server_url.rpartition("/")
    url = f"{base}/{dbname}"
    with psycopg.connect(pg_server_url, autocommit=True) as conn:
        # template0: the timescale image pre-installs the current version in
        # template1, which would leave nothing to update.
        conn.execute(f'CREATE DATABASE "{dbname}" TEMPLATE template0')
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            default, older = conn.execute(
                "SELECT (SELECT default_version FROM pg_available_extensions"
                "        WHERE name = 'timescaledb'),"
                "       array_agg(version ORDER BY string_to_array(version, '.')::int[] DESC)"
                " FROM pg_available_extension_versions"
                " WHERE name = 'timescaledb' AND version ~ '^[0-9]+[.][0-9]+[.][0-9]+$'"
                "   AND string_to_array(version, '.')::int[] < string_to_array("
                "     (SELECT default_version FROM pg_available_extensions"
                "      WHERE name = 'timescaledb'), '.')::int[]"
            ).fetchone()
        installed = None
        for candidate in older or []:
            try:
                # Fresh session per attempt: a failed load poisons the session.
                with psycopg.connect(url, autocommit=True) as conn:
                    conn.execute(f"CREATE EXTENSION timescaledb VERSION '{candidate}'")
                installed = candidate
                break
            except psycopg.Error:
                continue
        assert installed, f"image ships no installable timescaledb older than {default}"

        migrate.run(url, pathlib.Path("/nonexistent"))
        migrate.run(url, pathlib.Path("/nonexistent"))  # already current: no-op

        with psycopg.connect(url) as conn:
            assert conn.execute(
                "SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'"
            ).fetchone()[0] == default
    finally:
        with psycopg.connect(pg_server_url, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')

