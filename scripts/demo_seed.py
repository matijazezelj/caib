"""`make demo`: cold clone -> populated dashboards + firing sample alerts,
no cloud credentials.

Seeds fixture-shaped events for every module with timestamps rewritten across
the last 30 days (dense in the last 24h, so both the raw and the cagg-backed
views have something to show), drains the loader, refreshes the caggs, then
runs every detection rule once with safety lag 0 (the demo WANTS alerts —
which is also why --advance-watermarks is deliberately not used here).
"""

import datetime
import json
import os
import pathlib
import random
import sys
import time

import psycopg
from psycopg.types.json import Jsonb

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(ROOT / "pollers"))

from caib_core import detect, loader, notify
from caib_pollers.modules.cloudflare_audit import envelope as cf_envelope

UTC = datetime.UTC
SEED = 42
CLONES_PER_RECORD = 150


def pg_url() -> str:
    if url := os.environ.get("CAIB_PG_URL"):
        return url
    port = os.environ.get("CAIB_PG_PORT", "5432")
    password = os.environ.get("CAIB_PG_APP_PASSWORD", "caib-dev-app")
    return f"postgresql://caib_app:{password}@localhost:{port}/caib"


def ct_envelope(record: dict) -> dict:
    """Python mirror of the CloudTrail VRL remap — demo-only, so fixture
    records can be seeded without running Vector."""
    user = record.get("userIdentity") or {}
    attrs = {
        "user_type": user.get("type"),
        "event_source": record.get("eventSource"),
        "aws_region": record.get("awsRegion"),
        "error_code": record.get("errorCode"),
        "mfa": (record.get("additionalEventData") or {}).get("MFAUsed"),
        "policy_arn": (record.get("requestParameters") or {}).get("policyArn"),
        "world_open": True
        if "0.0.0.0/0" in json.dumps(record.get("requestParameters"))
        else None,
    }
    return {
        "event_time": record.get("eventTime"),
        "event_id": record.get("eventID"),
        "actor": user.get("arn") or user.get("type"),
        "src_ip": record.get("sourceIPAddress"),
        "action": record.get("eventName"),
        "target": None,
        "outcome": "failure" if record.get("errorCode") else "success",
        "severity": "info",
        "attrs": {k: v for k, v in attrs.items() if v is not None},
        "raw": record,
    }


def demo_timestamps(rng: random.Random, n: int, now: datetime.datetime) -> list[datetime.datetime]:
    """n timestamps over the last 30 days, ~half of them inside the last 24h."""
    out = []
    for i in range(n):
        if i % 2 == 0:
            delta = datetime.timedelta(seconds=rng.randrange(86_400))
        else:
            delta = datetime.timedelta(seconds=rng.randrange(86_400, 30 * 86_400))
        out.append(now - delta)
    return out


def clone(env: dict, ts: datetime.datetime, i: int) -> dict:
    out = dict(env)
    out["event_time"] = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    out["event_id"] = f"demo-{i}-{env.get('event_id')}"
    return out


def brute_force_burst(now: datetime.datetime) -> list[dict]:
    """5 console-login failures then a success from one IP, well inside the
    threshold rule's 30m lookback. Event ids are unique per run so a repeated
    demo still produces a fresh firing."""
    nonce = now.strftime("%H%M%S")
    base = {
        "actor": "arn:aws:iam::123456789012:user/bob",
        "src_ip": "198.51.100.66",
        "action": "ConsoleLogin",
        "severity": "info",
        "attrs": {"user_type": "IAMUser", "mfa": "No"},
    }
    envs = []
    for i in range(5):
        ts = now - datetime.timedelta(minutes=20 - i * 3)
        envs.append({**base, "event_time": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                     "event_id": f"demo-bf-{nonce}-fail-{i}", "outcome": "failure"})
    ts = now - datetime.timedelta(minutes=4)
    envs.append({**base, "event_time": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "event_id": f"demo-bf-{nonce}-success", "outcome": "success"})
    return envs


def main() -> None:
    rng = random.Random(SEED)
    now = datetime.datetime.now(UTC)
    url = pg_url()

    ct_records = json.loads(
        (ROOT / "modules/aws_cloudtrail/fixtures/cloudtrail_records.json").read_text()
    )["Records"]
    cf_records = []
    for page in ("logs_audit_page1.json", "logs_audit_page2.json"):
        cf_records.extend(
            json.loads((ROOT / "modules/cloudflare_audit/fixtures" / page).read_text())["result"]
        )

    batches: list[tuple[str, dict]] = []
    i = 0
    for record in ct_records:
        env = ct_envelope(record)
        for ts in demo_timestamps(rng, CLONES_PER_RECORD, now):
            batches.append(("aws_cloudtrail", clone(env, ts, i)))
            i += 1
    for record in cf_records:
        env = cf_envelope(record)
        for ts in demo_timestamps(rng, CLONES_PER_RECORD, now):
            batches.append(("cloudflare_audit", clone(env, ts, i)))
            i += 1
    for env in brute_force_burst(now):
        batches.append(("aws_cloudtrail", env))

    try:
        conn = psycopg.connect(url, autocommit=True, connect_timeout=5)
    except psycopg.Error as exc:
        raise SystemExit(f"cannot reach the dev DB ({exc}) — run `make up` first") from exc

    with conn:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO ingest.landing (received_at, module, payload)"
                " VALUES (now(), %s, %s)",
                [(module, Jsonb(env)) for module, env in batches],
            )
        print(f"seeded {len(batches)} envelopes into landing")

        total = 0
        while True:
            moved, _, dead = loader.run_batch(conn, 5000, drop_raw=False)
            total += moved
            if dead:
                raise SystemExit(f"demo seed produced {dead} dead-letter rows — fixture bug")
            if moved < 5000:
                break
        print(f"loader moved {total} envelopes")

        conn.execute("CALL refresh_continuous_aggregate('caib.events_5m', NULL, NULL)")
        conn.execute("CALL refresh_continuous_aggregate('caib.actors_1h', NULL, NULL)")
        print("continuous aggregates refreshed")

        time.sleep(1)  # let ingested_at fall behind now()
        rules = detect.load_rules(ROOT / "detect" / "rules")
        detect.validate_rules(conn, rules)
        cfg = detect.Cfg.from_env()
        cfg.safety_lag_s = 0  # demo wants the just-seeded events in-window
        fired = 0
        for rule in rules:
            if not rule.enabled:
                continue
            stats = detect.evaluate_rule(conn, rule, cfg)
            if stats and stats["rows"]:
                fired += stats["rows"]
                print(f"  rule {rule.id}: {stats['rows']} alert(s)")
        notify.drain(conn, cfg.webhook_url)

        n_alerts = conn.execute("SELECT count(*) FROM detect.alerts").fetchone()[0]
        n_events = conn.execute("SELECT count(*) FROM caib.events").fetchone()[0]

    grafana_port = os.environ.get("CAIB_GRAFANA_PORT", "3000")
    print(f"\ndemo ready: {n_events} events, {n_alerts} alerts")
    print(f"Grafana: http://localhost:{grafana_port}/dashboards (admin / your CAIB_GRAFANA_ADMIN_PASSWORD)")


if __name__ == "__main__":
    main()
