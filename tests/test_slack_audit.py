"""slack_audit: envelope mapping, window/cursor paging, end-to-end dedup, and
every seed rule firing on its fixture and staying quiet on benign ones.
Fixtures are SYNTHETIC (built from Slack's docs; see API_NOTES.md)."""

import datetime
import importlib.util
import json
import logging
import pathlib

import httpx
import psycopg
import pytest
from caib_core import detect, loader
from caib_pollers import framework
from caib_pollers.modules.slack_audit import POLLER, WINDOW_LAG_S, envelope
from psycopg.types.json import Jsonb

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "modules" / "slack_audit" / "fixtures"
RULES = ROOT / "detect" / "rules" / "slack_audit"
PAGE1 = json.loads((FIXTURES / "logs_page1.json").read_text())
PAGE2 = json.loads((FIXTURES / "logs_page2.json").read_text())
ENTRIES = {e["action"]: e for e in PAGE1["entries"] + PAGE2["entries"]}
CFG = detect.Cfg(safety_lag_s=0)
UTC = datetime.UTC


@pytest.fixture(autouse=True)
def slack_env(monkeypatch):
    monkeypatch.setenv("CAIB_SLACK_AUDIT_TOKEN", "xoxp-test-token")


def test_fixtures_are_marked_synthetic():
    assert "SYNTHETIC" in PAGE1["_comment"] and "SYNTHETIC" in PAGE2["_comment"]


# --- envelope ---------------------------------------------------------------


def test_envelope_mapping_login():
    entry = ENTRIES["user_login"]
    env = envelope(entry)
    assert env["event_time"] == "2026-08-23T09:15:04Z"  # unix -> RFC3339 UTC
    assert env["event_id"] == "0b1e8a52-6c7d-4900-a12f-3456789ab001"
    assert env["actor"] == "alice@example.com"
    assert env["src_ip"] == "203.0.113.44"
    assert env["action"] == "user_login"
    assert env["target"] == "W0ALICE001"
    assert env["outcome"] == "success"
    assert env["severity"] == "info"
    attrs = env["attrs"]
    assert attrs["actor_id"] == "W0ALICE001"
    assert attrs["actor_team"] == "T0EXAMPLE1"
    assert attrs["entity_type"] == "user"
    assert attrs["location_type"] == "enterprise"
    assert attrs["location_name"] == "Example Corp"
    assert attrs["session_id"] == "847288190092"
    assert "anomaly_reasons" not in attrs and "app_id" not in attrs
    assert env["raw"] == entry


def test_envelope_mapping_app_and_channel_entities():
    app = envelope(ENTRIES["app_installed"])
    assert app["target"] == "A0SHADYAPP"
    assert app["attrs"]["entity_type"] == "app"
    assert app["attrs"]["entity_name"] == "Export Helper"
    assert app["attrs"]["app_id"] == "A0SHADYAPP"  # context.app
    chan = envelope(ENTRIES["public_channel_created"])
    assert chan["target"] == "C0PROJ0001"
    assert chan["attrs"]["channel_privacy"] == "public"
    assert chan["attrs"]["entity_name"] == "proj-falcon"


def test_envelope_mapping_anomaly_reasons_and_outcomes():
    anomaly = envelope(ENTRIES["anomaly"])
    assert anomaly["attrs"]["anomaly_reasons"] == "ip_address,tor"
    assert anomaly["outcome"] == "unknown"
    assert envelope(ENTRIES["user_login_failed"])["outcome"] == "failure"
    assert envelope(ENTRIES["file_download_blocked"])["outcome"] == "failure"
    # session_id null in the fixture -> omitted, not stored as null
    assert "session_id" not in envelope(ENTRIES["user_login_failed"])["attrs"]


def test_envelope_never_raises_on_odd_records():
    env = envelope({"id": "x", "date_create": "not-a-time", "action": "user_login"})
    assert env["event_time"] is None  # dead-letters this record only
    assert framework.validate_envelope(env) == "missing event_time"
    # Placeholder actor (USLACKUSER) without email -> id; message entity -> channel/ts
    env = envelope({
        "id": "y", "date_create": 1787443200, "action": "message_tombstoned",
        "actor": {"type": "user", "user": {"id": "USLACKUSER"}},
        "entity": {"type": "message",
                   "message": {"team": "T1", "channel": "C1", "timestamp": "1787443200.1"}},
    })
    assert env["actor"] == "USLACKUSER"
    assert env["target"] == "C1/1787443200.1"
    assert env["event_time"] == "2026-08-23T00:00:00Z"


