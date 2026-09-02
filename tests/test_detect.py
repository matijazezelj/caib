import datetime
import http.server
import json
import os
import pathlib
import signal
import subprocess
import sys
import threading
import time
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


def test_parse_rule_rejects_malformed_rules(tmp_path):
    """parse_rule is the only thing between a typo and a rule that silently
    never fires — validate_rules only checks SQL that parsed."""
    good = {
        "id": "x.r", "module": "x", "title": "t",
        "severity": "low", "interval": 60, "sql": "SELECT 1",
    }

    def write(name: str, body: str) -> pathlib.Path:
        path = tmp_path / name
        path.write_text(body)
        return path

    def yaml_of(doc: dict) -> str:
        return "".join(f"{k}: {v}\n" for k, v in doc.items())

    assert detect.parse_rule(write("ok.yaml", yaml_of(good))).id == "x.r"

    with pytest.raises(TypeError, match="not a mapping"):
        detect.parse_rule(write("list.yaml", "- just\n- a list\n"))

    for key in ("id", "module", "sql", "severity", "interval"):
        doc = {k: v for k, v in good.items() if k != key}
        with pytest.raises(ValueError, match="missing"):
            detect.parse_rule(write(f"no_{key}.yaml", yaml_of(doc)))

    with pytest.raises(ValueError, match="unknown keys"):
        detect.parse_rule(write("extra.yaml", yaml_of({**good, "sevrity": "low"})))

    with pytest.raises(ValueError, match="bad severity"):
        detect.parse_rule(write("sev.yaml", yaml_of({**good, "severity": "urgent"})))

    for bad_interval in ("60s", "0", "-1"):
        with pytest.raises(ValueError, match="interval"):
            detect.parse_rule(
                write(f"int_{bad_interval}.yaml", yaml_of({**good, "interval": bad_interval}))
            )


def test_cfg_from_env_reads_every_knob(monkeypatch):
    """Cfg.from_env is what the running service uses; every test until now
    built Cfg directly, so a typo'd env name would not have shown up."""
    for var in (
        "CAIB_DETECT_SAFETY_LAG", "CAIB_DETECT_MAX_ALERTS_PER_TICK",
        "CAIB_NOTIFY_WEBHOOK_URL", "CAIB_NOTIFY_MIN_SEVERITY",
        "CAIB_DETECT_TICK_S", "CAIB_NOTIFY_FORMAT",
    ):
        monkeypatch.delenv(var, raising=False)

    default = Cfg.from_env()
    assert (default.safety_lag_s, default.max_alerts_per_tick) == (60.0, 500)
    assert (default.webhook_url, default.min_severity) == ("", "info")
    assert (default.tick_s, default.notify_format) == (15.0, "json")

    monkeypatch.setenv("CAIB_DETECT_SAFETY_LAG", "0")
    monkeypatch.setenv("CAIB_DETECT_MAX_ALERTS_PER_TICK", "7")
    monkeypatch.setenv("CAIB_NOTIFY_WEBHOOK_URL", "https://hooks.slack.com/services/T/B/X")
    monkeypatch.setenv("CAIB_NOTIFY_MIN_SEVERITY", "high")
    monkeypatch.setenv("CAIB_DETECT_TICK_S", "0.5")
    cfg = Cfg.from_env()
    assert (cfg.safety_lag_s, cfg.max_alerts_per_tick, cfg.tick_s) == (0.0, 7, 0.5)
    assert cfg.min_severity == "high"
    # A Slack webhook auto-selects Block Kit; an explicit format still wins.
    assert cfg.notify_format == "slack"
    monkeypatch.setenv("CAIB_NOTIFY_FORMAT", "json")
    assert Cfg.from_env().notify_format == "json"


@pytest.mark.parametrize("value", ["urgent", "HIGH", "", "3"])
def test_bad_min_severity_fails_at_startup(monkeypatch, value):
    """A severity outside the enum used to survive Cfg.from_env() and blow up in
    SEVERITIES.index() at the first alert — hours or days later, inside a rule's
    transaction, on a service that had looked healthy the whole time."""
    monkeypatch.setenv("CAIB_NOTIFY_MIN_SEVERITY", value)
    with pytest.raises(SystemExit, match="CAIB_NOTIFY_MIN_SEVERITY"):
        Cfg.from_env()


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


