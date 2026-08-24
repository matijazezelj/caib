import datetime
import http.server
import json
import pathlib
import threading
import typing

import psycopg
import pytest
from caib_core import detect, loader, notify
from caib_core.detect import Cfg, Rule
from psycopg.types.json import Jsonb

ROOT = pathlib.Path(__file__).resolve().parent.parent
RULES_DIR = ROOT / "detect" / "rules"
UTC = datetime.UTC

CFG = Cfg(safety_lag_s=0)  # tests ingest "now"; no lag so events fall in-window


def root_login_rule(**overrides) -> Rule:
    kwargs = {
        "id": "aws_cloudtrail.root_console_login",
        "module": "aws_cloudtrail",
        "title": "Root account console login",
        "severity": "high",
        "interval_s": 60,
        "lookback": "15m",
        "sql": (RULES_DIR / "aws_cloudtrail" / "root_console_login.yaml").read_text(),
    }
    kwargs["sql"] = detect.parse_rule(
        RULES_DIR / "aws_cloudtrail" / "root_console_login.yaml"
    ).sql
    kwargs.update(overrides)
    return Rule(**kwargs)


def land_and_load(conn, envelopes, module="aws_cloudtrail"):
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO ingest.landing (received_at, module, payload)"
            " VALUES (now(), %s, %s)",
            [(module, Jsonb(e)) for e in envelopes],
        )
    conn.commit()
    loader.run_batch(conn, 1000, drop_raw=False)