# --- fetch / cursoring ------------------------------------------------------


class Resp:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class PagedCtxStub:
    """Serves page1 until a cursor is passed, then page2."""

    def __init__(self, backfill_start):
        self.backfill_start = backfill_start
        self.requests: list[dict] = []
        self.log = logging.getLogger("test")

    def get(self, url, *, params=None, headers=None):
        self.requests.append({"url": url, "params": dict(params), "headers": headers})
        return Resp(PAGE1 if "cursor" not in params else PAGE2)


def test_fetch_pages_through_window(dummy_ctx):
    ctx = PagedCtxStub(dummy_ctx.backfill_start)

    first = POLLER.fetch(None, ctx)
    assert len(first.envelopes) == len(PAGE1["entries"])
    assert first.has_more
    assert first.next_cursor["page_cursor"] == PAGE1["response_metadata"]["next_cursor"]
    req = ctx.requests[0]
    assert req["url"] == "https://api.slack.com/audit/v1/logs"
    assert req["params"]["oldest"] == int(dummy_ctx.backfill_start.timestamp())
    now = int(datetime.datetime.now(UTC).timestamp())
    assert abs(req["params"]["latest"] - (now - WINDOW_LAG_S)) < 60
    assert req["headers"]["Authorization"] == "Bearer xoxp-test-token"

    second = POLLER.fetch(first.next_cursor, ctx)
    assert len(second.envelopes) == len(PAGE2["entries"])
    assert not second.has_more  # next_cursor "" = done
    # Window sealed; bounds are inclusive integers, so the next window starts
    # AT the previous latest (1s overlap, dedup absorbs) - never latest+1.
    assert second.next_cursor == {"since": first.next_cursor["before"]}
    p = ctx.requests[1]["params"]
    assert p["cursor"] == first.next_cursor["page_cursor"]
    assert (p["oldest"], p["latest"]) == (req["params"]["oldest"], req["params"]["latest"])


def test_fetch_waits_when_window_not_open(dummy_ctx):
    ctx = PagedCtxStub(dummy_ctx.backfill_start)
    future = int(datetime.datetime.now(UTC).timestamp())
    result = POLLER.fetch({"since": future}, ctx)
    assert result.envelopes == [] and not result.has_more
    assert result.next_cursor == {"since": future}
    assert ctx.requests == []


def _status_error(code):
    req = httpx.Request("GET", "https://api.slack.com/audit/v1/logs")
    return httpx.HTTPStatusError("x", request=req, response=httpx.Response(code, request=req))


class FailingCtxStub(PagedCtxStub):
    def __init__(self, backfill_start, code):
        super().__init__(backfill_start)
        self.code = code

    def get(self, url, *, params=None, headers=None):
        self.requests.append({"url": url, "params": dict(params), "headers": headers})
        raise _status_error(self.code)


def test_expired_page_cursor_restarts_the_window(dummy_ctx):
    """Slack cursors expire; a persisted one must not crash-loop the poller."""
    ctx = FailingCtxStub(dummy_ctx.backfill_start, 400)
    cursor = {"since": 1787443200, "before": 1787450000, "page_cursor": "stale"}
    result = POLLER.fetch(cursor, ctx)
    assert result.envelopes == []
    assert result.has_more
    assert result.next_cursor == {"since": 1787443200}


@pytest.mark.parametrize("code,cursor", [
    (401, None),
    (403, {"since": 1787443200, "before": 1787450000, "page_cursor": "c"}),
    (400, None),  # 400 without a page cursor is a real error (e.g. invalid_range)
])
def test_auth_and_other_errors_crash(dummy_ctx, code, cursor):
    ctx = FailingCtxStub(dummy_ctx.backfill_start, code)
    with pytest.raises(httpx.HTTPStatusError):
        POLLER.fetch(cursor, ctx)


