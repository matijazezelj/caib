"""Read-only query core for the agent query layer.

Pure functions over a psycopg connection. No HTTP, no MCP, no framework types:
`caib_agentapi.app` and `caib_agentapi.mcp_server` are two transports over
exactly these seven functions, so the two surfaces cannot drift apart in what
they will answer or in what they refuse.

Every guardrail lives here rather than in a transport, for the same reason:

  * a hard per-function LIMIT cap, applied to whatever the caller asked for;
  * `since`/`until` coerced to aware UTC datetimes;
  * a maximum lookback on event queries equal to the live `caib.events`
    retention policy (90 days unless CAIB_EVENTS_RETENTION_DAYS says otherwise;
    none when there is no policy) — older raw events do not exist, so a bigger
    window only costs chunk scans. Read per request, see retention_lookback();
  * no caller string is ever interpolated into SQL text. Every value goes
    through a psycopg named parameter. The only things composed into a query
    string are fragments from module-level constants in this file, which is why
    the filters are built as a list of fixed strings rather than as f-strings
    over caller input.

Read-only is not enforced here, and deliberately so: it is enforced by the
`caib_agent_ro` role (migration 0008 grants SELECT on five tables and nothing
else; `ALTER ROLE ... SET default_transaction_read_only = on`). This module
being SELECT-only is a property anyone can read; the role is the property
nobody can edit out.

Remember when adding SQL: literal `%` must be written `%%`, because these
statements all carry named parameters (see CLAUDE.md).
"""

import datetime
import os
import pathlib
from typing import Any

import psycopg
from psycopg.rows import dict_row

# The event-query lookback ceiling is the caib.events retention policy as it is
# in the database right now: 90 days from migration 0002 unless the operator
# changed it (CAIB_EVENTS_RETENTION_DAYS, caib_core.events_retention). Asking
# for more than that cannot return more data; it only buys a scan across every
# chunk that still exists. No policy (retention 0, never drop) means no
# ceiling: every stored event is reachable, and what bounds a huge scan is the
# role's statement_timeout (15s, caib_core.migrate.AGENT_ROLE_SETTINGS) and the
# LIMIT caps below — there is deliberately no separate invented cap.
#
# Read per request rather than cached: it is one catalog row, and a cache would
# keep clamping to the old value after a migrate changed it. caib_agent_ro reads
# timescaledb_information.jobs through TimescaleDB's own PUBLIC grant; migration
# 0008 grants nothing for it.
RETENTION_SQL = """
SELECT extract(epoch FROM (config->>'drop_after')::interval)
FROM timescaledb_information.jobs
WHERE proc_name = 'policy_retention'
  AND hypertable_schema = 'caib' AND hypertable_name = 'events'
"""


def retention_lookback(conn: psycopg.Connection) -> datetime.timedelta | None:
    """caib.events' retention as a timedelta, or None when no policy drops it."""
    row = conn.execute(RETENTION_SQL).fetchone()
    return None if row is None else datetime.timedelta(seconds=float(row[0]))


DEFAULT_EVENT_WINDOW = datetime.timedelta(hours=24)
DEFAULT_ALERT_WINDOW = datetime.timedelta(days=7)

# Hard caps. A caller asking for more gets this many; there is no override.
SEARCH_EVENTS_CAP = 500
ALERTS_CAP = 500
TOP_ACTORS_CAP = 100
ACTOR_ACTIVITY_CAP = 500
# The per-action rollup in actor_activity() is a summary, not a listing.
ACTOR_ROLLUP_CAP = 50

OUTCOMES = ("success", "failure", "unknown")
SEVERITIES = ("info", "low", "medium", "high", "critical")
ALERT_STATUSES = ("open", "ack", "closed")

# The Search dashboard's column list, minus `raw`. Kept identical on purpose:
# "what search_events returns" and "what the Search dashboard shows" should not
# need two answers. src_ip goes through host() because inet renders as a CIDR.
EVENT_COLUMNS = (
    "event_time, module, actor, action, target, outcome, severity, "
    "host(src_ip) AS src_ip, attrs"
)
# `raw` is the whole vendor event: the biggest column and the one most likely to
# hold something nobody sanitised. Opt-in per call, never a default.
EVENT_COLUMNS_WITH_RAW = EVENT_COLUMNS + ", raw"


