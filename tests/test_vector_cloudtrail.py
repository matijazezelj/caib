"""Phase 3 AC: the CloudTrail push path.

- vector test proves the remap fans one S3 object ({"Records":[...]}) out to
  one envelope per record on the pinned Vector version.
- A stdin->postgres run proves the real VRL + postgres sink land N rows for
  N Records and the loader maps them.
- A LocalStack S3+SQS run proves source framing + gzip end-to-end.
- A malformed remap must fail loudly on landing's NOT NULLs, not drop silently.
"""

import gzip
import json
import pathlib
import subprocess
import time
import uuid

import psycopg
import pytest
import yaml
from caib_core import loader

ROOT = pathlib.Path(__file__).resolve().parent.parent
VECTOR_IMAGE = "timberio/vector:0.57.0-debian"
FIXTURE = ROOT / "modules" / "aws_cloudtrail" / "fixtures" / "cloudtrail_records.json"
MODULE_YAML = ROOT / "vector" / "modules" / "aws_cloudtrail.yaml"
BASE_YAML = ROOT / "vector" / "vector.yaml"

N_RECORDS = len(json.loads(FIXTURE.read_text())["Records"])


def remap_vrl() -> str:
    return yaml.safe_load(MODULE_YAML.read_text())["transforms"]["remap_aws_cloudtrail"]["source"]


def host_url(pg_url: str) -> str:
    return pg_url.replace("127.0.0.1", "host.docker.internal").replace(
        "localhost", "host.docker.internal"
    )


def run_vector(config: dict, cfg_dir: pathlib.Path, stdin: bytes | None = None,
               timeout: int = 60, extra_args: list[str] | None = None) -> subprocess.CompletedProcess:
    cfg_path = cfg_dir / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(config))
    cmd = [
        "docker", "run", "--rm", "-i",
        "--add-host=host.docker.internal:host-gateway",
        "-v", f"{cfg_dir}:/cfg:ro",
        "-e", "AWS_ACCESS_KEY_ID=test", "-e", "AWS_SECRET_ACCESS_KEY=test",
        VECTOR_IMAGE, *(extra_args or ["--config", "/cfg/config.yaml"]),
    ]
    return subprocess.run(
        cmd, input=stdin, capture_output=True, timeout=timeout, check=False
    )


