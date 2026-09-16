"""The shared query core, and the role it is meant to be unable to exceed.

Two things are under test here and they are not the same thing. The first half
checks that `caib_core.query` filters, caps and clamps the way it says it does.
The second half checks that even if it did not, `caib_agent_ro` could not read
the raw ingest tables or write anything — because the caps are code and the
grants are the part nobody can edit out at runtime.
"""

import datetime

import psycopg
import pytest
from caib_core import query

UTC = datetime.UTC

# Exactly what migration 0008 grants, minus the _timescaledb_internal rows
# TimescaleDB adds itself when a continuous aggregate is granted (its own
# materialisation views, holding counts already derivable from caib.events).
EXPECTED_READABLE = {
    "caib.events",
    "caib.events_5m",
    "caib.actors_1h",
    "detect.alerts",
    "ops.heartbeat",
}

UNREADABLE = (
    "ingest.landing",
    "ingest.dead_letter",
    "ingest.cursors",
    "detect.outbox",
    "detect.rule_state",
)


@pytest.fixture()
def ro(seeded_db, agent_ro_url):
    """A connection as caib_agent_ro to the seeded database."""
    with psycopg.connect(agent_ro_url) as conn:
        yield conn


# --- search_events ------------------------------------------------------


def test_default_window_is_24h_and_excludes_older_events(ro):
    result = query.search_events(ro)
    # Seven of the eight seeded events are recent; carol's is 40 days back.
    assert result["count"] == 7
    assert "carol@example.com" not in {e["actor"] for e in result["events"]}
    assert result["window"]["clamped_to_max_lookback"] is False


def test_a_wider_window_reaches_the_older_event(ro):
    since = datetime.datetime.now(UTC) - datetime.timedelta(days=60)
    result = query.search_events(ro, since=since)
    assert result["count"] == 8
    assert "carol@example.com" in {e["actor"] for e in result["events"]}


def test_events_come_back_newest_first(ro):
    times = [e["event_time"] for e in query.search_events(ro)["events"]]
    assert times == sorted(times, reverse=True)


def test_free_text_matches_search_text(ro):
    assert query.search_events(ro, q="bar-staging")["count"] == 2
    assert query.search_events(ro, q="no-such-bucket")["count"] == 0


def test_equality_filters(ro):
    assert query.search_events(ro, module="gcp_audit")["count"] == 1
    assert query.search_events(ro, actor="alice@example.com")["count"] == 4
    assert query.search_events(ro, action="DeleteBucket")["count"] == 2
    assert query.search_events(ro, outcome="failure")["count"] == 3
    assert query.search_events(ro, severity="high")["count"] == 3


def test_filters_combine(ro):
    result = query.search_events(ro, module="aws_cloudtrail", outcome="failure")
    assert result["count"] == 3
    assert {e["module"] for e in result["events"]} == {"aws_cloudtrail"}


def test_raw_is_excluded_by_default_and_opt_in(ro):
    """`raw` is the unsanitised vendor event. Absent unless asked for — and the
    absence has to be the column not being selected, not a null."""
    default = query.search_events(ro, module="gcp_audit")
    assert default["include_raw"] is False
    assert "raw" not in default["events"][0]

    with_raw = query.search_events(ro, module="gcp_audit", include_raw=True)
    assert with_raw["include_raw"] is True
    assert with_raw["events"][0]["raw"]["protoPayload"]["secret"] == "unsanitised-vendor-blob"


def test_an_out_of_enum_filter_is_rejected_not_answered_empty(ro):
    """Zero rows reads as "nothing happened", which is the worst possible wrong
    answer about an audit log. A typo has to fail loudly."""
    for kwargs in ({"severity": "warn"}, {"outcome": "denied"}):
        with pytest.raises(query.QueryError):
            query.search_events(ro, **kwargs)


# --- the caps and the clamp ---------------------------------------------


def test_limit_cap_holds_when_the_caller_asks_for_more(ro):
    assert query.search_events(ro, limit=999_999)["limit"] == query.SEARCH_EVENTS_CAP
    assert query.get_alerts(ro, limit=999_999)["limit"] == query.ALERTS_CAP
    assert query.top_actors(ro, limit=999_999)["limit"] == query.TOP_ACTORS_CAP
    assert (
        query.actor_activity(ro, "alice@example.com", limit=999_999)["limit"]
        == query.ACTOR_ACTIVITY_CAP
    )