class QueryError(ValueError):
    """Bad input from the caller. Transports turn this into a 4xx / a tool
    error, never into a 500 — it means the request was wrong, not the server."""


def _aware(value: Any, field: str) -> datetime.datetime | None:
    """datetime | ISO-8601 str | None -> aware UTC datetime | None.

    Naive input is read as UTC rather than rejected: the whole stack stores and
    displays UTC (compose sets `-c timezone=UTC`), so a naive timestamp from an
    agent means UTC in every case that matters.
    """
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.datetime.fromisoformat(value)
        except ValueError as exc:
            raise QueryError(f"{field}: not an ISO-8601 timestamp: {value!r}") from exc
    if not isinstance(value, datetime.datetime):
        raise QueryError(f"{field}: expected a timestamp, got {type(value).__name__}")
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.UTC)
    return value.astimezone(datetime.UTC)


def _window(
    since: Any,
    until: Any,
    default_span: datetime.timedelta,
    max_lookback: datetime.timedelta | None,
) -> tuple[datetime.datetime, datetime.datetime, bool]:
    """Resolve (since, until) and clamp to `max_lookback` (None: no clamp).

    Returns the effective window plus whether it was clamped. Callers put both
    in the response: an agent that asked for a year and silently got 90 days
    would otherwise draw conclusions from a window it does not know about.
    """
    now = datetime.datetime.now(datetime.UTC)
    until_dt = _aware(until, "until") or now
    since_dt = _aware(since, "since") or (until_dt - default_span)
    if since_dt >= until_dt:
        raise QueryError(
            f"since ({since_dt.isoformat()}) must be before until ({until_dt.isoformat()})"
        )
    if max_lookback is None:
        return since_dt, until_dt, False
    floor = now - max_lookback
    if since_dt < floor:
        return floor, until_dt, True
    return since_dt, until_dt, False


def _cap(limit: Any, cap: int, default: int) -> int:
    if limit is None:
        return default
    try:
        limit = int(limit)
    except (TypeError, ValueError) as exc:
        raise QueryError(f"limit: expected an integer, got {limit!r}") from exc
    if limit < 1:
        raise QueryError(f"limit: must be at least 1, got {limit}")
    return min(limit, cap)


def _one_of(value: Any, allowed: tuple[str, ...], field: str) -> str | None:
    """Reject an out-of-enum filter rather than answering it with zero rows —
    `severity="warn"` is a typo, and an empty result set reads as "nothing
    happened", which is the worst possible answer to give about an audit log."""
    if value is None:
        return None
    if value not in allowed:
        raise QueryError(f"{field}: must be one of {'|'.join(allowed)}, got {value!r}")
    return value


def _jsonable(value: Any) -> Any:
    """datetime -> ISO-8601. Everything else the queries below select is already
    a JSON-native type: jsonb comes back as dict, inet is cast with host(), and
    the counts are cast to bigint/int in SQL so none of them arrive as Decimal.
    """
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    return value


def _rows(cur) -> list[dict]:
    return [{k: _jsonable(v) for k, v in row.items()} for row in cur.fetchall()]


def _fetch(conn: psycopg.Connection, sql: str, params: dict) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return _rows(cur)


def _fetch_page(
    conn: psycopg.Connection, sql: str, params: dict, limit: int
) -> tuple[list[dict], bool]:
    """Run a `LIMIT %(limit)s` query for one row more than the page, so
    `truncated` means "more rows exist" rather than "the page happened to be
    full" — exactly `limit` matching rows is not a truncated answer."""
    rows = _fetch(conn, sql, {**params, "limit": limit + 1})
    return rows[:limit], len(rows) > limit


