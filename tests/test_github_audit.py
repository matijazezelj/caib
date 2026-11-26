import importlib.util
import json
import pathlib

import httpx
import psycopg
import pytest
from caib_core import detect, loader
from caib_core.detect import Cfg
from caib_pollers import framework
from caib_pollers.modules.github_audit import POLLER, envelope
from psycopg.types.json import Jsonb

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "modules" / "github_audit" / "fixtures"
RULES_DIR = ROOT / "detect" / "rules" / "github_audit"
PAGE1 = json.loads((FIXTURES / "audit_log_page1.json").read_text())
PAGE2 = json.loads((FIXTURES / "audit_log_page2.json").read_text())
ALL_RECORDS = PAGE1 + PAGE2
CFG = Cfg(safety_lag_s=0)

AFTER = "MS42OTk1MTc2MjE3MDJlKzEyfFE1YTJCM2M0RDVlNkY3ZzhIOWkwSm8"


@pytest.fixture(autouse=True)
def gh_env(monkeypatch):
    monkeypatch.setenv("CAIB_GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("CAIB_GITHUB_ORG", "example-org")
    monkeypatch.delenv("CAIB_GITHUB_API_URL", raising=False)
    monkeypatch.delenv("CAIB_GITHUB_AUDIT_INCLUDE", raising=False)


def by_action(action: str, records=ALL_RECORDS) -> dict:
    return next(r for r in records if r["action"] == action)


# --- envelope mapping -------------------------------------------------------


def test_envelope_mapping_repo_made_public():
    rec = PAGE1[0]
    env = envelope(rec)
    assert env["event_time"] == "2026-11-25T09:00:01.000Z"  # epoch ms -> RFC3339
    assert env["event_id"] == "Q1a2B3c4D5e6F7g8H9i0Jk"  # _document_id
    assert env["actor"] == "alice"
    assert env["src_ip"] is None  # actor_ip only with IP disclosure enabled
    assert env["action"] == "repo.access"
    assert env["target"] == "example-org/payments-api"
    assert env["outcome"] == "success"
    assert env["severity"] == "info"
    assert env["attrs"] == {
        "org": "example-org",
        "business": "example-ent",
        "repo": "example-org/payments-api",
        "operation_type": "modify",
        "visibility": "public",
        "previous_visibility": "private",
        "public_repo": True,
        "user_agent": "Mozilla/5.0",
        "category": "repo",
        "actor_country": "HR",
    }
    assert env["raw"] == rec


def test_envelope_mapping_member_role_change_targets_the_user():
    env = envelope(by_action("org.update_member"))
    assert env["target"] == "mallory"
    assert env["attrs"]["permission"] == "admin"
    assert env["attrs"]["old_permission"] == "read"


def test_envelope_mapping_branch_and_token():
    env = envelope(by_action("protected_branch.destroy"))
    assert env["actor"] == "carol"
    assert env["attrs"]["branch"] == "main"
    assert env["attrs"]["programmatic_access_type"] == "Fine-grained personal access token"
    assert env["attrs"]["user_programmatic_access_name"] == "ci-cleanup"


def test_envelope_scopes_list_is_joined_and_non_scalars_dropped():
    env = envelope(by_action("org_credential_authorization.grant"))
    assert env["attrs"]["token_scopes"] == "repo,admin:org"
    erin = envelope(PAGE2[3])
    assert "repositories" not in erin["attrs"]  # list field stays in raw only
    assert erin["attrs"]["repository_selection"] == "selected"


def test_envelope_ip_and_git_event():
    env = envelope({**by_action("git.push"), "actor_ip": "203.0.113.9"})
    assert env["src_ip"] == "203.0.113.9"
    assert env["attrs"]["transport_protocol_name"] == "http"
    assert env["attrs"]["category"] == "git"
    assert "branch" not in env["attrs"]  # only protected_branch.* name is a branch


def test_every_fixture_envelope_passes_the_contract():
    for rec in ALL_RECORDS:
        assert framework.validate_envelope(envelope(rec)) is None, rec["action"]


# --- fetch / pagination -----------------------------------------------------


class LinkCtxStub:
    """Serves page1 with a Link rel="next" until `after` is passed, then
    page2 with no next link — the documented REST pagination shape."""

    def __init__(self, backfill_start):
        self.backfill_start = backfill_start
        self.requests: list[dict] = []

    def get(self, url, *, params=None, headers=None):
        self.requests.append({"url": url, "params": params, "headers": headers})
        request = httpx.Request("GET", url, params=params)
        if "after" not in params:
            next_url = (
                "https://api.github.com/organizations/9001/audit-log"
                f"?phrase=x&order=asc&per_page=100&after={AFTER}&before="
            )
            return httpx.Response(
                200, json=PAGE1, request=request,
                headers={"Link": f'<{next_url}>; rel="next"'},
            )
        return httpx.Response(200, json=PAGE2, request=request)


def test_fetch_pages_through_window(dummy_ctx):
    ctx = LinkCtxStub(dummy_ctx.backfill_start)

    first = POLLER.fetch(None, ctx)
    assert len(first.envelopes) == len(PAGE1)
    assert first.has_more
    assert first.next_cursor["page_cursor"] == AFTER
    req = ctx.requests[0]
    assert req["url"] == "https://api.github.com/orgs/example-org/audit-log"
    p = req["params"]
    before = first.next_cursor["before"]
    assert p["phrase"] == (
        f"created:2026-08-23T00:00:00+00:00..{before.replace('Z', '+00:00')}"
    )
    assert p["order"] == "asc"
    assert p["per_page"] == 100
    assert p["include"] == "web"
    assert "after" not in p
    assert req["headers"]["Authorization"] == "Bearer test-token"
    assert req["headers"]["X-GitHub-Api-Version"] == "2022-11-28"
    # '+00:00' must reach GitHub as %2B, not as a '+' that decodes to a space.
    wire = httpx.Request("GET", req["url"], params=p).url.query.decode()
    assert "%2B00%3A00.." in wire and "+" not in wire

    second = POLLER.fetch(first.next_cursor, ctx)
    assert len(second.envelopes) == len(PAGE2)
    assert not second.has_more
    assert second.next_cursor == {"since": before}  # window sealed
    assert ctx.requests[1]["params"]["after"] == AFTER
    assert ctx.requests[1]["params"]["phrase"] == p["phrase"]  # same window


def test_fetch_waits_when_window_not_open(dummy_ctx):
    ctx = LinkCtxStub(dummy_ctx.backfill_start)
    far_future = {"since": "2999-01-01T00:00:00Z"}
    result = POLLER.fetch(far_future, ctx)
    assert result.envelopes == [] and not result.has_more
    assert result.next_cursor == far_future
    assert ctx.requests == []


def test_fetch_include_and_api_url_from_env(dummy_ctx, monkeypatch):
    monkeypatch.setenv("CAIB_GITHUB_AUDIT_INCLUDE", "all")
    monkeypatch.setenv("CAIB_GITHUB_API_URL", "https://api.example.ghe.com/")
    ctx = LinkCtxStub(dummy_ctx.backfill_start)
    POLLER.fetch(None, ctx)
    assert ctx.requests[0]["params"]["include"] == "all"
    assert ctx.requests[0]["url"] == "https://api.example.ghe.com/orgs/example-org/audit-log"


def test_fetch_rejects_unknown_include(dummy_ctx, monkeypatch):
    monkeypatch.setenv("CAIB_GITHUB_AUDIT_INCLUDE", "everything")
    with pytest.raises(ValueError, match="CAIB_GITHUB_AUDIT_INCLUDE"):
        POLLER.fetch(None, LinkCtxStub(dummy_ctx.backfill_start))


def test_end_to_end_poll_load_dedup(migrated_db, dummy_ctx):
    ctx = LinkCtxStub(dummy_ctx.backfill_start)
    n = len(ALL_RECORDS)

    class FixturePoller:
        module = "github_audit"
        interval_s = 300

        def fetch(self, cursor, _ctx):
            return POLLER.fetch(cursor, ctx)

    poller = FixturePoller()
    with psycopg.connect(migrated_db) as conn:
        for _ in range(2):  # page 1 (has_more) + page 2
            framework.run_cycle(conn, poller, dummy_ctx)
        assert loader.run_batch(conn, 100, drop_raw=False) == (n, n, 0)

        # Re-poll the same fixtures: everything dedups on _document_id.
        conn.execute("DELETE FROM ingest.cursors")
        conn.commit()
        for _ in range(2):
            framework.run_cycle(conn, poller, dummy_ctx)
        assert loader.run_batch(conn, 100, drop_raw=False) == (n, 0, 0)
        assert conn.execute("SELECT count(*) FROM caib.events").fetchone()[0] == n
        row = conn.execute(
            "SELECT event_time, outcome FROM caib.events WHERE action = 'repo.access'"
            " AND attrs->>'visibility' = 'public'"
        ).fetchone()
        assert row[0].isoformat() == "2026-11-25T09:00:01+00:00"
        assert row[1] == "success"


# --- rules ------------------------------------------------------------------

BENIGN_ACTIONS_OR_IDS = {
    "Q1a2B3c4D5e6F7g8H9i0Jl",  # repo made private
    "Q1a2B3c4D5e6F7g8H9i0Jn",  # owner demoted to member
    "Q1a2B3c4D5e6F7g8H9i0Jp",  # private repo created
    "R2b3C4d5E6f7G8h9I0j1Kd",  # PAT granted to selected repos
    "R2b3C4d5E6f7G8h9I0j1Kg",  # git.push
}


def land(conn, records):
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO ingest.landing (received_at, module, payload)"
            " VALUES (now(), 'github_audit', %s)",
            [(Jsonb(envelope(r)),) for r in records],
        )
    conn.commit()
    loader.run_batch(conn, 1000, drop_raw=False)


