"""The Apache-only TimescaleDB build lacks compression, caggs, and retention
policies; CAIB requires the TSL build (spec: Licensing note)."""

import psycopg


def test_timescaledb_license_is_tsl(pg_server_url: str) -> None:
    with psycopg.connect(pg_server_url) as conn:
        license_ = conn.execute("SHOW timescaledb.license").fetchone()[0]
    assert license_ == "timescale"


def test_postgres_major_version(pg_server_url: str) -> None:
    with psycopg.connect(pg_server_url) as conn:
        major = conn.execute("SHOW server_version_num").fetchone()[0]
    assert int(major) // 10000 == 18