def test_vector_test_remap_fan_out(tmp_path):
    """`vector test` with the real config files: every fanned-out event is a
    well-formed landing row."""
    message = FIXTURE.read_text()
    tests_cfg = {
        "tests": [
            {
                "name": "cloudtrail_fan_out",
                "inputs": [
                    {
                        "insert_at": "remap_aws_cloudtrail",
                        "type": "log",
                        "log_fields": {"message": message},
                    }
                ],
                "outputs": [
                    {
                        "extract_from": "remap_aws_cloudtrail",
                        "conditions": [
                            {
                                "type": "vrl",
                                "source": (
                                    'assert!(.module == "aws_cloudtrail")\n'
                                    "assert!(is_string(.payload.event_time))\n"
                                    "assert!(is_string(.payload.action))\n"
                                    "assert!(exists(.received_at))\n"
                                    "assert!(is_object(.payload.attrs))\n"
                                ),
                            }
                        ],
                    }
                ],
            }
        ]
    }
    (tmp_path / "tests.yaml").write_text(yaml.safe_dump(tests_cfg))
    (tmp_path / "vector.yaml").write_text(BASE_YAML.read_text())
    (tmp_path / "aws_cloudtrail.yaml").write_text(MODULE_YAML.read_text())
    proc = subprocess.run(
        [
            "docker", "run", "--rm",
            "-v", f"{tmp_path}:/cfg:ro",
            "-e", "CAIB_PG_URL=postgresql://u:p@localhost:5432/x",
            "-e", "CAIB_CLOUDTRAIL_SQS_URL=https://sqs.us-east-1.amazonaws.com/123456789012/q",
            VECTOR_IMAGE,
            "test", "/cfg/vector.yaml", "/cfg/aws_cloudtrail.yaml", "/cfg/tests.yaml",
        ],
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert proc.returncode == 0, f"vector test failed:\n{proc.stdout}\n{proc.stderr}"


def test_stdin_to_landing_n_rows_for_n_records(migrated_db, tmp_path):
    config = {
        "sources": {"in": {"type": "stdin"}},
        "transforms": {
            "remap_aws_cloudtrail": {
                "type": "remap",
                "inputs": ["in"],
                "source": remap_vrl(),
            }
        },
        "sinks": {
            "pg": {
                "type": "postgres",
                "inputs": ["remap_*"],
                "endpoint": host_url(migrated_db),
                "table": "ingest.landing",
                "batch": {"max_events": 500, "timeout_secs": 1},
            }
        },
    }
    line = json.dumps(json.loads(FIXTURE.read_text())) + "\n"
    proc = run_vector(config, tmp_path, stdin=line.encode())
    assert proc.returncode == 0, proc.stderr.decode()

    with psycopg.connect(migrated_db) as conn:
        rows = conn.execute(
            "SELECT payload->>'action', payload->>'outcome', payload->'attrs'"
            " FROM ingest.landing ORDER BY payload->>'event_time'"
        ).fetchall()
        assert len(rows) == N_RECORDS
        by_action = {r[0]: r for r in rows}
        assert by_action["ConsoleLogin"][1] == "success"
        assert by_action["ConsoleLogin"][2]["mfa"] == "No"
        assert by_action["ConsoleLogin"][2]["user_type"] == "Root"
        assert by_action["AssumeRole"][1] == "failure"
        assert by_action["AssumeRole"][2]["error_code"] == "AccessDenied"

        assert loader.run_batch(conn, 100, drop_raw=False) == (N_RECORDS, N_RECORDS, 0)
        actor = conn.execute(
            "SELECT actor FROM caib.events WHERE action = 'ConsoleLogin'"
        ).fetchone()[0]
        assert actor == "arn:aws:iam::123456789012:root"


def test_malformed_remap_fails_loudly_on_not_nulls(migrated_db, tmp_path):
    config = {
        "sources": {"in": {"type": "stdin"}},
        "transforms": {
            "remap_bad": {
                "type": "remap",
                "inputs": ["in"],
                # Forgets "module" - landing's NOT NULL must reject the batch.
                "source": '. = {"received_at": now(), "payload": {"oops": true}}',
            }
        },
        "sinks": {
            "pg": {
                "type": "postgres",
                "inputs": ["remap_*"],
                "endpoint": host_url(migrated_db),
                "table": "ingest.landing",
                "batch": {"max_events": 10, "timeout_secs": 1},
            }
        },
    }
    try:
        proc = run_vector(config, tmp_path, stdin=b'{"x": 1}\n', timeout=25)
        out = proc.stdout.decode() + proc.stderr.decode()
    except subprocess.TimeoutExpired as exc:
        # Vector retrying the poisoned batch forever is the "loud" failure mode.
        out = (exc.stdout or b"").decode() + (exc.stderr or b"").decode()
    assert "error" in out.lower()
    with psycopg.connect(migrated_db) as conn:
        assert conn.execute("SELECT count(*) FROM ingest.landing").fetchone()[0] == 0


@pytest.mark.slow
def test_s3_sqs_localstack_end_to_end(migrated_db, tmp_path):
    boto3 = pytest.importorskip("boto3")

    # Vector talks to LocalStack over a shared docker network: the loopback
    # port publish below is only for boto3 on the host. (host.docker.internal
    # can't reach 127.0.0.1-bound host ports on Linux runners.)
    net_name = f"caib-net-{uuid.uuid4().hex[:8]}"
    subprocess.run(["docker", "network", "create", net_name], check=True, capture_output=True)
    ls_name = f"caib-ls-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        [
            "docker", "run", "-d", "--rm", "--name", ls_name,
            "--network", net_name,
            "-e", "SERVICES=s3,sqs",
            "-p", "127.0.0.1:0:4566",
            # Last community-edition series; localstack:latest (2026.x) demands
            # a license token and exits.
            "localstack/localstack:4.14",
        ],
        check=True, capture_output=True,
    )
    vec_name = f"caib-vec-{uuid.uuid4().hex[:8]}"
    try:
        port = subprocess.run(
            ["docker", "port", ls_name, "4566/tcp"],
            check=True, capture_output=True, text=True,
        ).stdout.strip().splitlines()[0].rsplit(":", 1)[1]
        endpoint = f"http://127.0.0.1:{port}"

        # Wait for localstack readiness.
        s3 = boto3.client(
            "s3", endpoint_url=endpoint, region_name="us-east-1",
            aws_access_key_id="test", aws_secret_access_key="test",
        )
        deadline = time.monotonic() + 120
        while True:
            try:
                s3.list_buckets()
                break
            except Exception:
                if time.monotonic() > deadline:
                    raise
                time.sleep(1)

        sqs = boto3.client(
            "sqs", endpoint_url=endpoint, region_name="us-east-1",
            aws_access_key_id="test", aws_secret_access_key="test",
        )
        s3.create_bucket(Bucket="ct-logs")
        queue_url = sqs.create_queue(QueueName="ct-q")["QueueUrl"]

        key = "AWSLogs/123456789012/CloudTrail/us-east-1/2026/08/23/x.json.gz"
        body = gzip.compress(json.dumps(json.loads(FIXTURE.read_text())).encode())
        s3.put_object(Bucket="ct-logs", Key=key, Body=body)
        notification = {
            "Records": [
                {
                    "eventVersion": "2.1",
                    "eventSource": "aws:s3",
                    "awsRegion": "us-east-1",
                    "eventTime": "2026-08-23T09:10:00.000Z",
                    "eventName": "ObjectCreated:Put",
                    "userIdentity": {"principalId": "AWS:AIDAEXAMPLE"},
                    "requestParameters": {"sourceIPAddress": "203.0.113.1"},
                    "responseElements": {
                        "x-amz-request-id": "EXAMPLE123456789",
                        "x-amz-id-2": "EXAMPLE123/abcdefghijk",
                    },
                    "s3": {
                        "s3SchemaVersion": "1.0",
                        "configurationId": "ct-notify",
                        "bucket": {
                            "name": "ct-logs",
                            "ownerIdentity": {"principalId": "EXAMPLE"},
                            "arn": "arn:aws:s3:::ct-logs",
                        },
                        "object": {
                            "key": key,
                            "size": len(body),
                            "eTag": "0123456789abcdef0123456789abcdef",
                            "sequencer": "0055AED6DCD90281E5",
                        },
                    },
                }
            ]
        }
        sqs.send_message(QueueUrl=queue_url, MessageBody=json.dumps(notification))

        module_cfg = yaml.safe_load(MODULE_YAML.read_text())
        src = module_cfg["sources"]["aws_cloudtrail_s3"]
        src["region"] = "us-east-1"
        src["endpoint"] = f"http://{ls_name}:4566"
        src["force_path_style"] = True
        src["sqs"]["queue_url"] = (
            queue_url.replace(f"127.0.0.1:{port}", f"{ls_name}:4566")
            .replace(f"localhost:{port}", f"{ls_name}:4566")
        )
        config = {
            **module_cfg,
            "sinks": {
                "pg": {
                    "type": "postgres",
                    "inputs": ["remap_*"],
                    "endpoint": host_url(migrated_db),
                    "table": "ingest.landing",
                    "batch": {"max_events": 500, "timeout_secs": 1},
                }
            },
        }
        (tmp_path / "config.yaml").write_text(yaml.safe_dump(config))
        subprocess.run(
            [
                "docker", "run", "-d", "--rm", "--name", vec_name,
                "--network", net_name,
                "--add-host=host.docker.internal:host-gateway",
                "-v", f"{tmp_path}:/cfg:ro",
                "-e", "AWS_ACCESS_KEY_ID=test", "-e", "AWS_SECRET_ACCESS_KEY=test",
                VECTOR_IMAGE, "--config", "/cfg/config.yaml",
            ],
            check=True, capture_output=True,
        )

        with psycopg.connect(migrated_db) as conn:
            deadline = time.monotonic() + 90
            while True:
                n = conn.execute("SELECT count(*) FROM ingest.landing").fetchone()[0]
                if n >= N_RECORDS:
                    break
                if time.monotonic() > deadline:
                    logs = subprocess.run(
                        ["docker", "logs", vec_name],
                        capture_output=True, text=True, check=False,
                    )
                    raise AssertionError(
                        f"expected {N_RECORDS} rows, got {n}\n{logs.stdout}\n{logs.stderr}"
                    )
                time.sleep(2)
            assert n == N_RECORDS  # exactly one event per Record
            assert loader.run_batch(conn, 100, drop_raw=False) == (
                N_RECORDS, N_RECORDS, 0,
            )
    finally:
        subprocess.run(["docker", "rm", "-f", vec_name], capture_output=True, check=False)
        subprocess.run(["docker", "rm", "-f", ls_name], capture_output=True, check=False)
        subprocess.run(["docker", "network", "rm", net_name], capture_output=True, check=False)
