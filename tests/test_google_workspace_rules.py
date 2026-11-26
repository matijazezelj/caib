"""google_workspace detection rules against the (SYNTHETIC) fixture events:
each rule fires exactly on the events it is meant for, and no rule fires on
the benign remainder."""

import datetime
import json
import pathlib

import psycopg
import pytest
from caib_core import detect, loader
from caib_core.detect import Cfg
from caib_pollers.modules.google_workspace import envelopes
from psycopg.types.json import Jsonb

ROOT = pathlib.Path(__file__).resolve().parent.parent
RULES_DIR = ROOT / "detect" / "rules" / "google_workspace"
FIXTURES = ROOT / "modules" / "google_workspace" / "fixtures"
CFG = Cfg(safety_lag_s=0)  # events are ingested "now"; no lag

# rule -> exact dedup keys it must produce on the full fixture set.
EXPECTED = {
    "admin_privilege_granted": {
        "admin@example.com|mallory@example.com",  # GRANT_ADMIN_PRIVILEGE
        "admin@example.com|frank@example.com",    # ASSIGN_ROLE
    },
    "domain_wide_delegation_granted": {"admin@example.com|109876543210987654321"},
    "two_step_verification_disabled": {
        "admin@example.com|ENFORCE_STRONG_AUTHENTICATION|/Sales",  # not /Engineering (true)
    },
    "login_risk_signal": {
        "carol@example.com|suspicious_login",
        "dave@example.com|gov_attack_warning",
    },
    "email_forwarding_out_of_domain": {"erin@example.com|drop@exfil.example.net"},
    "drive_file_made_public": {
        "erin@example.com|1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789synth",  # not alice's -> private
    },
}
DETECTIONS = sorted(EXPECTED)


def fixture_envelopes() -> list[dict]:
    """Every fixture event, re-timed to a few minutes ago."""
    ts = (datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=5)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    out = []
    for path in sorted(FIXTURES.glob("*.json")):
        for activity in json.loads(path.read_text())["items"]:
            for env in envelopes(activity):
                out.append(dict(env, event_time=ts))
    return out


def is_trigger(env: dict) -> bool:
    """The fixture events some rule is expected to fire on."""
    a, act = env["attrs"], env["action"]
    return (
        (a["application"] == "admin" and act in (
            "GRANT_ADMIN_PRIVILEGE", "ASSIGN_ROLE", "AUTHORIZE_API_CLIENT_ACCESS"))
        or (act == "ENFORCE_STRONG_AUTHENTICATION" and a.get("new_value") == "false")
        or (a["application"] == "login" and act in (
            "suspicious_login", "gov_attack_warning", "email_forwarding_out_of_domain"))
        or (act == "change_document_visibility" and a.get("new_value") == "people_with_link")
    )


def land_and_load(conn, envs):
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO ingest.landing (received_at, module, payload)"
            " VALUES (now(), 'google_workspace', %s)",
            [(Jsonb(e),) for e in envs],
        )
    conn.commit()
    moved, _, dead = loader.run_batch(conn, 1000, drop_raw=False)
    assert (moved, dead) == (len(envs), 0)


def fired(conn, name: str) -> set[str]:
    rule = detect.parse_rule(RULES_DIR / f"{name}.yaml")
    detect.evaluate_rule(conn, rule, CFG)
    conn.commit()
    return {
        r[0] for r in conn.execute(
            "SELECT dedup_key FROM detect.alerts WHERE rule_id = %s", (rule.id,)
        )
    }


def test_rule_files_cover_the_expected_set():
    names = {p.stem for p in RULES_DIR.glob("*.yaml")}
    assert names == set(EXPECTED) | {"dead_man"}
    for path in RULES_DIR.glob("*.yaml"):
        rule = detect.parse_rule(path)
        assert rule.module == "google_workspace"
        assert rule.id == f"google_workspace.{path.stem}"


def test_rules_validate_against_the_schema(migrated_db):
    rules = [detect.parse_rule(p) for p in sorted(RULES_DIR.glob("*.yaml"))]
    with psycopg.connect(migrated_db) as conn:
        detect.validate_rules(conn, rules)  # EXPLAINs every rule


@pytest.mark.parametrize("name", DETECTIONS)
def test_rule_fires_exactly_on_its_fixture_events(migrated_db, name):
    with psycopg.connect(migrated_db) as conn:
        land_and_load(conn, fixture_envelopes())
        assert fired(conn, name) == EXPECTED[name]


def test_no_rule_fires_on_benign_fixture_events(migrated_db):
    benign = [e for e in fixture_envelopes() if not is_trigger(e)]
    # The benign set still has near-misses: 2SV enabled, a drive item made
    # private, a SAML login_failure, a login_failure, 2sv_enroll, a download.
    assert len(benign) == 12
    with psycopg.connect(migrated_db) as conn:
        land_and_load(conn, benign)
        for name in DETECTIONS:
            assert fired(conn, name) == set(), name


def test_dead_man_fires_on_silence_only(migrated_db):
    with psycopg.connect(migrated_db) as conn:
        assert fired(conn, "dead_man") == {"google_workspace"}
        conn.execute("DELETE FROM detect.alerts")
        conn.execute("UPDATE detect.rule_state SET watermark = watermark - interval '1 minute'")
        conn.commit()
        land_and_load(conn, fixture_envelopes()[:1])
        assert fired(conn, "dead_man") == set()
