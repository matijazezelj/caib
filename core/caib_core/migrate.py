"""Apply db/migrations in order, record them in caib.schema_migrations, sync roles,
then apply the retention knobs (caib_core.events_retention).

Bootstrap (roles, extension, schema_migrations) lives here rather than in a
migration file: CREATE EXTENSION timescaledb must run outside a transaction
block, and role passwords must never appear in repo files (spec: ALTER ROLE
from env, never in migrations).

Migration files run under SET LOCAL ROLE caib_app so caib_app owns every
object; the runner connects as superuser only for bootstrap and grants.
"""

import logging
import os
import pathlib
import re
import time

import psycopg
from psycopg import sql

from caib_core import events_retention

log = logging.getLogger("caib.migrate")

MIGRATION_RE = re.compile(r"^\d{4}_[a-z0-9_]+\.sql$")

# Files whose first line contains this marker run statement-by-statement in
# autocommit instead of one transaction — required for DDL that refuses to run
# in a transaction block (continuous aggregates). Such files must be fully
# idempotent (IF NOT EXISTS everywhere): a crash mid-file re-runs the file.
NO_TX_MARKER = "caib:no-transaction"

DOLLAR_TAG_RE = re.compile(r"\$[A-Za-z_]*\$")

# caib_ro also covers ingest: the Overview dashboard reads ingest.landing and
# ingest.dead_letter (see DECISIONS.md). ops carries the loop heartbeats, also
# on the Overview dashboard.
RO_SCHEMAS = ("caib", "ingest", "detect", "ops")

# The agent query layer's role. Separate from caib_ro on purpose — caib_ro is
# Grafana's, and sync_grants() below keeps handing it SELECT on every table in
# every schema above, ingest.* included. caib_agent_ro's grants are enumerated
# one table at a time in migration 0008 instead. Nothing here grants it
# anything beyond CONNECT.
AGENT_ROLE = "caib_agent_ro"

# Session defaults for that role, so the caps hold for anything holding its
# credentials — psql included, not just caib_agent_api. They live here rather
# than in migration 0008 because ALTER ROLE ... SET on another role needs
# superuser (or CREATEROLE plus ADMIN), and migration files run as caib_app.
# Re-applied on every migrate: this is the enforcement, so let it self-heal.
AGENT_ROLE_SETTINGS = (
    # A runaway query dies in the database, not in whatever is holding the
    # connection. Mirrored by a client-side cap in caib_agentapi, but this is
    # the one that cannot be edited out of the caller.
    ("statement_timeout", "15s"),
    # Belt to the grants' braces: even a SELECT-only role can hold locks and
    # pin xmin through an open transaction.
    ("idle_in_transaction_session_timeout", "30s"),
    # Read-only v1 is a property of the role, not of the code in front of it.
    ("default_transaction_read_only", "on"),
)


def bootstrap(conn: psycopg.Connection) -> None:
    conn.execute(
        """
        DO $$ BEGIN
          IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'caib_app') THEN
            CREATE ROLE caib_app LOGIN;
          END IF;
          IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'caib_ro') THEN
            CREATE ROLE caib_ro LOGIN;
          END IF;
          -- LOGIN, and deliberately passwordless: with no password set, scram
          -- authentication cannot succeed, so the role is inert until an
          -- operator supplies CAIB_AGENT_RO_PASSWORD and sync_passwords() runs.
          IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'caib_agent_ro') THEN
            CREATE ROLE caib_agent_ro LOGIN;
          END IF;
        END $$
        """
    )
    conn.execute(
        sql.SQL("GRANT CREATE ON DATABASE {} TO caib_app").format(
            sql.Identifier(conn.info.dbname)
        )
    )
    # PUBLIC already has CONNECT on a database by default; granting it by name
    # means the role keeps working on a hardened deployment that revoked it.
    conn.execute(
        sql.SQL("GRANT CONNECT ON DATABASE {} TO caib_agent_ro").format(
            sql.Identifier(conn.info.dbname)
        )
    )
    for name, value in AGENT_ROLE_SETTINGS:
        conn.execute(
            sql.SQL("ALTER ROLE {} SET {} = {}").format(
                sql.Identifier(AGENT_ROLE), sql.Identifier(name), sql.Literal(value)
            )
        )
    conn.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
    conn.execute("CREATE SCHEMA IF NOT EXISTS caib AUTHORIZATION caib_app")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS caib.schema_migrations (
          version    text PRIMARY KEY,
          applied_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )


def apply_pending(conn: psycopg.Connection, migrations_dir: pathlib.Path) -> int:
    files = sorted(
        (f for f in migrations_dir.iterdir() if MIGRATION_RE.match(f.name))
        if migrations_dir.exists()
        else []
    )
    prefixes = [f.name[:4] for f in files]
    dupes = {p for p in prefixes if prefixes.count(p) > 1}
    if dupes:
        raise SystemExit(f"duplicate migration numbers: {sorted(dupes)}")

    applied = {
        row[0] for row in conn.execute("SELECT version FROM caib.schema_migrations")
    }
    count = 0
    for f in files:
        if f.name in applied:
            continue
        log.info("applying %s", f.name)
        text = f.read_text()
        first_line = text.lstrip().splitlines()[0] if text.strip() else ""
        if NO_TX_MARKER in first_line:
            conn.execute("SET ROLE caib_app")
            try:
                for stmt in split_statements(text):
                    conn.execute(stmt)
            finally:
                conn.execute("RESET ROLE")
            conn.execute(
                "INSERT INTO caib.schema_migrations (version) VALUES (%s)", (f.name,)
            )
        else:
            with conn.transaction():
                conn.execute("SET LOCAL ROLE caib_app")
                conn.execute(text)
                conn.execute("RESET ROLE")
                conn.execute(
                    "INSERT INTO caib.schema_migrations (version) VALUES (%s)", (f.name,)
                )
        count += 1
    return count