def fire_all(conn) -> dict[str, list[dict]]:
    """Evaluate every github_audit rule once; return entity lists per rule."""
    rules = detect.load_rules(RULES_DIR)
    detect.validate_rules(conn, rules)
    for rule in rules:
        detect.evaluate_rule(conn, rule, CFG)
        conn.commit()
    out: dict[str, list[dict]] = {r.id: [] for r in rules}
    for rule_id, entity in conn.execute(
        "SELECT rule_id, entity FROM detect.alerts ORDER BY rule_id, dedup_key"
    ):
        out[rule_id].append(entity)
    return out


def test_rules_load_and_every_module_rule_is_present():
    rules = detect.load_rules(RULES_DIR)
    ids = {r.id for r in rules}
    assert ids == {
        "github_audit.dead_man",
        "github_audit.repo_made_public",
        "github_audit.org_owner_added",
        "github_audit.branch_protection_removed",
        "github_audit.security_control_disabled",
        "github_audit.broad_access_granted",
    }
    assert all(r.module == "github_audit" for r in rules)


def test_rules_fire_on_fixtures_and_skip_benign_twins(migrated_db):
    with psycopg.connect(migrated_db) as conn:
        land(conn, ALL_RECORDS)
        alerts = fire_all(conn)

    assert alerts["github_audit.dead_man"] == []  # events just arrived
    assert alerts["github_audit.repo_made_public"] == [
        {"repo": "example-org/payments-api"}
    ]  # not old-website (made private)
    assert alerts["github_audit.org_owner_added"] == [
        {"org": "example-org", "new_owner": "mallory"}
    ]  # not bob (demoted)
    assert alerts["github_audit.branch_protection_removed"] == [
        {"repo": "example-org/payments-api", "actor": "carol"}
    ]
    assert sorted(e["action"] for e in alerts["github_audit.security_control_disabled"]) == [
        "org.disable_two_factor_requirement",
        "repository_secret_scanning.disable",
    ]
    broad = alerts["github_audit.broad_access_granted"]
    assert sorted((e["action"], e["credential"]) for e in broad) == [
        ("integration_installation.create", "acme-deploy-bot"),
        ("org_credential_authorization.grant", "dave"),
        ("personal_access_token.access_granted", "dave-laptop"),
    ]  # not erin-docs-bot (selected repos)