def test_error_body_and_missing_token_crash(dummy_ctx, monkeypatch):
    class ErrCtx(PagedCtxStub):
        def get(self, url, *, params=None, headers=None):
            return Resp({"ok": False, "error": "feature_not_enabled"})

    with pytest.raises(RuntimeError, match="feature_not_enabled"):
        POLLER.fetch(None, ErrCtx(dummy_ctx.backfill_start))
    monkeypatch.setenv("CAIB_SLACK_AUDIT_TOKEN", "")
    with pytest.raises(SystemExit, match="CAIB_SLACK_AUDIT_TOKEN"):
        POLLER.fetch(None, PagedCtxStub(dummy_ctx.backfill_start))


def test_end_to_end_poll_load_dedup(migrated_db, dummy_ctx):
    ctx = PagedCtxStub(dummy_ctx.backfill_start)
    total = len(PAGE1["entries"]) + len(PAGE2["entries"])

    class FixturePoller:
        module = "slack_audit"
        interval_s = 300

        def fetch(self, cursor, _ctx):
            return POLLER.fetch(cursor, ctx)

    poller = FixturePoller()
    with psycopg.connect(migrated_db) as conn:
        for _ in range(2):
            framework.run_cycle(conn, poller, dummy_ctx)
        assert loader.run_batch(conn, 100, drop_raw=False) == (total, total, 0)

        conn.execute("DELETE FROM ingest.cursors")
        conn.commit()
        for _ in range(2):
            framework.run_cycle(conn, poller, dummy_ctx)
        assert loader.run_batch(conn, 100, drop_raw=False) == (total, 0, 0)
        assert conn.execute(
            "SELECT count(*), count(src_ip) FROM caib.events WHERE module = 'slack_audit'"
        ).fetchone() == (total, total)


# --- rules ------------------------------------------------------------------

BENIGN = ["user_login", "user_login_failed", "file_downloaded", "file_download_blocked",
          "public_channel_created", "role_change_to_user", "app_uninstalled",
          "pref.dnd_enabled", "user_logout"]
# rule -> the fixture action that must fire it
FIRES_ON = {
    "admin_role_granted": "role_change_to_admin",
    "anomaly_detected": "anomaly",
    "app_installed_or_scopes_expanded": "app_installed",
    "auth_settings_changed": "pref.sso_setting_changed",
    "data_export_started": "manual_export_started",
}


def _recent(env: dict, i: int, minutes_ago: float = 5, **overrides) -> dict:
    ts = datetime.datetime.now(UTC) - datetime.timedelta(minutes=minutes_ago)
    return {**env, "event_time": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "event_id": f"{env['event_id']}-{i}", **overrides}


def _land(conn, envs):
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO ingest.landing (received_at, module, payload) VALUES (now(), %s, %s)",
            [("slack_audit", Jsonb(e)) for e in envs],
        )
    conn.commit()
    loader.run_batch(conn, 1000, drop_raw=False)


def _alerts(conn, rule):
    detect.evaluate_rule(conn, rule, CFG)
    conn.commit()
    return conn.execute(
        "SELECT entity, context FROM detect.alerts WHERE rule_id = %s", (rule.id,)
    ).fetchall()


def test_rule_set_is_complete_and_valid(migrated_db):
    rules = {r.id: r for r in detect.load_rules(RULES)}
    assert set(rules) == {f"slack_audit.{n}" for n in [*FIRES_ON, "bulk_file_download", "dead_man"]}
    assert all(r.module == "slack_audit" for r in rules.values())
    with psycopg.connect(migrated_db) as conn:
        detect.validate_rules(conn, list(rules.values()))


@pytest.mark.parametrize("name", sorted(FIRES_ON))
def test_rule_fires_on_its_fixture(migrated_db, name):
    rule = detect.parse_rule(RULES / f"{name}.yaml")
    envs = [_recent(envelope(ENTRIES[FIRES_ON[name]]), 0)]
    with psycopg.connect(migrated_db) as conn:
        _land(conn, envs)
        alerts = _alerts(conn, rule)
    assert len(alerts) == 1
    entity, context = alerts[0]
    assert entity["actor"] == envs[0]["actor"]
    assert context["events"] == 1


