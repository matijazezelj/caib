"""1M-event benchmark harness (`make bench`).

Generates representative events (CloudTrail/GCP/Slack-shaped, timestamps spread
over 30 days), pumps them through landing -> loader, and records measurements to
bench/results/<version>-<date>.json. Measurement, not a gate: no thresholds.

Runs against its own throwaway timescaledb container so it never touches dev data.
Row count via CAIB_BENCH_EVENTS (default 1_000_000); seeded rng for reproducibility.
"""

import datetime
import json
import pathlib
import random
import subprocess
import sys
import time
import uuid

import psycopg

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "core"))
from caib_core import loader, migrate

ROOT = pathlib.Path(__file__).resolve().parent.parent
TSDB_IMAGE = "timescale/timescaledb:2.30.0-pg18"
UTC = datetime.UTC

N_EVENTS = 1_000_000
SEED = 20260824
DAYS = 30
REINSERT_DAY = 25  # day index whose rows get re-inserted against a compressed chunk

ACTORS = [f"user{i}@example.com" for i in range(400)] + [
    f"arn:aws:iam::123456789012:role/svc-{i}" for i in range(100)
]
IPS = [f"203.0.113.{i}" for i in range(1, 255)] + [f"198.51.100.{i}" for i in range(1, 255)]

CT_ACTIONS = [
    "ConsoleLogin", "AssumeRole", "GetObject", "PutObject", "RunInstances",
    "DescribeInstances", "CreateUser", "AttachUserPolicy", "StopLogging",
    "AuthorizeSecurityGroupIngress", "GetCallerIdentity", "ListBuckets",
]
GCP_ACTIONS = [
    "google.iam.admin.v1.CreateServiceAccountKey", "SetIamPolicy",
    "google.logging.v2.ConfigServiceV2.UpdateSink", "v1.compute.instances.insert",
    "google.iam.v1.IAMPolicy.SetIamPolicy", "storage.objects.get",
]
SLACK_ACTIONS = [
    "user_login", "file_downloaded", "app_installed", "user_role_changed",
    "channel_created", "message_deleted",
]


def ct_envelope(rng: random.Random, ts: str) -> dict:
    action = rng.choice(CT_ACTIONS)
    actor = rng.choice(ACTORS)
    ip = rng.choice(IPS)
    failure = rng.random() < 0.08
    event_id = str(uuid.UUID(int=rng.getrandbits(128)))
    return {
        "event_time": ts,
        "event_id": event_id,
        "actor": actor,
        "src_ip": ip,
        "action": action,
        "target": f"arn:aws:s3:::bucket-{rng.randrange(50)}/key-{rng.randrange(10000)}",
        "outcome": "failure" if failure else "success",
        "severity": "info",
        "attrs": {
            "user_type": rng.choice(["IAMUser", "AssumedRole", "Root"]),
            "event_source": "s3.amazonaws.com",
            "aws_region": rng.choice(["us-east-1", "eu-central-1", "eu-west-1"]),
            "error_code": "AccessDenied" if failure else None,
            "mfa": rng.choice(["Yes", "No"]),
        },
        "raw": {
            "eventVersion": "1.09",
            "eventID": event_id,
            "eventTime": ts,
            "eventName": action,
            "eventSource": "s3.amazonaws.com",
            "awsRegion": "eu-central-1",
            "sourceIPAddress": ip,
            "userAgent": "aws-sdk-go/1.44.256 (go1.20; linux; amd64)",
            "userIdentity": {
                "type": "AssumedRole",
                "principalId": f"AROA{rng.getrandbits(64):016X}",
                "arn": actor,
                "accountId": "123456789012",
                "sessionContext": {
                    "attributes": {"mfaAuthenticated": "false", "creationDate": ts},
                    "sessionIssuer": {"type": "Role", "arn": actor},
                },
            },
            "requestParameters": {
                "bucketName": f"bucket-{rng.randrange(50)}",
                "key": f"key-{rng.randrange(10000)}",
            },
            "responseElements": None,
            "readOnly": action.startswith(("Get", "List", "Describe")),
            "eventType": "AwsApiCall",
            "managementEvent": True,
            "recipientAccountId": "123456789012",
        },
    }


