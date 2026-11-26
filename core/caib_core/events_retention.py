"""Raw-event and continuous-aggregate retention, set from env at migrate time.

Migration 0002 creates `caib.events` with `drop_after => 90 days` and 0004 gives
both caggs 400 days. Migrations are append-only and the spec says not to
parameterize them, so the knobs live here instead: `migrate` calls
`sync_retention()` after the migrations, the same way it re-syncs roles and
grants on every run.

    CAIB_EVENTS_RETENTION_DAYS  raw events (`caib.events`); 0 = never drop
    CAIB_CAGG_RETENTION_DAYS    `caib.events_5m` and `caib.actors_1h`; 0 = never drop

**Unset (or empty) means "leave the database alone"**, not "reset to the
default". A deployment that followed the old docs and raised retention by hand
with `remove_retention_policy`/`add_retention_policy` must not have it silently
cut back to 90 days — and dropped — by the next `make up`. Fresh databases get
the migrations' 90/400 days, so the defaults are unchanged.

The one change made beyond what was asked: when CAIB_EVENTS_RETENTION_DAYS is
set so that raw events would outlive the caggs (say events at 3 years, caggs
still at 400 days) and CAIB_CAGG_RETENTION_DAYS is unset, the caggs are raised
to match. Dashboards read the caggs for anything wider than ~7 days, so a cagg
shorter than raw shows an empty panel over data that is still on disk. Raising
retention never deletes anything. An *explicit* CAIB_CAGG_RETENTION_DAYS
shorter than raw is refused instead.

An existing policy is changed with `alter_job` (same job id, its run history and
`scheduled` flag kept); one is added only where none exists, and removed for 0.
Re-running with the same values touches nothing. All changes commit together.
"""

import logging
import math
import os
from collections.abc import Mapping

import psycopg

log = logging.getLogger("caib.migrate")

EVENTS_VAR = "CAIB_EVENTS_RETENTION_DAYS"
CAGG_VAR = "CAIB_CAGG_RETENTION_DAYS"

EVENTS = "caib.events"
CAGGS = ("caib.events_5m", "caib.actors_1h")

# Raw events younger than this are the uncompressed hot tier: compression runs
# at 7 days, the dashboards' raw panels span up to 7 days, and detection reads
# recent raw rows. A shorter retention would drop data before any of that is
# done with it.
MIN_EVENTS_DAYS = 7
# Upper bound only to catch typos (an extra zero or two); 0 is "forever".
MAX_DAYS = 36500


class RetentionConfigError(SystemExit):
    """Refused retention settings. A SystemExit so the one-shot migrate
    container exits non-zero with the message instead of a traceback."""


def parse_days(name: str, environ: Mapping[str, str]) -> int | None:
    """A retention knob: None when unset/empty, else whole days (0 = never)."""
    raw = environ.get(name, "").strip()
    if not raw:
        return None
    try:
        days = int(raw)
    except ValueError:
        raise RetentionConfigError(
            f"{name}={raw!r}: expected a whole number of days (0 = never drop)"
        ) from None
    if not 0 <= days <= MAX_DAYS:
        raise RetentionConfigError(
            f"{name}={days}: must be between 0 (never drop) and {MAX_DAYS} days"
        )
    return days


def validate_env(environ: Mapping[str, str] | None = None) -> None:
    """The checks that need no database. migrate calls this before applying
    any migration, so a typo fails the run with nothing changed."""
    env = os.environ if environ is None else environ
    events_days = parse_days(EVENTS_VAR, env)
    parse_days(CAGG_VAR, env)
    _check_events_floor(events_days)


def _check_events_floor(events_days: int | None) -> None:
    if events_days is not None and 0 < events_days < MIN_EVENTS_DAYS:
        raise RetentionConfigError(
            f"{EVENTS_VAR}={events_days}: raw events must be kept at least "
            f"{MIN_EVENTS_DAYS} days (compression, the dashboards' raw panels and "
            "detection all read the last 7 days); use 0 to never drop"
        )


def _span(days: int) -> float:
    """Days as a comparable span: 0 (never drop) is longer than anything."""
    return math.inf if days == 0 else days


def _label(days: float) -> str:
    return "never drop" if days in (0, math.inf) else f"{days:g} days"


def _exists(conn: psycopg.Connection, relation: str) -> bool:
    """Whether the hypertable or cagg exists (migrate also runs on partial
    migration sets, e.g. in tests)."""
    schema, name = relation.split(".")
    return conn.execute(
        "SELECT 1 FROM timescaledb_information.hypertables"
        " WHERE hypertable_schema = %s AND hypertable_name = %s"
        " UNION ALL"
        " SELECT 1 FROM timescaledb_information.continuous_aggregates"
        " WHERE view_schema = %s AND view_name = %s",
        (schema, name, schema, name),
    ).fetchone() is not None