def split_statements(text: str) -> list[str]:
    """Split SQL on top-level semicolons, respecting single quotes,
    dollar-quoting, and line comments. No block-comment support: don't use
    /* */ in no-transaction migration files."""
    statements: list[str] = []
    buf: list[str] = []
    i, n = 0, len(text)
    dollar_tag: str | None = None
    in_string = False
    while i < n:
        ch = text[i]
        if dollar_tag:
            if text.startswith(dollar_tag, i):
                buf.append(dollar_tag)
                i += len(dollar_tag)
                dollar_tag = None
            else:
                buf.append(ch)
                i += 1
        elif in_string:
            buf.append(ch)
            if ch == "'":
                if i + 1 < n and text[i + 1] == "'":
                    buf.append("'")
                    i += 1
                else:
                    in_string = False
            i += 1
        elif ch == "'":
            in_string = True
            buf.append(ch)
            i += 1
        elif ch == "$" and (m := DOLLAR_TAG_RE.match(text, i)):
            dollar_tag = m.group(0)
            buf.append(dollar_tag)
            i = m.end()
        elif text.startswith("--", i):
            end = text.find("\n", i)
            end = n if end == -1 else end
            buf.append(text[i:end])
            i = end
        elif ch == ";":
            statements.append("".join(buf).strip())
            buf = []
            i += 1
        else:
            buf.append(ch)
            i += 1
    if tail := "".join(buf).strip():
        statements.append(tail)

    def is_comment_only(stmt: str) -> bool:
        return all(
            not line.strip() or line.strip().startswith("--") for line in stmt.splitlines()
        )

    return [s for s in statements if not is_comment_only(s)]


def sync_passwords(conn: psycopg.Connection) -> None:
    roles = (
        ("caib_app", "CAIB_PG_APP_PASSWORD"),
        ("caib_ro", "CAIB_PG_RO_PASSWORD"),
        (AGENT_ROLE, "CAIB_AGENT_RO_PASSWORD"),
    )
    for role, var in roles:
        password = os.environ.get(var)
        if not password:
            log.warning("%s not set; password for role %s left unchanged", var, role)
            continue
        conn.execute(
            sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )


def sync_grants(conn: psycopg.Connection) -> None:
    for schema in RO_SCHEMAS:
        if conn.execute(
            "SELECT 1 FROM pg_namespace WHERE nspname = %s", (schema,)
        ).fetchone() is None:
            continue
        ident = sql.Identifier(schema)
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO caib_ro").format(ident))
        conn.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO caib_ro").format(ident))
        conn.execute(
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES FOR ROLE caib_app IN SCHEMA {} "
                "GRANT SELECT ON TABLES TO caib_ro"
            ).format(ident)
        )


def _connect_with_retry(url: str, timeout_s: float) -> psycopg.Connection:
    """HA clusters elect a leader before accepting writes; wait for it instead
    of failing the one-shot migrate container."""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            return psycopg.connect(url, autocommit=True, connect_timeout=5)
        except psycopg.OperationalError as exc:
            if time.monotonic() > deadline:
                raise
            log.info("database not ready (%s); retrying", exc)
            time.sleep(2)


def update_extension(conn: psycopg.Connection) -> None:
    """Bring an existing database's timescaledb extension up to the image's.

    Swapping the image does not upgrade a database: a volume created on
    2.29.2 keeps running the 2.29.2 library the new image still ships, and
    `CREATE EXTENSION IF NOT EXISTS` in bootstrap() does nothing about it.
    `ALTER EXTENSION timescaledb UPDATE` has to be the FIRST statement of a
    session — anything before it, even a catalog read of extversion, loads the
    old library and the update then refuses ("cannot be updated after the old
    version has already been loaded"). Hence a dedicated fresh connection,
    and an unconditional update: it is a no-op NOTICE when already current,
    and "does not exist" on a database bootstrap has not created it in yet.

    Sessions other services opened before this ran (loader, detect,
    agent-api) keep the old library until they reconnect — see docs/DEPLOY.md.
    """
    try:
        conn.execute("ALTER EXTENSION timescaledb UPDATE")
    except psycopg.errors.UndefinedObject:
        return  # fresh database; bootstrap() creates it at the image's version
    version = conn.execute(
        "SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'"
    ).fetchone()[0]
    log.info("timescaledb extension at %s", version)


def run(super_url: str, migrations_dir: pathlib.Path, wait_s: float = 0) -> int:
    def connect() -> psycopg.Connection:
        return (
            _connect_with_retry(super_url, wait_s) if wait_s
            else psycopg.connect(super_url, autocommit=True)
        )

    events_retention.validate_env()
    with connect() as conn:
        update_extension(conn)
    with connect() as conn:
        bootstrap(conn)
        count = apply_pending(conn, migrations_dir)
        sync_passwords(conn)
        sync_grants(conn)
        events_retention.sync_retention(conn)
    return count


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    migrations_dir = pathlib.Path(os.environ.get("CAIB_MIGRATIONS_DIR", "db/migrations"))
    wait_s = float(os.environ.get("CAIB_MIGRATE_WAIT_S", "120"))
    count = run(os.environ["CAIB_PG_SUPER_URL"], migrations_dir, wait_s=wait_s)
    log.info("migrate done: %d migration(s) applied", count)