def gcp_envelope(rng: random.Random, ts: str) -> dict:
    action = rng.choice(GCP_ACTIONS)
    actor = rng.choice(ACTORS)
    ip = rng.choice(IPS)
    denied = rng.random() < 0.05
    event_id = f"{rng.getrandbits(64):016x}"
    return {
        "event_time": ts,
        "event_id": event_id,
        "actor": actor,
        "src_ip": ip,
        "action": action,
        "target": f"projects/example-project/serviceAccounts/sa-{rng.randrange(40)}",
        "outcome": "failure" if denied else "success",
        "severity": "low" if denied else "info",
        "attrs": {
            "service_name": "iam.googleapis.com",
            "log_id": "cloudaudit.googleapis.com/activity",
            "permission_denied": denied,
        },
        "raw": {
            "insertId": event_id,
            "timestamp": ts,
            "logName": "projects/example-project/logs/cloudaudit.googleapis.com%2Factivity",
            "severity": "ERROR" if denied else "NOTICE",
            "protoPayload": {
                "@type": "type.googleapis.com/google.cloud.audit.AuditLog",
                "authenticationInfo": {"principalEmail": actor},
                "requestMetadata": {
                    "callerIp": ip,
                    "callerSuppliedUserAgent": "google-cloud-sdk gcloud/456.0.0",
                },
                "serviceName": "iam.googleapis.com",
                "methodName": action,
                "resourceName": f"projects/example-project/serviceAccounts/sa-{rng.randrange(40)}",
                "status": {"code": 7, "message": "PERMISSION_DENIED"} if denied else {},
            },
            "resource": {"type": "service_account", "labels": {"project_id": "example-project"}},
        },
    }


def slack_envelope(rng: random.Random, ts: str) -> dict:
    action = rng.choice(SLACK_ACTIONS)
    actor = rng.choice(ACTORS)
    ip = rng.choice(IPS)
    event_id = f"{rng.getrandbits(64):016x}"
    return {
        "event_time": ts,
        "event_id": event_id,
        "actor": actor,
        "src_ip": ip,
        "action": action,
        "target": f"C{rng.getrandbits(40):010X}",
        "outcome": "success",
        "severity": "info",
        "attrs": {"entity_type": "channel", "workspace": "acme"},
        "raw": {
            "id": event_id,
            "date_create": ts,
            "action": action,
            "actor": {"type": "user", "user": {"id": f"U{rng.getrandbits(40):010X}", "email": actor}},
            "entity": {"type": "channel", "channel": {"id": f"C{rng.getrandbits(40):010X}"}},
            "context": {"location": {"type": "workspace", "id": "T024BE7LD"}, "ip_address": ip},
        },
    }


GENERATORS = [
    ("aws_cloudtrail", 0.60, ct_envelope),
    ("gcp_audit", 0.25, gcp_envelope),
    ("slack_audit", 0.15, slack_envelope),
]


def start_container() -> tuple[str, str]:
    name = f"caib-bench-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        [
            "docker", "run", "-d", "--rm", "--name", name,
            "-e", "POSTGRES_PASSWORD=bench", "-e", "POSTGRES_DB=caib",
            "-p", "127.0.0.1:0:5432", TSDB_IMAGE,
            "postgres",
            "-c", "shared_preload_libraries=timescaledb",
            "-c", "default_toast_compression=lz4",
            "-c", "timezone=UTC",
            "-c", "shared_buffers=1GB",
            "-c", "max_wal_size=4GB",
        ],
        check=True, capture_output=True,
    )
    out = subprocess.run(
        ["docker", "port", name, "5432/tcp"], check=True, capture_output=True, text=True
    ).stdout
    port = out.strip().splitlines()[0].rsplit(":", 1)[1]
    url = f"postgresql://postgres:bench@127.0.0.1:{port}/caib"
    deadline = time.monotonic() + 90
    while True:
        try:
            with psycopg.connect(url, connect_timeout=3) as conn:
                conn.execute("SELECT 1")
            return name, url
        except psycopg.Error:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.5)