def test_a_smaller_limit_is_honoured_and_reported_as_truncated(ro):
    result = query.search_events(ro, limit=2)
    assert result["limit"] == 2
    assert result["count"] == 2
    assert result["truncated"] is True


def test_exactly_limit_rows_is_not_truncated(ro):
    """Seven events sit in the default window. A page of seven is the whole
    answer; only a page that leaves rows behind is truncated."""
    exact = query.search_events(ro, limit=7)
    assert (exact["count"], exact["truncated"]) == (7, False)
    assert query.search_events(ro, limit=6)["truncated"] is True

    alerts = query.get_alerts(ro, limit=3)
    assert (alerts["count"], alerts["truncated"]) == (3, False)
    assert query.get_alerts(ro, limit=2)["truncated"] is True

    actors = query.top_actors(ro)["count"]
    assert query.top_actors(ro, limit=actors)["truncated"] is False
    assert query.actor_activity(ro, "alice@example.com", limit=4)["truncated"] is False


def test_a_nonsense_limit_is_rejected(ro):
    for bad in (0, -1, "twelve"):
        with pytest.raises(query.QueryError):
            query.search_events(ro, limit=bad)


def test_lookback_is_clamped_to_the_retention_window(ro):
    """Asking past the 90-day retention policy cannot return more data, and the
    caller is told the window moved rather than left to assume it did not."""
    now = datetime.datetime.now(UTC)
    result = query.search_events(ro, since=now - datetime.timedelta(days=400))
    assert result["window"]["clamped_to_max_lookback"] is True
    floor = datetime.datetime.fromisoformat(result["window"]["since"])
    assert abs((now - floor) - query.MAX_LOOKBACK) < datetime.timedelta(minutes=1)


def test_timestamps_parse_from_iso_strings_and_naive_datetimes(ro):
    now = datetime.datetime.now(UTC)
    since = now - datetime.timedelta(hours=1)
    from_string = query.search_events(ro, since=since.isoformat())["count"]
    from_naive = query.search_events(ro, since=since.replace(tzinfo=None))["count"]
    assert from_string == from_naive == query.search_events(ro, since=since)["count"]


def test_a_backwards_window_and_a_bad_timestamp_are_rejected(ro):
    now = datetime.datetime.now(UTC)
    with pytest.raises(query.QueryError):
        query.search_events(ro, since=now, until=now - datetime.timedelta(hours=1))
    with pytest.raises(query.QueryError):
        query.search_events(ro, since="last tuesday")


# --- the other six ------------------------------------------------------


def test_get_alerts_filters_and_orders(ro):
    assert query.get_alerts(ro)["count"] == 3
    assert query.get_alerts(ro, status="open")["count"] == 1
    assert query.get_alerts(ro, severity="critical")["count"] == 1
    assert query.get_alerts(ro, rule_id="gcp_audit.iam_policy_changed")["count"] == 1
    seen = [a["last_seen"] for a in query.get_alerts(ro)["alerts"]]
    assert seen == sorted(seen, reverse=True)
    with pytest.raises(query.QueryError):
        query.get_alerts(ro, status="acknowledged")


def _add_alert(db_url: str, dedup_key: str, status: str, last_seen_sql: str) -> None:
    with psycopg.connect(db_url) as conn:
        conn.execute(
            "INSERT INTO detect.alerts (rule_id, dedup_key, severity, title, first_seen,"
            f" last_seen, status) VALUES ('aws_cloudtrail.root_console_login', %s, 'high',"
            f" 'Root account console login', {last_seen_sql}, {last_seen_sql}, %s)",
            (dedup_key, status),
        )


@pytest.fixture()
def stale_alerts(seeded_db):
    """Alerts whose rules stopped matching long ago, and one from the future."""
    _add_alert(seeded_db, "open-10d", "open", "now() - interval '10 days'")
    _add_alert(seeded_db, "ack-120d", "ack", "now() - interval '120 days'")
    _add_alert(seeded_db, "closed-10d", "closed", "now() - interval '10 days'")
    # Vendor event times drift; last_seen can sit slightly ahead of now().
    _add_alert(seeded_db, "open-future", "open", "now() + interval '2 minutes'")
    return seeded_db