def root_login_envelope(i=0, minutes_ago=5, **overrides):
    ts = (datetime.datetime.now(UTC) - datetime.timedelta(minutes=minutes_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    env = {
        "event_time": ts,
        "event_id": f"root-login-{i}",
        "actor": "arn:aws:iam::123456789012:root",
        "src_ip": "203.0.113.10",
        "action": "ConsoleLogin",
        "outcome": "success",
        "severity": "info",
        "attrs": {"user_type": "Root", "mfa": "No"},
    }
    env.update(overrides)
    return env


# --- rule loading -----------------------------------------------------------


def test_load_seed_rules_and_validate_sql(migrated_db):
    rules = detect.load_rules(RULES_DIR)
    assert len(rules) >= 11
    dead_men = [r for r in rules if r.id.endswith(".dead_man")]
    assert {r.module for r in dead_men} >= {"aws_cloudtrail", "cloudflare_audit"}
    with psycopg.connect(migrated_db) as conn:
        detect.validate_rules(conn, rules)  # EXPLAIN every seed rule


def test_broken_rule_blocks_startup(migrated_db, tmp_path):
    (tmp_path / "bad.yaml").write_text(
        "id: x.bad\nmodule: x\ntitle: t\nseverity: high\ninterval: 60\n"
        "sql: SELECT nonsense FROM caib.no_such_table\n"
    )
    rules = detect.load_rules(tmp_path)
    with psycopg.connect(migrated_db) as conn, pytest.raises(SystemExit):
        detect.validate_rules(conn, rules)


def test_duplicate_rule_ids_rejected(tmp_path):
    body = "id: x.dup\nmodule: x\ntitle: t\nseverity: low\ninterval: 60\nsql: SELECT 1\n"
    (tmp_path / "a.yaml").write_text(body)
    (tmp_path / "b.yaml").write_text(body)
    with pytest.raises(ValueError, match="duplicate"):
        detect.load_rules(tmp_path)


def test_lookback_parsing():
    assert detect.lookback_interval("15m") == "15 minutes"
    assert detect.lookback_interval("2h") == "2 hours"
    assert detect.lookback_interval("30 minutes") == "30 minutes"


# --- evaluation -------------------------------------------------------------


def test_existence_rule_fires_then_folds(migrated_db):
    rule = root_login_rule()
    with psycopg.connect(migrated_db) as conn:
        land_and_load(conn, [root_login_envelope(0)])
        stats = detect.evaluate_rule(conn, rule, CFG)
        conn.commit()
        assert stats["rows"] == 1

        alerts = conn.execute(
            "SELECT dedup_key, severity, status, event_count FROM detect.alerts"
        ).fetchall()
        assert alerts == [
            ("arn:aws:iam::123456789012:root|203.0.113.10", "high", "open", 1)
        ]
        outbox = conn.execute("SELECT kind FROM detect.outbox").fetchall()
        assert outbox == [("new",)]

        # Second window with a fresh event: folds, no new alert row,
        # recurrence NOT enqueued by default.
        land_and_load(conn, [root_login_envelope(1)])
        detect.evaluate_rule(conn, rule, CFG)
        conn.commit()
        assert conn.execute("SELECT count(*) FROM detect.alerts").fetchone()[0] == 1
        assert conn.execute(
            "SELECT event_count FROM detect.alerts"
        ).fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM detect.outbox").fetchone()[0] == 1


def test_unopened_window_skips(migrated_db):
    rule = root_login_rule()
    with psycopg.connect(migrated_db) as conn:
        detect.evaluate_rule(conn, rule, CFG)  # watermark -> now()
        conn.commit()
        # With a real safety lag, wm_to = now()-1h < watermark: window not open.
        assert detect.evaluate_rule(conn, rule, Cfg(safety_lag_s=3600)) is None


def test_crash_between_upsert_and_watermark_is_atomic(migrated_db, monkeypatch):
    rule = root_login_rule()

    def boom(conn, rule_id, wm_to):
        raise RuntimeError("crash before watermark")

    with psycopg.connect(migrated_db) as conn:
        land_and_load(conn, [root_login_envelope(0)])
        monkeypatch.setattr(detect, "_set_watermark", boom)
        with pytest.raises(RuntimeError):
            detect.evaluate_rule(conn, rule, CFG)
        # Whole window rolled back: no alert, no outbox, no watermark.
        assert conn.execute("SELECT count(*) FROM detect.alerts").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM detect.outbox").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM detect.rule_state").fetchone()[0] == 0

        # Recovery: exactly one alert, exactly one notification.
        monkeypatch.undo()
        detect.evaluate_rule(conn, rule, CFG)
        conn.commit()
        assert conn.execute("SELECT count(*) FROM detect.alerts").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM detect.outbox").fetchone()[0] == 1


def test_rate_cap_produces_exactly_one_meta_alert(migrated_db):
    flood = Rule(
        id="x.flood", module="x", title="flood", severity="low", interval_s=60,
        sql=(
            "SELECT i::text AS dedup_key, '{}'::jsonb AS entity,"
            " '{}'::jsonb AS context, now() AS last_event_time"
            " FROM generate_series(1, 600) i"
            " WHERE %(wm_from)s < %(wm_to)s"
        ),
    )
    cfg = Cfg(safety_lag_s=0, max_alerts_per_tick=500)
    with psycopg.connect(migrated_db) as conn:
        stats = detect.evaluate_rule(conn, flood, cfg)
        conn.commit()
        assert stats["overflow"]
        alerts = conn.execute(
            "SELECT dedup_key FROM detect.alerts"
        ).fetchall()
        assert alerts == [("overflow",)]
        outbox = conn.execute("SELECT kind, payload FROM detect.outbox").fetchall()
        assert len(outbox) == 1
        assert outbox[0][0] == "meta"
        assert outbox[0][1]["context"]["rows"] == 501  # LIMIT cap+1
        # Watermark advanced despite the overflow.
        assert conn.execute("SELECT count(*) FROM detect.rule_state").fetchone()[0] == 1


def test_absence_rule_fires_on_silence_and_folds(migrated_db):
    dead_man = detect.parse_rule(RULES_DIR / "aws_cloudtrail" / "dead_man.yaml")
    with psycopg.connect(migrated_db) as conn:
        detect.evaluate_rule(conn, dead_man, CFG)
        conn.commit()
        assert conn.execute(
            "SELECT dedup_key, status FROM detect.alerts"
        ).fetchall() == [("aws_cloudtrail", "open")]

        # Still silent next window: folds into the same alert.
        conn.execute("UPDATE detect.rule_state SET watermark = watermark - interval '1 minute'")
        conn.commit()
        detect.evaluate_rule(conn, dead_man, CFG)
        conn.commit()
        assert conn.execute("SELECT count(*) FROM detect.alerts").fetchone()[0] == 1
        assert conn.execute("SELECT event_count FROM detect.alerts").fetchone()[0] == 2

        # Events flowing again: stops firing (alert stays for a human to close).
        land_and_load(conn, [root_login_envelope(9)])
        conn.execute("UPDATE detect.rule_state SET watermark = watermark - interval '1 minute'")
        conn.commit()
        detect.evaluate_rule(conn, dead_man, CFG)
        conn.commit()
        assert conn.execute("SELECT event_count FROM detect.alerts").fetchone()[0] == 2


def test_threshold_rule_brute_force(migrated_db):
    rule = detect.parse_rule(
        RULES_DIR / "aws_cloudtrail" / "login_failures_then_success.yaml"
    )
    envs = [
        root_login_envelope(i, minutes_ago=10 - i, outcome="failure",
                            actor="alice@example.com")
        for i in range(5)
    ] + [root_login_envelope(99, minutes_ago=1, actor="alice@example.com")]
    with psycopg.connect(migrated_db) as conn:
        land_and_load(conn, envs)
        stats = detect.evaluate_rule(conn, rule, CFG)
        conn.commit()
        assert stats["rows"] == 1
        payload = conn.execute("SELECT context FROM detect.alerts").fetchone()[0]
        assert payload["failures"] == 5
        assert payload["successes"] == 1


def test_advance_watermarks_suppresses_backfill(migrated_db):
    rule = root_login_rule()
    with psycopg.connect(migrated_db) as conn:
        land_and_load(conn, [root_login_envelope(i) for i in range(3)])
        detect.advance_watermarks(conn, [rule])
        conn.commit()
        # The backfilled events sit BELOW the advanced watermark; whether the
        # next tiny window has opened yet or not, nothing may fire.
        stats = detect.evaluate_rule(conn, rule, CFG)
        conn.commit()
        assert stats is None or stats["rows"] == 0
        assert conn.execute("SELECT count(*) FROM detect.alerts").fetchone()[0] == 0


# --- notifier ----------------------------------------------------------------


class Webhook(http.server.BaseHTTPRequestHandler):
    received: typing.ClassVar[list[dict]] = []
    fail = False

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if Webhook.fail:
            self.send_response(500)
        else:
            Webhook.received.append(json.loads(body))
            self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture()
def webhook_url():
    Webhook.received = []
    Webhook.fail = False
    server = http.server.HTTPServer(("127.0.0.1", 0), Webhook)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/hook"
    server.shutdown()


def test_webhook_receives_new_only_by_default(migrated_db, webhook_url):
    rule = root_login_rule()
    with psycopg.connect(migrated_db) as conn:
        land_and_load(conn, [root_login_envelope(0)])
        detect.evaluate_rule(conn, rule, CFG)
        land_and_load(conn, [root_login_envelope(1)])
        detect.evaluate_rule(conn, rule, CFG)  # fold -> recurrence, not enqueued
        conn.commit()

        sent, failed = notify.drain(conn, webhook_url)
        conn.commit()
        assert (sent, failed) == (1, 0)
    assert len(Webhook.received) == 1
    assert Webhook.received[0]["kind"] == "new"
    assert Webhook.received[0]["rule_id"] == "aws_cloudtrail.root_console_login"


def test_failed_post_stays_unsent_and_retries(migrated_db, webhook_url):
    rule = root_login_rule()
    with psycopg.connect(migrated_db) as conn:
        land_and_load(conn, [root_login_envelope(0)])
        detect.evaluate_rule(conn, rule, CFG)
        conn.commit()

        Webhook.fail = True
        sent, failed = notify.drain(conn, webhook_url)
        conn.commit()
        assert (sent, failed) == (0, 1)
        assert conn.execute(
            "SELECT count(*) FROM detect.outbox WHERE sent_at IS NULL"
        ).fetchone()[0] == 1

        Webhook.fail = False
        sent, failed = notify.drain(conn, webhook_url)
        conn.commit()
        assert (sent, failed) == (1, 0)
    assert len(Webhook.received) == 1


def test_blank_url_drains_without_sending(migrated_db):
    rule = root_login_rule()
    with psycopg.connect(migrated_db) as conn:
        land_and_load(conn, [root_login_envelope(0)])
        detect.evaluate_rule(conn, rule, CFG)
        conn.commit()
        assert notify.drain(conn, "") == (1, 0)
        conn.commit()
        assert conn.execute(
            "SELECT count(*) FROM detect.outbox WHERE sent_at IS NULL"
        ).fetchone()[0] == 0


def test_min_severity_filters_at_enqueue(migrated_db):
    rule = root_login_rule(severity="low")
    cfg = Cfg(safety_lag_s=0, min_severity="high")
    with psycopg.connect(migrated_db) as conn:
        land_and_load(conn, [root_login_envelope(0)])
        detect.evaluate_rule(conn, rule, cfg)
        conn.commit()
        assert conn.execute("SELECT count(*) FROM detect.alerts").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM detect.outbox").fetchone()[0] == 0