def current_policy(
    conn: psycopg.Connection, relation: str
) -> tuple[int, float] | None:
    """(job_id, drop_after in days) of the relation's retention policy, or None
    when it has none (never drop)."""
    # The jobs view names a cagg's policy by the view, not by its
    # materialization hypertable.
    row = conn.execute(
        "SELECT job_id,"
        " extract(epoch FROM (config->>'drop_after')::interval) / 86400"
        " FROM timescaledb_information.jobs"
        " WHERE proc_name = 'policy_retention'"
        " AND hypertable_schema = %s AND hypertable_name = %s",
        tuple(relation.split(".")),
    ).fetchone()
    return (row[0], float(row[1])) if row else None


def _current_span(conn: psycopg.Connection, relation: str) -> float:
    policy = current_policy(conn, relation)
    return math.inf if policy is None else policy[1]


def _apply(conn: psycopg.Connection, relation: str, days: int) -> bool:
    """Make the relation's policy say `days` (0 = remove it). True if changed."""
    policy = current_policy(conn, relation)
    if days == 0:
        if policy is None:
            return False
        conn.execute(
            "SELECT remove_retention_policy(%s::regclass, if_exists => true)", (relation,)
        )
        log.warning("%s: retention policy removed (was %s) — raw data is kept forever",
                    relation, _label(policy[1]))
        return True
    if policy is None:
        conn.execute(
            "SELECT add_retention_policy(%s::regclass,"
            " drop_after => make_interval(days => %s), if_not_exists => true)",
            (relation, days),
        )
        log.info("%s: retention policy added: %s", relation, _label(days))
        return True
    job_id, was = policy
    if was == days:
        return False
    conn.execute(
        "SELECT alter_job(job_id,"
        " config => jsonb_set(config, '{drop_after}', to_jsonb(%s::text)))"
        " FROM timescaledb_information.jobs WHERE job_id = %s",
        (f"{days} days", job_id),
    )
    level = logging.WARNING if days < was else logging.INFO
    log.log(level, "%s: retention changed %s -> %s%s", relation, _label(was), _label(days),
            " — older chunks are dropped on the next policy run" if days < was else "")
    return True


def sync_retention(
    conn: psycopg.Connection, environ: Mapping[str, str] | None = None
) -> dict[str, bool]:
    """Apply CAIB_EVENTS_RETENTION_DAYS / CAIB_CAGG_RETENTION_DAYS. Validates
    everything before changing anything. Returns {relation: changed}."""
    env = os.environ if environ is None else environ
    events_days = parse_days(EVENTS_VAR, env)
    cagg_days = parse_days(CAGG_VAR, env)

    if not _exists(conn, EVENTS):
        if events_days is not None or cagg_days is not None:
            log.warning("%s does not exist; retention settings not applied", EVENTS)
        return {}

    _check_events_floor(events_days)
    events_span = (
        _span(events_days) if events_days is not None else _current_span(conn, EVENTS)
    )

    caggs = [c for c in CAGGS if _exists(conn, c)]
    cagg_targets: dict[str, int] = {}
    if cagg_days is not None:
        if _span(cagg_days) < events_span:
            raise RetentionConfigError(
                f"{CAGG_VAR}={cagg_days} is shorter than raw-event retention "
                f"({_label(events_span)}). Dashboards read the continuous aggregates "
                "for any range wider than ~7 days, so they would show nothing for "
                "periods whose raw events are still stored. Set it to at least "
                f"{EVENTS_VAR}, or 0 (never drop)."
            )
        cagg_targets = dict.fromkeys(caggs, cagg_days)
    elif events_days is not None:
        # Only when raw retention was set explicitly: with both knobs unset,
        # migrate must not touch a database whose events policy someone
        # raised or removed by hand.
        for cagg in caggs:
            if _current_span(conn, cagg) < events_span:
                cagg_targets[cagg] = 0 if events_span == math.inf else int(events_span)
                log.info("%s: raised to match raw-event retention (%s); set %s to override",
                         cagg, _label(events_span), CAGG_VAR)

    # One transaction: all of it or none of it. As caib_app, which owns the
    # migrations' policy jobs, so an added job gets the same owner.
    changed: dict[str, bool] = {}
    with conn.transaction():
        conn.execute("SET LOCAL ROLE caib_app")
        if events_days is not None:
            changed[EVENTS] = _apply(conn, EVENTS, events_days)
        for cagg, days in cagg_targets.items():
            changed[cagg] = _apply(conn, cagg, days)

    for rel in (EVENTS, *caggs):
        log.info("%s retention: %s", rel, _label(_current_span(conn, rel)))
    return changed
