"""gcp_audit module: config validates on the pinned Vector, and the real remap
maps LogEntry fixtures into correct envelopes end-to-end (stdin -> postgres
sink -> loader). The pre-parse transform stands in for the gcp_pubsub source's
decoding.codec json, which parses each message into the event root."""

import json
import pathlib
import subprocess

import psycopg
import yaml
from caib_core import loader

ROOT = pathlib.Path(__file__).resolve().parent.parent
VECTOR_IMAGE = "timberio/vector:0.57.0-debian"
FIXTURE = ROOT / "modules" / "gcp_audit" / "fixtures" / "log_entries.json"
MODULE_YAML = ROOT / "vector" / "modules" / "gcp_audit.yaml"

ENTRIES = json.loads(FIXTURE.read_text())


def test_gcp_config_validates(tmp_path):
    (tmp_path / "vector.yaml").write_text((ROOT / "vector" / "vector.yaml").read_text())
    (tmp_path / "gcp_audit.yaml").write_text(MODULE_YAML.read_text())
    proc = subprocess.run(
        [
            "docker", "run", "--rm",
            "-v", f"{tmp_path}:/cfg:ro",
            "-e", "CAIB_PG_URL=postgresql://u:p@localhost:5432/x",
            "-e", "CAIB_GCP_PROJECT=acme-prod",
            "-e", "CAIB_GCP_SUBSCRIPTION=caib-audit",
            VECTOR_IMAGE,
            "validate", "--no-environment", "/cfg/vector.yaml", "/cfg/gcp_audit.yaml",
        ],
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert proc.returncode == 0, f"vector validate failed:\n{proc.stdout}\n{proc.stderr}"


def test_gcp_stdin_e2e(migrated_db, tmp_path):
    remap_vrl = yaml.safe_load(MODULE_YAML.read_text())["transforms"]["remap_gcp_audit"]["source"]
    config = {
        "sources": {"in": {"type": "stdin"}},
        "transforms": {
            "parse_stdin": {
                "type": "remap",
                "inputs": ["in"],
                "source": ". = parse_json!(string!(.message))",
            },
            "remap_gcp_audit": {
                "type": "remap",
                "inputs": ["parse_stdin"],
                "source": remap_vrl,
            },
        },
        "sinks": {
            "pg": {
                "type": "postgres",
                "inputs": ["remap_*"],
                "endpoint": migrated_db.replace("127.0.0.1", "host.docker.internal").replace(
                    "localhost", "host.docker.internal"
                ),
                "table": "ingest.landing",
                "batch": {"max_events": 500, "timeout_secs": 1},
            }
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config))
    stdin = "".join(json.dumps(e) + "\n" for e in ENTRIES).encode()
    proc = subprocess.run(
        [
            "docker", "run", "--rm", "-i",
            "--add-host=host.docker.internal:host-gateway",
            "-v", f"{tmp_path}:/cfg:ro",
            VECTOR_IMAGE, "--config", "/cfg/config.yaml",
        ],
        input=stdin, capture_output=True, timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr.decode()

    with psycopg.connect(migrated_db) as conn:
        rows = conn.execute(
            "SELECT payload->>'action', payload->>'actor', payload->>'target',"
            "       payload->>'outcome', payload->>'severity', payload->'attrs'"
            " FROM ingest.landing ORDER BY payload->>'event_time'"
        ).fetchall()
        assert len(rows) == len(ENTRIES)
        by_action = {r[0]: r for r in rows}

        iam = by_action["SetIamPolicy"]
        assert iam[1] == "alice@example.com"
        assert iam[2] == "projects/acme-prod"
        assert iam[3] == "success"
        assert iam[5]["roles_added"] == "roles/owner"
        assert iam[5]["permission_denied"] is False

        denied = by_action["storage.objects.get"]
        assert denied[3] == "failure"
        assert denied[4] == "low"  # severity ERROR -> low
        assert denied[5]["permission_denied"] is True
        assert denied[5]["service_name"] == "storage.googleapis.com"

        assert loader.run_batch(conn, 100, drop_raw=False) == (3, 3, 0)
        assert conn.execute(
            "SELECT count(*) FROM caib.events WHERE module = 'gcp_audit'"
        ).fetchone()[0] == 3