def test_engine_exits_when_the_database_connection_dies(migrated_db, tmp_path):
    """The loop swallows psycopg.Error so a bad rule doesn't kill the engine.
    A lost connection must not be swallowed too: it never heals on the same
    connection, so the process has to exit and be restarted."""
    (tmp_path / "tick.yaml").write_text(
        "id: x.tick\nmodule: x\ntitle: tick\nseverity: low\ninterval: 1\n"
        "sql: SELECT 'k' AS dedup_key, '{}'::jsonb AS entity, '{}'::jsonb AS context,"
        " now() AS last_event_time WHERE %(wm_from)s > %(wm_to)s\n"
    )
    app_name = "caib_detect_conn_loss"
    env = {
        **os.environ,
        "CAIB_PG_URL": f"{migrated_db}?application_name={app_name}",
        "CAIB_RULES_DIR": str(tmp_path),
        "CAIB_DETECT_TICK_S": "0.1",
        "CAIB_DETECT_SAFETY_LAG": "0",
        "CAIB_NOTIFY_WEBHOOK_URL": "",
    }
    proc = subprocess.Popen(
        [sys.executable, "-c", "from caib_core.detect import main; main()"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        with psycopg.connect(migrated_db, autocommit=True) as conn:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if conn.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE application_name = %s",
                    (app_name,),
                ).fetchone()[0]:
                    break
                time.sleep(0.1)
            else:
                raise AssertionError("detect never connected")
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                " WHERE application_name = %s",
                (app_name,),
            )
        assert proc.wait(timeout=30) != 0, "detect kept running without a database"
    finally:
        proc.kill()


def _wait_for_beat(conn, after=None, timeout_s=30.0):
    """Next ops.heartbeat stamp from detect newer than `after`."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        row = conn.execute(
            "SELECT beat_at FROM ops.heartbeat WHERE service = 'detect'"
        ).fetchone()
        if row and (after is None or row[0] > after):
            return row[0]
        time.sleep(0.1)
    raise AssertionError("detect stopped beating")


def test_sighup_with_an_unparseable_rule_keeps_the_old_set(migrated_db, tmp_path):
    """The reload only caught ValueError/SystemExit, so a YAML syntax error in
    the rules directory — the exact thing you SIGHUP after — killed the running
    engine. It must log and keep evaluating the rules it already has."""
    (tmp_path / "tick.yaml").write_text(
        "id: x.tick\nmodule: x\ntitle: tick\nseverity: low\ninterval: 1\n"
        "sql: SELECT 'k' AS dedup_key, '{}'::jsonb AS entity, '{}'::jsonb AS context,"
        " now() AS last_event_time WHERE %(wm_from)s > %(wm_to)s\n"
    )
    env = {
        **os.environ,
        "CAIB_PG_URL": migrated_db,
        "CAIB_RULES_DIR": str(tmp_path),
        "CAIB_DETECT_TICK_S": "0.1",
        "CAIB_DETECT_SAFETY_LAG": "0",
        "CAIB_NOTIFY_WEBHOOK_URL": "",
    }
    proc = subprocess.Popen(
        [sys.executable, "-c", "from caib_core.detect import main; main()"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        with psycopg.connect(migrated_db, autocommit=True) as conn:
            beat = _wait_for_beat(conn)
            (tmp_path / "broken.yaml").write_text("id: x.broken\n\tsql: [unclosed\n")
            proc.send_signal(signal.SIGHUP)
            beat = _wait_for_beat(conn, after=beat)  # survived the reload
            _wait_for_beat(conn, after=beat)  # and kept ticking
        assert proc.poll() is None, "detect died on a broken rule file"
        proc.terminate()
        _out, err = proc.communicate(timeout=30)
        assert "rule reload failed, keeping old set" in err
    finally:
        proc.kill()


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