def generate_and_copy(conn: psycopg.Connection, n_events: int) -> tuple[int, float, list]:
    """COPY n_events into landing. Returns (raw_bytes, copy_seconds, reinsert_rows)."""
    rng = random.Random(SEED)
    now = datetime.datetime.now(UTC)
    raw_bytes = 0
    reinsert_rows: list[tuple[str, str]] = []
    t0 = time.monotonic()
    with conn.cursor() as cur, cur.copy(
        "COPY ingest.landing (received_at, module, payload) FROM STDIN"
    ) as copy:
        for i in range(n_events):
            day = rng.randrange(DAYS)
            offset = datetime.timedelta(days=day, seconds=rng.randrange(86400))
            ts = (now - offset).strftime("%Y-%m-%dT%H:%M:%SZ")
            pick = rng.random()
            acc = 0.0
            for module, weight, gen in GENERATORS:
                acc += weight
                if pick <= acc:
                    break
            payload = json.dumps(gen(rng, ts), separators=(",", ":"))
            raw_bytes += len(payload)
            copy.write_row((now.isoformat(), module, payload))
            if day == REINSERT_DAY:
                reinsert_rows.append((module, payload))
    conn.commit()
    return raw_bytes, time.monotonic() - t0, reinsert_rows


def drain_loader(conn: psycopg.Connection, batch: int = 5000) -> tuple[int, float]:
    total = 0
    t0 = time.monotonic()
    while True:
        moved, _, _ = loader.run_batch(conn, batch, drop_raw=False)
        total += moved
        if moved < batch:
            return total, time.monotonic() - t0


def query_latencies(conn: psycopg.Connection) -> dict:
    queries = {
        "module_time_filter_3d": (
            "SELECT count(*) FROM caib.events"
            " WHERE module = 'aws_cloudtrail' AND event_time > now() - interval '3 days'"
        ),
        "actor_top10_7d": (
            "SELECT actor, count(*) FROM caib.events"
            " WHERE event_time > now() - interval '7 days'"
            " GROUP BY 1 ORDER BY 2 DESC LIMIT 10"
        ),
        "search_ilike_30d": (
            "SELECT count(*) FROM caib.events"
            " WHERE event_time > now() - interval '30 days'"
            "   AND search_text ILIKE '%attachuserpolicy%'"
        ),
    }
    out = {}
    for key, sql_text in queries.items():
        times = []
        for _ in range(3):
            t0 = time.monotonic()
            conn.execute(sql_text).fetchall()
            times.append(time.monotonic() - t0)
        out[key] = {"best_s": round(min(times), 4), "worst_s": round(max(times), 4)}
    return out


def sizes(conn: psycopg.Connection) -> dict:
    table_b, index_b, toast_b, total_b = conn.execute(
        "SELECT table_bytes, index_bytes, toast_bytes, total_bytes"
        " FROM hypertable_detailed_size('caib.events')"
    ).fetchone()
    idx_names = [
        r[0] for r in conn.execute(
            "SELECT indexname FROM pg_indexes"
            " WHERE schemaname = 'caib' AND tablename = 'events' ORDER BY 1"
        )
    ]
    indexes = {}
    for idx in idx_names:
        indexes[idx] = conn.execute(
            "SELECT hypertable_index_size(%s)", (f"caib.{idx}",)
        ).fetchone()[0]
    return {
        "heap_bytes": table_b,
        "toast_bytes": toast_b,
        "index_bytes": index_b,
        "total_bytes": total_b,
        "indexes": indexes,
    }