def _window_meta(
    since: datetime.datetime,
    until: datetime.datetime,
    clamped: bool,
    max_lookback: datetime.timedelta | None,
) -> dict:
    return {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "clamped_to_max_lookback": clamped,
        "max_lookback_days": max_lookback.days if max_lookback else None,
    }


def search_events(
    conn: psycopg.Connection,
    q: str | None = None,
    module: str | None = None,
    actor: str | None = None,
    action: str | None = None,
    outcome: str | None = None,
    severity: str | None = None,
    since: Any = None,
    until: Any = None,
    limit: int | None = 100,
    include_raw: bool = False,
) -> dict:
    """Free-text + field search over caib.events, newest first.

    Mirrors the Search dashboard: `q` is a substring match against the
    loader-built `search_text` (lowercased actor, action, target, src_ip,
    outcome and attribute values), and everything else is an equality filter.
    Note that `%` and `_` inside `q` are LIKE wildcards, exactly as they are in
    the dashboard's textbox.

    Window defaults to the last 24 hours. Cap 500 rows.
    """
    lookback = retention_lookback(conn)
    since_dt, until_dt, clamped = _window(since, until, DEFAULT_EVENT_WINDOW, lookback)
    limit = _cap(limit, SEARCH_EVENTS_CAP, 100)

    # Fragments are literals from this file; every caller value is a parameter.
    where = ["event_time >= %(since)s", "event_time < %(until)s"]
    params: dict[str, Any] = {"since": since_dt, "until": until_dt}
    if q:
        where.append("search_text ILIKE '%%' || %(q)s || '%%'")
        params["q"] = q
    for field, value in (
        ("module", module),
        ("actor", actor),
        ("action", action),
        ("outcome", _one_of(outcome, OUTCOMES, "outcome")),
        ("severity", _one_of(severity, SEVERITIES, "severity")),
    ):
        if value is not None:
            where.append(f"{field} = %({field})s")
            params[field] = value

    columns = EVENT_COLUMNS_WITH_RAW if include_raw else EVENT_COLUMNS
    events, truncated = _fetch_page(
        conn,
        f"SELECT {columns} FROM caib.events"
        f" WHERE {' AND '.join(where)}"
        " ORDER BY event_time DESC LIMIT %(limit)s",
        params,
        limit,
    )
    return {
        "window": _window_meta(since_dt, until_dt, clamped, lookback),
        "limit": limit,
        "count": len(events),
        "truncated": truncated,
        "include_raw": include_raw,
        "events": events,
    }


ALERT_COLUMNS = (
    "id, rule_id, dedup_key, severity, title, entity, context, "
    "first_seen, last_seen, event_count, status"
)


# Unresolved alerts are exempt from the default window. An open alert whose
# rule stopped matching two weeks ago is still an open case — it is exactly
# what "what is open right now?" is asking about — and detect.alerts keeps it
# forever (caib_core.retention only ever deletes closed ones). Filtering it out
# by last_seen answered that question with "nothing". An explicit `since` is
# the caller asking about recency, so it applies to every status.
#
# No lookback clamp and no upper bound either: the event ceiling is the
# caib.events retention policy, which alerts do not follow, and last_seen is a
# vendor event time that can sit slightly in the future.
ALERT_DEFAULT_WINDOW_FILTER = "(status <> 'closed' OR last_seen >= %(since)s)"
ALERT_EXPLICIT_WINDOW_FILTER = "last_seen >= %(since)s"


def _alert_window(since: Any) -> tuple[dict, str, datetime.datetime]:
    since_dt, until_dt, _ = _window(since, None, DEFAULT_ALERT_WINDOW, max_lookback=None)
    explicit = since is not None
    meta = _window_meta(since_dt, until_dt, False, max_lookback=None)
    meta["applies_to"] = "all alerts" if explicit else "closed alerts only"
    return (
        meta,
        ALERT_EXPLICIT_WINDOW_FILTER if explicit else ALERT_DEFAULT_WINDOW_FILTER,
        since_dt,
    )