def test_anomaly_alert_carries_reasons(migrated_db):
    rule = detect.parse_rule(RULES / "anomaly_detected.yaml")
    with psycopg.connect(migrated_db) as conn:
        _land(conn, [_recent(envelope(ENTRIES["anomaly"]), 0)])
        (entity, context), = _alerts(conn, rule)
    assert entity == {"actor": "carol@example.com", "reasons": "ip_address,tor"}
    assert context["src_ips"] == ["192.0.2.77"]


def test_no_rule_fires_on_benign_fixtures(migrated_db):
    envs = [_recent(envelope(ENTRIES[a]), i) for i, a in enumerate(BENIGN)]
    # one actor at threshold - 1: the fixture's own download plus 48 more files
    dl = envelope(ENTRIES["file_downloaded"])
    envs += [_recent(dl, 100 + i, minutes_ago=20 - i * 0.3, target=f"F0FILE{i:04d}")
             for i in range(48)]
    rules = [r for r in detect.load_rules(RULES) if r.id != "slack_audit.dead_man"]
    with psycopg.connect(migrated_db) as conn:
        _land(conn, envs)
        fired = {r.id: _alerts(conn, r) for r in rules}
    assert {k: v for k, v in fired.items() if v} == {}


def test_bulk_download_fires_at_threshold(migrated_db):
    rule = detect.parse_rule(RULES / "bulk_file_download.yaml")
    dl = envelope(ENTRIES["file_downloaded"])
    # 50 distinct files plus re-downloads of the same file (which must not count)
    envs = [_recent(dl, i, minutes_ago=25 - i * 0.4, target=f"F0FILE{i:04d}") for i in range(50)]
    envs += [_recent(dl, 900 + i, minutes_ago=2, target="F0FILE0000") for i in range(5)]
    with psycopg.connect(migrated_db) as conn:
        _land(conn, envs)
        (entity, context), = _alerts(conn, rule)
    assert entity == {"actor": "bob@example.com"}
    assert context["distinct_files"] == 50
    assert context["events"] == 55


def test_bulk_download_ignores_repeats_of_one_file(migrated_db):
    rule = detect.parse_rule(RULES / "bulk_file_download.yaml")
    dl = envelope(ENTRIES["file_downloaded"])
    envs = [_recent(dl, i, minutes_ago=10) for i in range(80)]  # same file, 80 views
    with psycopg.connect(migrated_db) as conn:
        _land(conn, envs)
        assert _alerts(conn, rule) == []


def test_dead_man_fires_on_silence_and_clears_with_events(migrated_db):
    rule = detect.parse_rule(RULES / "dead_man.yaml")
    with psycopg.connect(migrated_db) as conn:
        alerts = _alerts(conn, rule)
        # entity shape matters: demo_seed auto-closes on entity->>'module'
        assert [a[0] for a in alerts] == [{"module": "slack_audit"}]
        _land(conn, [_recent(envelope(ENTRIES["user_login"]), 0)])
        conn.execute("UPDATE detect.rule_state SET watermark = watermark - interval '1 minute'")
        conn.commit()
        detect.evaluate_rule(conn, rule, CFG)
        conn.commit()
        assert conn.execute(
            "SELECT event_count FROM detect.alerts WHERE rule_id = %s", (rule.id,)
        ).fetchone()[0] == 1  # no new firing once events flow


# --- demo seed --------------------------------------------------------------


def test_demo_seed_slack_envelopes_load_cleanly(migrated_db):
    """`make demo` needs the compose stack; this checks the seed's slack path
    (fixtures -> envelope() -> loader) instead."""
    spec = importlib.util.spec_from_file_location("demo_seed", ROOT / "scripts" / "demo_seed.py")
    demo_seed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo_seed)
    pairs = demo_seed.slack_envelopes()
    assert len(pairs) == len(ENTRIES)
    assert {m for m, _ in pairs} == {"slack_audit"}
    assert all(framework.validate_envelope(e) is None for _, e in pairs)
    with psycopg.connect(migrated_db) as conn:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO ingest.landing (received_at, module, payload) VALUES (now(), %s, %s)",
                [(m, Jsonb(e)) for m, e in pairs],
            )
        conn.commit()
        assert loader.run_batch(conn, 1000, drop_raw=False) == (len(pairs), len(pairs), 0)