def test_benign_fixtures_fire_nothing(migrated_db):
    benign = [r for r in ALL_RECORDS if r["_document_id"] in BENIGN_ACTIONS_OR_IDS]
    assert len(benign) == len(BENIGN_ACTIONS_OR_IDS)
    with psycopg.connect(migrated_db) as conn:
        land(conn, benign)
        alerts = fire_all(conn)
    assert {k: v for k, v in alerts.items() if v} == {}


def test_two_actors_on_one_repo_fold_into_one_alert(migrated_db):
    second = {**PAGE1[0], "_document_id": "second-actor", "actor": "mallory"}
    with psycopg.connect(migrated_db) as conn:
        land(conn, [PAGE1[0], second])
        fire_all(conn)
        row = conn.execute(
            "SELECT entity, context, event_count FROM detect.alerts"
            " WHERE rule_id = 'github_audit.repo_made_public'"
        ).fetchall()
    assert len(row) == 1
    entity, context, count = row[0]
    assert entity == {"repo": "example-org/payments-api"}
    assert sorted(context["actors"]) == ["alice", "mallory"]
    assert count == 2


def test_narrow_classic_token_grant_does_not_fire(migrated_db):
    narrow = {
        **by_action("org_credential_authorization.grant"),
        "_document_id": "narrow-scope-grant",
        "managed_token_scopes": ["read:org", "read:packages"],
    }
    with psycopg.connect(migrated_db) as conn:
        land(conn, [narrow])
        alerts = fire_all(conn)
    assert alerts["github_audit.broad_access_granted"] == []


def test_dead_man_fires_when_module_is_silent(migrated_db):
    with psycopg.connect(migrated_db) as conn:
        alerts = fire_all(conn)
    assert alerts["github_audit.dead_man"] == [{"module": "github_audit"}]


# --- make demo seeding path (compose is not run here) -----------------------


def test_demo_seed_seeds_github_audit_and_fires_its_rules(migrated_db, monkeypatch):
    """Runs scripts/demo_seed.py's main() against the throwaway DB: our
    envelope() path must land without dead letters and fire our rules."""
    monkeypatch.setenv("CAIB_PG_URL", migrated_db)
    monkeypatch.delenv("CAIB_NOTIFY_WEBHOOK_URL", raising=False)
    spec = importlib.util.spec_from_file_location("demo_seed", ROOT / "scripts" / "demo_seed.py")
    demo_seed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo_seed)
    demo_seed.main()

    with psycopg.connect(migrated_db) as conn:
        n = conn.execute(
            "SELECT count(*) FROM caib.events WHERE module = 'github_audit'"
        ).fetchone()[0]
        assert n == len(ALL_RECORDS) * demo_seed.CLONES_PER_RECORD
        assert conn.execute("SELECT count(*) FROM ingest.dead_letter").fetchone()[0] == 0
        fired = {
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT rule_id FROM detect.alerts WHERE rule_id LIKE 'github_audit.%'"
            )
        }
    assert fired == {
        "github_audit.repo_made_public",
        "github_audit.org_owner_added",
        "github_audit.branch_protection_removed",
        "github_audit.security_control_disabled",
        "github_audit.broad_access_granted",
    }