def get_alerts(
    conn: psycopg.Connection,
    status: str | None = None,
    severity: str | None = None,
    rule_id: str | None = None,
    since: Any = None,
    limit: int | None = 100,
) -> dict:
    """Alerts from detect.alerts, most recently seen first.

    Without `since`: every open and acked alert, however old, plus closed
    alerts last seen in the past 7 days. With `since`: every alert last seen
    at or after it, whatever its status. Cap 500 rows.
    """
    meta, window_filter, since_dt = _alert_window(since)
    limit = _cap(limit, ALERTS_CAP, 100)

    where = [window_filter]
    params: dict[str, Any] = {"since": since_dt}
    for field, value in (
        ("status", _one_of(status, ALERT_STATUSES, "status")),
        ("severity", _one_of(severity, SEVERITIES, "severity")),
        ("rule_id", rule_id),
    ):
        if value is not None:
            where.append(f"{field} = %({field})s")
            params[field] = value

    alerts, truncated = _fetch_page(
        conn,
        f"SELECT {ALERT_COLUMNS} FROM detect.alerts"
        f" WHERE {' AND '.join(where)}"
        " ORDER BY last_seen DESC, id DESC LIMIT %(limit)s",
        params,
        limit,
    )
    return {
        "window": meta,
        "limit": limit,
        "count": len(alerts),
        "truncated": truncated,
        "alerts": alerts,
    }


def alert_summary(conn: psycopg.Connection, since: Any = None) -> dict:
    """Alert counts, grouped by status and by severity.

    Same window rule as get_alerts(): without `since`, every open and acked
    alert plus closed ones last seen in the past 7 days, so `by_status.open`
    is the real open count. One GROUP BY over (status, severity) folded in
    Python, so the two breakdowns are guaranteed to sum to the same total.
    """
    meta, window_filter, since_dt = _alert_window(since)
    rows = _fetch(
        conn,
        "SELECT status, severity, count(*)::bigint AS n FROM detect.alerts"
        f" WHERE {window_filter}"
        " GROUP BY status, severity",
        {"since": since_dt},
    )
    # Zero-filled so a caller can tell "no critical alerts" from "the key for
    # critical is missing because I forgot to ask for it".
    by_status = dict.fromkeys(ALERT_STATUSES, 0)
    by_severity = dict.fromkeys(SEVERITIES, 0)
    total = 0
    for row in rows:
        n = row["n"]
        total += n
        by_status[row["status"]] = by_status.get(row["status"], 0) + n
        by_severity[row["severity"]] = by_severity.get(row["severity"], 0) + n
    return {
        "window": meta,
        "total": total,
        "by_status": by_status,
        "by_severity": by_severity,
    }


# The Overview dashboard's "Top actors (24h)" panel reads caib.actors_1h, and so
# does this — but only when it can do so without lying. actors_1h buckets by the
# hour, so a window starting at 09:30 would either count the whole 09:00 bucket
# (over-reporting the half hour before the caller asked about) or drop it
# (under-reporting). An hour-aligned `since` has neither problem: every bucket
# lies wholly inside the window. Anything else falls back to counting
# caib.events directly, which is exact at any boundary and costs a rowstore scan
# the trgm/module indexes can help with. Callers are told which one ran.
TOP_ACTORS_CAGG_SQL = """
SELECT actor, module, sum(n)::bigint AS events, sum(failures)::bigint AS failures
FROM caib.actors_1h
WHERE bucket >= %(since)s AND bucket < %(until)s AND actor IS NOT NULL
  {module_filter}
GROUP BY actor, module
ORDER BY events DESC, actor
LIMIT %(limit)s
"""

TOP_ACTORS_EVENTS_SQL = """
SELECT actor, module, count(*)::bigint AS events,
       count(*) FILTER (WHERE outcome = 'failure')::bigint AS failures
FROM caib.events
WHERE event_time >= %(since)s AND event_time < %(until)s AND actor IS NOT NULL
  {module_filter}
GROUP BY actor, module
ORDER BY events DESC, actor
LIMIT %(limit)s
"""