def test_open_and_acked_alerts_are_never_aged_out_of_the_default_window(stale_alerts, ro):
    """"What is open?" must not be answered with "nothing" because the rule
    last matched more than a week — or more than the 90-day event retention —
    ago. Retention never deletes open or acked alerts; neither may the view."""
    open_keys = {a["dedup_key"] for a in query.get_alerts(ro, status="open")["alerts"]}
    assert open_keys == {"k1", "open-10d", "open-future"}
    assert [a["dedup_key"] for a in query.get_alerts(ro, status="ack")["alerts"]] == [
        "k3", "ack-120d"]
    # Closed alerts still follow the 7-day default window.
    closed = {a["dedup_key"] for a in query.get_alerts(ro, status="closed")["alerts"]}
    assert closed == {"k2"}

    result = query.get_alerts(ro)
    assert result["window"]["applies_to"] == "closed alerts only"
    assert result["window"]["clamped_to_max_lookback"] is False

    summary = query.alert_summary(ro)
    assert summary["by_status"] == {"open": 3, "ack": 2, "closed": 1}
    assert summary["total"] == 6


def test_an_explicit_since_applies_to_every_status_and_is_not_clamped(stale_alerts, ro):
    now = datetime.datetime.now(UTC)
    recent = query.get_alerts(ro, since=now - datetime.timedelta(days=1))
    assert {a["dedup_key"] for a in recent["alerts"]} == {"k1", "k2", "k3", "open-future"}
    assert recent["window"]["applies_to"] == "all alerts"

    # Alerts are not caib.events: the 90-day retention ceiling does not apply.
    year = query.get_alerts(ro, since=now - datetime.timedelta(days=365))
    assert year["window"]["clamped_to_max_lookback"] is False
    assert "ack-120d" in {a["dedup_key"] for a in year["alerts"]}
    assert query.alert_summary(ro, since=now - datetime.timedelta(days=365))["total"] == 7


def test_alert_summary_breakdowns_agree_with_the_total(ro):
    summary = query.alert_summary(ro)
    assert summary["total"] == 3
    assert summary["by_status"] == {"open": 1, "ack": 1, "closed": 1}
    assert summary["by_severity"]["critical"] == 1
    assert summary["by_severity"]["info"] == 0, "severities must be zero-filled"
    assert sum(summary["by_status"].values()) == summary["total"]
    assert sum(summary["by_severity"].values()) == summary["total"]


def test_top_actors_counts_events_and_failures(ro):
    result = query.top_actors(ro)
    assert result["source"] == "caib.events", "an unaligned window must not use the cagg"
    by_actor = {a["actor"]: a for a in result["actors"]}
    assert by_actor["alice@example.com"]["events"] == 4
    assert by_actor["alice@example.com"]["failures"] == 1
    assert by_actor["bob@example.com"]["failures"] == 2
    # Busiest first.
    assert result["actors"][0]["events"] >= result["actors"][-1]["events"]


def test_top_actors_uses_the_continuous_aggregate_on_an_hour_boundary(ro):
    """The cagg is only correct when every bucket lies wholly inside the window,
    so the source has to switch on alignment — and say which one it used."""
    aligned = datetime.datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    aligned -= datetime.timedelta(hours=6)
    assert query.top_actors(ro, since=aligned)["source"] == "caib.actors_1h"


def test_top_actors_module_filter(ro):
    result = query.top_actors(ro, module="gcp_audit")
    assert {a["module"] for a in result["actors"]} == {"gcp_audit"}


def test_actor_activity_rollup_covers_the_window_not_the_page(ro):
    """The event list truncates; the counts must not, or the rollup would say
    something different depending on how many rows were asked for."""
    result = query.actor_activity(ro, "alice@example.com", limit=1)
    assert result["count"] == 1
    assert result["truncated"] is True
    rollup = {r["action"]: r for r in result["by_action"]}
    assert rollup["GetObject"]["events"] == 4
    assert rollup["GetObject"]["failures"] == 1
    assert "raw" not in result["events"][0]


def test_actor_activity_requires_an_actor(ro):
    with pytest.raises(query.QueryError):
        query.actor_activity(ro, "")


def test_module_health_reports_lag_volume_and_heartbeats(ro):
    health = query.module_health(ro)
    modules = {m["module"]: m for m in health["modules"]}
    assert set(modules) == {"aws_cloudtrail", "gcp_audit"}
    assert modules["gcp_audit"]["events_1h"] == 1
    # Five of the six aws events are inside the hour; carol's is 40 days old.
    assert modules["aws_cloudtrail"]["events_24h"] == 6
    assert modules["aws_cloudtrail"]["lag_seconds"] >= 0
    assert {s["service"] for s in health["services"]} == {"loader", "detect"}
    assert all(s["age_seconds"] < 300 for s in health["services"])