def main() -> None:
    import os

    n_events = int(os.environ.get("CAIB_BENCH_EVENTS", N_EVENTS))
    os.environ.setdefault("CAIB_PG_APP_PASSWORD", "bench-app")
    os.environ.setdefault("CAIB_PG_RO_PASSWORD", "bench-ro")

    print(f"bench: {n_events} events, image {TSDB_IMAGE}")
    name, url = start_container()
    results: dict = {
        "generated_at": datetime.datetime.now(UTC).isoformat(),
        "image": TSDB_IMAGE,
        "n_events": n_events,
        "seed": SEED,
        "module_mix": {m: w for m, w, _ in GENERATORS},
    }
    try:
        migrate.run(url, ROOT / "db" / "migrations")
        with psycopg.connect(url, autocommit=True) as conn:
            raw_bytes, copy_s, reinsert_rows = generate_and_copy(conn, n_events)
            results["raw_json_bytes"] = raw_bytes
            results["landing_copy"] = {
                "seconds": round(copy_s, 1),
                "events_per_s": round(n_events / copy_s),
            }
            print(f"copied {n_events} envelopes ({raw_bytes / 1e9:.2f} GB JSON) in {copy_s:.0f}s")

            loaded, load_s = drain_loader(conn)
            results["loader"] = {
                "seconds": round(load_s, 1),
                "events_per_s": round(loaded / load_s),
            }
            print(f"loaded {loaded} events in {load_s:.0f}s ({loaded / load_s:.0f}/s)")

            results["hot"] = sizes(conn)
            results["search_text_bytes"] = conn.execute(
                "SELECT sum(pg_column_size(search_text)) FROM caib.events"
            ).fetchone()[0]
            results["queries_uncompressed"] = query_latencies(conn)

            t0 = time.monotonic()
            conn.execute("SELECT count(compress_chunk(c)) FROM show_chunks('caib.events') c")
            results["compress_all_seconds"] = round(time.monotonic() - t0, 1)
            before_b, after_b = conn.execute(
                "SELECT before_compression_total_bytes, after_compression_total_bytes"
                " FROM hypertable_compression_stats('caib.events')"
            ).fetchone()
            results["compression"] = {
                "before_bytes": before_b,
                "after_bytes": after_b,
                "ratio": round(before_b / after_b, 2) if after_b else None,
            }
            results["compressed"] = sizes(conn)
            results["queries_compressed"] = query_latencies(conn)
            print(
                f"compression: {before_b / 1e9:.2f} GB -> {after_b / 1e9:.2f} GB"
                f" ({results['compression']['ratio']}x)"
            )

            # Dedup re-insert against now-compressed chunks (worst case for upserts).
            with conn.cursor() as cur, cur.copy(
                "COPY ingest.landing (received_at, module, payload) FROM STDIN"
            ) as copy:
                for module, payload in reinsert_rows:
                    copy.write_row((datetime.datetime.now(UTC).isoformat(), module, payload))
            n_before = conn.execute("SELECT count(*) FROM caib.events").fetchone()[0]
            redone, redo_s = drain_loader(conn)
            n_after = conn.execute("SELECT count(*) FROM caib.events").fetchone()[0]
            assert n_before == n_after, "dedup re-insert must not add rows"
            results["dedup_reinsert_compressed"] = {
                "events": redone,
                "seconds": round(redo_s, 1),
                "events_per_s": round(redone / redo_s) if redo_s else None,
            }
            print(f"dedup re-insert vs compressed: {redone} events in {redo_s:.1f}s")
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)

    # Where each number comes from — these get quoted to clients.
    results["provenance"] = {
        "raw_json_bytes": "sum of len(json.dumps(envelope)) over all generated envelopes, pre-insert",
        "landing_copy": "wall clock of COPY into ingest.landing from host psycopg",
        "loader": "wall clock of loader.run_batch drain, landing->events incl. sha256 dedup + search_text build",
        "hot/compressed": "hypertable_detailed_size('caib.events'); per-index hypertable_index_size()",
        "search_text_bytes": "sum(pg_column_size(search_text)) over caib.events while uncompressed",
        "compression": "hypertable_compression_stats('caib.events') before/after_compression_total_bytes, after compress_chunk() over ALL chunks",
        "queries_*": "psycopg wall clock, best/worst of 3 consecutive runs each",
        "dedup_reinsert_compressed": "re-COPY of one old day's envelopes + loader drain against fully compressed chunks; asserts zero new rows",
        "methodology": "single run on a shared machine; expect +-20-30% run-to-run variance (observed: dedup re-insert 14.9k vs 19.3k events/s across runs with no code change on that path)",
    }

    from importlib.metadata import version

    ver = version("caib-core")
    out_dir = ROOT / "bench" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{ver}-{datetime.datetime.now(UTC).date().isoformat()}.json"
    out_path.write_text(json.dumps(results, indent=2) + "\n")
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