def top_actors(
    conn: psycopg.Connection,
    since: Any = None,
    module: str | None = None,
    limit: int | None = 20,
) -> dict:
    """Busiest actors in a window, with their failure counts.

    Window defaults to the last 24 hours and runs to now. Reads the
    caib.actors_1h continuous aggregate when `since` falls on an hour boundary
    and caib.events otherwise; the answer says which, in `source`. Cap 100.
    """
    lookback = retention_lookback(conn)
    since_dt, until_dt, clamped = _window(since, None, DEFAULT_EVENT_WINDOW, lookback)
    limit = _cap(limit, TOP_ACTORS_CAP, 20)

    aligned = (since_dt.minute, since_dt.second, since_dt.microsecond) == (0, 0, 0)
    template = TOP_ACTORS_CAGG_SQL if aligned else TOP_ACTORS_EVENTS_SQL
    params: dict[str, Any] = {"since": since_dt, "until": until_dt}
    module_filter = ""
    if module is not None:
        module_filter = "AND module = %(module)s"
        params["module"] = module

    actors, truncated = _fetch_page(
        conn, template.format(module_filter=module_filter), params, limit
    )
    return {
        "window": _window_meta(since_dt, until_dt, clamped, lookback),
        "source": "caib.actors_1h" if aligned else "caib.events",
        "limit": limit,
        "count": len(actors),
        "truncated": truncated,
        "actors": actors,
    }


ACTOR_ROLLUP_SQL = """
SELECT action,
       count(*)::bigint AS events,
       count(*) FILTER (WHERE outcome = 'failure')::bigint AS failures,
       min(event_time) AS first_event_time,
       max(event_time) AS last_event_time
FROM caib.events
WHERE actor = %(actor)s AND event_time >= %(since)s AND event_time < %(until)s
GROUP BY action
ORDER BY events DESC, action
LIMIT %(rollup_limit)s
"""


def actor_activity(
    conn: psycopg.Connection,
    actor: str,
    since: Any = None,
    until: Any = None,
    limit: int | None = 200,
) -> dict:
    """One actor's timeline: their events newest-first, plus a per-action rollup.

    The rollup is computed over the whole window, not over the returned page, so
    it stays true when the event list is truncated — the counts are the point of
    asking. `raw` is never included here; use search_events(include_raw=True)
    for a specific event. Window defaults to the last 24 hours. Cap 500 events
    and 50 distinct actions.
    """
    if not actor:
        raise QueryError("actor: required")
    lookback = retention_lookback(conn)
    since_dt, until_dt, clamped = _window(since, until, DEFAULT_EVENT_WINDOW, lookback)
    limit = _cap(limit, ACTOR_ACTIVITY_CAP, 200)
    params = {
        "actor": actor,
        "since": since_dt,
        "until": until_dt,
        "rollup_limit": ACTOR_ROLLUP_CAP,
    }

    events, truncated = _fetch_page(
        conn,
        f"SELECT {EVENT_COLUMNS} FROM caib.events"
        " WHERE actor = %(actor)s"
        "   AND event_time >= %(since)s AND event_time < %(until)s"
        " ORDER BY event_time DESC LIMIT %(limit)s",
        params,
        limit,
    )
    by_action = _fetch(conn, ACTOR_ROLLUP_SQL, params)
    return {
        "actor": actor,
        "window": _window_meta(since_dt, until_dt, clamped, lookback),
        "limit": limit,
        "count": len(events),
        "truncated": truncated,
        "events": events,
        "by_action": by_action,
    }


