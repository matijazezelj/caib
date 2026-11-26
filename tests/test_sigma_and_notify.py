"""Phase 8: Sigma->SQL converter (existence shape) and Slack Block Kit
notification formatting."""

import json

import psycopg
import pytest
import yaml
from caib_core import detect, loader, notify
from caib_core.sigma import UnsupportedSigma, convert, to_yaml
from psycopg.types.json import Jsonb

SIGMA_ROOT_LOGIN = yaml.safe_load("""
title: AWS Root Credentials Console Login
id: 00000000-0000-4000-8000-000000000001
status: stable
level: high
logsource: {product: aws, service: cloudtrail}
detection:
  selection:
    eventSource: signin.amazonaws.com
    eventName: ConsoleLogin
    userIdentity.type: Root
  filter:
    errorCode|contains: Failure
  condition: selection and not filter
""")


def test_convert_produces_valid_rule_that_fires(migrated_db, tmp_path):
    rule_yaml = to_yaml(convert(SIGMA_ROOT_LOGIN, "aws_cloudtrail"))
    assert "# Converted from Sigma rule 00000000-0000-4000-8000-000000000001" in rule_yaml
    path = tmp_path / "sigma_root.yaml"
    path.write_text(rule_yaml)

    rule = detect.parse_rule(path)
    assert rule.severity == "high"
    assert rule.id == "aws_cloudtrail.sigma_aws_root_credentials_console_login"

    with psycopg.connect(migrated_db) as conn:
        detect.validate_rules(conn, [rule])  # EXPLAIN passes
        payload = {
            "event_time": "2026-08-24T10:00:00Z",
            "event_id": "sigma-e2e-1",
            "actor": "arn:aws:iam::123456789012:root",
            "action": "ConsoleLogin",
            "outcome": "success",
            "severity": "info",
            "attrs": {"user_type": "Root", "event_source": "signin.amazonaws.com"},
        }
        conn.execute(
            "INSERT INTO ingest.landing (received_at, module, payload)"
            " VALUES (now(), 'aws_cloudtrail', %s)",
            (Jsonb(payload),),
        )
        conn.commit()
        loader.run_batch(conn, 100, drop_raw=False)
        stats = detect.evaluate_rule(conn, rule, detect.Cfg(safety_lag_s=0))
        conn.commit()
        assert stats["rows"] == 1


def test_convert_handles_lists_wildcards_and_modifiers():
    sigma = yaml.safe_load("""
title: Trail tampering
level: critical
detection:
  selection:
    eventName:
      - StopLogging
      - DeleteTrail
    eventSource|endswith: .amazonaws.com
    userIdentity.arn: 'arn:aws:iam::*:user/*'
  condition: selection
""")
    rule = convert(sigma, "aws_cloudtrail")
    sql = rule["sql"]
    assert "(action = 'StopLogging' OR action = 'DeleteTrail')" in sql
    assert "attrs->>'event_source' LIKE '%%.amazonaws.com'" in sql
    assert "actor LIKE 'arn:aws:iam::%%:user/%%'" in sql


@pytest.mark.parametrize(
    "condition", ["selection or filter", "1 of selection*", "all of them"]
)
def test_unsupported_conditions_raise(condition):
    sigma = {
        "title": "x",
        "detection": {"selection": {"eventName": "Foo"}, "condition": condition},
    }
    with pytest.raises((UnsupportedSigma, SystemExit)):
        convert(sigma, "aws_cloudtrail")


def test_unmapped_field_raises():
    sigma = {
        "title": "x",
        "detection": {"selection": {"requestParameters.bucketName": "b"}, "condition": "selection"},
    }
    with pytest.raises(UnsupportedSigma, match="no field mapping"):
        convert(sigma, "aws_cloudtrail")


# --- Block Kit ----------------------------------------------------------------


PAYLOAD = {
    "alert_id": 7,
    "rule_id": "aws_cloudtrail.root_console_login",
    "module": "aws_cloudtrail",
    "title": "Root account console login",
    "severity": "high",
    "kind": "new",
    "dedup_key": "arn:aws:iam::123456789012:root|203.0.113.10",
    "entity": {"actor": "arn:aws:iam::123456789012:root"},
    "context": {"events": 3},
    "first_seen": "2026-08-24T10:00:00+00:00",
    "last_seen": "2026-08-24T10:05:00+00:00",
    "event_count": 3,
}


def test_slack_message_structure():
    msg = notify.slack_message(PAYLOAD)
    assert msg["text"] == "[high] Root account console login"
    types = [b["type"] for b in msg["blocks"]]
    assert types == ["header", "section", "section", "context"]
    assert "🟧" in msg["blocks"][0]["text"]["text"]
    fields = " ".join(f["text"] for f in msg["blocks"][1]["fields"])
    assert "*Module:* aws_cloudtrail" in fields
    assert "*Events:* 3" in fields
    detail = msg["blocks"][2]["text"]["text"]
    assert detail.startswith("```") and "arn:aws:iam::123456789012:root" in detail
    assert "kind: new" in msg["blocks"][3]["elements"][0]["text"]


def test_slack_message_truncates_huge_context():
    huge = dict(PAYLOAD, context={"blob": "x" * 10000})
    detail = notify.slack_message(huge)["blocks"][2]["text"]["text"]
    assert len(detail) < 3000


def test_resolve_format():
    assert notify.resolve_format("", "https://hooks.slack.com/services/T/B/x") == "slack"
    assert notify.resolve_format("", "https://example.com/hook") == "json"
    assert notify.resolve_format("json", "https://hooks.slack.com/services/T/B/x") == "json"
    assert notify.resolve_format("slack", "https://example.com/hook") == "slack"
    # Hostname match, not substring: a look-alike URL is not Slack.
    assert notify.resolve_format("", "https://hooks.slack.com.example.com/x") == "json"
    assert notify.resolve_format("", "https://example.com/?u=hooks.slack.com") == "json"


def test_drain_sends_blocks_when_slack_format(migrated_db, webhook_capture):
    url, received = webhook_capture
    with psycopg.connect(migrated_db) as conn:
        conn.execute(
            "INSERT INTO detect.outbox (alert_id, kind, payload) VALUES (1, 'new', %s)",
            (Jsonb(PAYLOAD),),
        )
        conn.commit()
        assert notify.drain(conn, url, fmt="slack") == (1, 0)
        conn.commit()
    assert len(received) == 1
    assert "blocks" in received[0]
    assert received[0]["text"] == "[high] Root account console login"


def test_drain_sends_raw_payload_when_json_format(migrated_db, webhook_capture):
    url, received = webhook_capture
    with psycopg.connect(migrated_db) as conn:
        conn.execute(
            "INSERT INTO detect.outbox (alert_id, kind, payload) VALUES (1, 'new', %s)",
            (Jsonb(PAYLOAD),),
        )
        conn.commit()
        assert notify.drain(conn, url, fmt="json") == (1, 0)
        conn.commit()
    assert received == [PAYLOAD]


def test_payload_roundtrip_is_json_serializable():
    json.dumps(notify.slack_message(PAYLOAD))
