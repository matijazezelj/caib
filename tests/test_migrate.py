import pathlib

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