def test_list_rules_reads_the_engines_own_rule_set(tmp_path):
    from caib_core import detect

    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "one.yaml").write_text(
        "id: test.one\nmodule: aws_cloudtrail\ntitle: One\nseverity: high\n"
        "interval: 60\nenabled: false\nsql: SELECT 1\n"
    )
    listed = query.list_rules(rules_dir)
    assert listed["count"] == 1
    assert listed["enabled_count"] == 0
    rule = listed["rules"][0]
    assert rule["id"] == "test.one"
    assert rule["severity"] == "high"
    assert rule["enabled"] is False
    assert "sql" not in rule, "rule SQL is detection logic, not an agent's business"
    # Same source of truth as the engine, so the two cannot describe different
    # rule sets.
    assert [r.id for r in detect.load_rules(rules_dir)] == [r["id"] for r in listed["rules"]]

    with pytest.raises(query.QueryError):
        query.list_rules(tmp_path / "nope")


def test_the_shipped_rule_set_lists(ro):
    listed = query.list_rules("detect/rules")
    assert listed["count"] > 0
    assert all({"id", "title", "severity", "enabled"} <= set(r) for r in listed["rules"])


# --- the role, which is the part that actually enforces read-only -------


def test_caib_agent_ro_reads_exactly_the_five_granted_tables(ro):
    granted = {
        f"{schema}.{table}"
        for schema, table in ro.execute(
            "SELECT table_schema, table_name FROM information_schema.table_privileges"
            " WHERE grantee = 'caib_agent_ro' AND privilege_type = 'SELECT'"
            "   AND table_schema IN ('caib', 'ingest', 'detect', 'ops')"
        ).fetchall()
    }
    assert granted == EXPECTED_READABLE


def test_caib_agent_ro_holds_no_privilege_other_than_select(ro):
    privileges = {
        row[0]
        for row in ro.execute(
            "SELECT DISTINCT privilege_type FROM information_schema.table_privileges"
            " WHERE grantee = 'caib_agent_ro'"
        ).fetchall()
    }
    assert privileges == {"SELECT"}


@pytest.mark.parametrize("table", UNREADABLE)
def test_caib_agent_ro_cannot_read_the_raw_or_internal_tables(ro, table):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        ro.execute(f"SELECT count(*) FROM {table}")


def _go_read_write(conn: psycopg.Connection) -> None:
    """Undo the role's read-only session default, as a hostile client would.

    The commit is required, not incidental: default_transaction_read_only is
    read when a transaction begins, and psycopg has already opened one by the
    time the SET runs. Without it the writes below would fail with
    ReadOnlySqlTransaction — which proves nothing about the grants, since any
    client can flip that GUC.
    """
    conn.execute("SET default_transaction_read_only = off")
    conn.commit()
    assert conn.execute("SHOW default_transaction_read_only").fetchone()[0] == "off"


def test_caib_agent_ro_sessions_are_read_only_by_default(ro):
    assert ro.execute("SHOW default_transaction_read_only").fetchone()[0] == "on"
    assert ro.execute("SHOW statement_timeout").fetchone()[0] == "15s"


@pytest.mark.parametrize(
    "statement",
    (
        (
            "INSERT INTO caib.events (event_time, ingested_at, module, dedup_hash, action)"
            " VALUES (now(), now(), 'x', '\\x00'::bytea, 'x')"
        ),
        "UPDATE detect.alerts SET status = 'closed'",
        "DELETE FROM detect.alerts",
        "UPDATE ops.heartbeat SET beat_at = now()",
        (
            "INSERT INTO detect.alerts (rule_id, dedup_key, severity, title, first_seen,"
            " last_seen) VALUES ('r', 'k', 'info', 't', now(), now())"
        ),
    ),
)
def test_caib_agent_ro_cannot_write_even_outside_a_read_only_transaction(ro, statement):
    """default_transaction_read_only is a session GUC and any client can turn it
    off, so proving "read-only" against it alone proves nothing. Turn it off
    first: the write must still fail, and fail on the missing grant."""
    _go_read_write(ro)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        ro.execute(statement)


def test_caib_agent_ro_cannot_create_objects(ro):
    _go_read_write(ro)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        ro.execute("CREATE TABLE caib.smuggled (x int)")