# The Overview dashboard's "Ingest lag by module" panel, plus the 1h/24h counts,
# in one statement. Two things differ from the panel, both on purpose:
#
#   * the module list is the union of the 30-day cagg and the 2-day rowstore.
#     The panel takes it from caib.events_5m alone, which is empty until the
#     aggregate first materialises — a freshly started stack would report no
#     modules at all while happily ingesting.
#   * `newest` comes from the 2-day rowstore when there is a recent event and
#     falls back to the cagg bucket otherwise, exactly as the panel does, so a
#     module that has gone quiet shows a growing lag instead of vanishing.
#
# Every branch is time-bounded; nothing here walks the whole retention window.
MODULE_HEALTH_SQL = """
WITH seen AS (
  SELECT module, max(bucket) AS newest
  FROM caib.events_5m WHERE bucket > now() - interval '30 days' GROUP BY module
), hot AS (
  SELECT module, max(event_time) AS newest,
         count(*) FILTER (WHERE event_time > now() - interval '1 hour')::bigint AS n_1h,
         count(*) FILTER (WHERE event_time > now() - interval '24 hours')::bigint AS n_24h
  FROM caib.events WHERE event_time > now() - interval '2 days' GROUP BY module
), modules AS (
  SELECT module FROM seen UNION SELECT module FROM hot
)
SELECT m.module,
       coalesce(h.newest, s.newest) AS last_event_time,
       extract(epoch FROM now() - coalesce(h.newest, s.newest))::int AS lag_seconds,
       coalesce(h.n_1h, 0) AS events_1h,
       coalesce(h.n_24h, 0) AS events_24h
FROM modules m
LEFT JOIN seen s ON s.module = m.module
LEFT JOIN hot  h ON h.module = m.module
ORDER BY lag_seconds DESC NULLS LAST, m.module
"""

HEARTBEAT_SQL = """
SELECT service, beat_at,
       extract(epoch FROM now() - beat_at)::int AS age_seconds
FROM ops.heartbeat ORDER BY service
"""


def module_health(conn: psycopg.Connection) -> dict:
    """The Overview dashboard's ops row as JSON: per-module ingest lag and event
    counts, plus the engine heartbeats.

    Two lists, because there are two different things and the audit log does not
    benefit from pretending otherwise: `modules` is per data source, `services`
    is per engine loop (loader, detect). ops.heartbeat is keyed by service, so
    there is no per-module heartbeat to report — a module going quiet shows up
    as `lag_seconds` growing in `modules`, which is what the dead-man rules
    watch too.

    Deliberately absent, because caib_agent_ro cannot read them: the landing
    backlog and dead-letter counts from the same dashboard row (ingest.*), and
    the Timescale job-error count (timescaledb_information). Those are operator
    questions, and the operator has Grafana.
    """
    modules = _fetch(conn, MODULE_HEALTH_SQL, {})
    services = _fetch(conn, HEARTBEAT_SQL, {})
    return {
        "generated_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "modules": modules,
        "services": services,
    }


def list_rules(source: pathlib.Path | str | None = None) -> dict:
    """The loaded detection rule set: id, module, title, severity, enabled.

    Takes a rules directory, not a connection — unlike the other six. The rule
    set lives in YAML files, and this reads them through
    `caib_core.detect.load_rules`, the same call the engine makes, so this can
    never describe a rule set the engine is not running. (detect.rule_state
    holds the per-rule watermarks, but caib_agent_ro is not granted it, and a
    watermark is engine internals rather than something to reason about.)

    The rule SQL is not returned: it is the detection logic, it is long, and
    nothing an analyst agent does with an alert needs it.

    `source` defaults to $CAIB_RULES_DIR, then to detect/rules — the same
    resolution `caib_core.detect.main` uses.
    """
    from caib_core import detect

    rules_dir = pathlib.Path(
        source if source is not None else os.environ.get("CAIB_RULES_DIR", "detect/rules")
    )
    if not rules_dir.is_dir():
        raise QueryError(f"rules directory not found: {rules_dir}")
    rules = detect.load_rules(rules_dir)
    return {
        "source": str(rules_dir),
        "count": len(rules),
        "enabled_count": sum(1 for r in rules if r.enabled),
        "rules": [
            {
                "id": r.id,
                "module": r.module,
                "title": r.title,
                "severity": r.severity,
                "enabled": r.enabled,
                "interval_s": r.interval_s,
                "lookback": r.lookback,
            }
            for r in sorted(rules, key=lambda r: r.id)
        ],
    }
