"""The HTTP transport: auth, error shape, and that it is the query core underneath.

The seven endpoints' semantics are tested once, in tests/test_query_core.py.
What is left to prove here is that the transport does not lose or change them,
and that nothing gets past the bearer token.
"""

import pytest
from caib_agentapi.app import create_app
from caib_agentapi.config import Settings
from fastapi.testclient import TestClient

TOKEN = "test-token-not-a-real-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

PROTECTED = (
    "/v1/events",
    "/v1/alerts",
    "/v1/alerts/summary",
    "/v1/actors/top",
    "/v1/actors/alice@example.com",
    "/v1/health/modules",
    "/v1/rules",
)


@pytest.fixture()
def client(seeded_db, agent_ro_url):
    app = create_app(Settings(pg_url=agent_ro_url, token=TOKEN))
    with TestClient(app) as client:
        yield client


# --- auth ---------------------------------------------------------------


@pytest.mark.parametrize("path", PROTECTED)
def test_no_token_is_401(client, path):
    response = client.get(path)
    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized", "detail": "bearer token required"}
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "header",
    (
        "Bearer wrong-token",
        f"Bearer {TOKEN} ",
        f"Bearer {TOKEN}x",
        f"Basic {TOKEN}",
        TOKEN,
        "",
    ),
)
def test_a_wrong_token_or_scheme_is_401(client, header):
    assert client.get("/v1/events", headers={"Authorization": header}).status_code == 401


@pytest.mark.parametrize("path", PROTECTED)
def test_the_right_token_is_200(client, path):
    assert client.get(path, headers=AUTH).status_code == 200


def test_the_probes_need_no_token(client):
    """The compose healthcheck calls /healthz; making it carry the token would
    put the token in `docker inspect` output."""
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ready"}


def test_the_service_refuses_to_start_without_a_token(monkeypatch):
    """An unauthenticated listener here is every audit event in the deployment.
    Forgetting the token has to mean "it did not start"."""
    monkeypatch.setenv("CAIB_RO_PG_URL", "postgresql://caib_agent_ro@localhost/caib")

    monkeypatch.setenv("CAIB_AGENT_API_TOKEN", "")
    with pytest.raises(SystemExit, match="CAIB_AGENT_API_TOKEN"):
        Settings.from_env(require_token=True)

    monkeypatch.delenv("CAIB_AGENT_API_TOKEN")
    with pytest.raises(SystemExit, match="CAIB_AGENT_API_TOKEN"):
        Settings.from_env(require_token=True)

    monkeypatch.setenv("CAIB_AGENT_API_TOKEN", "a-token")
    assert Settings.from_env(require_token=True).token == "a-token"


def test_the_service_refuses_to_start_without_a_database_url(monkeypatch):
    monkeypatch.delenv("CAIB_RO_PG_URL", raising=False)
    monkeypatch.setenv("CAIB_AGENT_API_TOKEN", "a-token")
    with pytest.raises(SystemExit, match="CAIB_RO_PG_URL"):
        Settings.from_env(require_token=True)


def test_it_binds_loopback_unless_told_otherwise(monkeypatch):
    monkeypatch.setenv("CAIB_RO_PG_URL", "postgresql://caib_agent_ro@localhost/caib")
    monkeypatch.setenv("CAIB_AGENT_API_TOKEN", "a-token")
    monkeypatch.delenv("CAIB_AGENT_API_BIND", raising=False)
    assert Settings.from_env(require_token=True).bind == "127.0.0.1"

    monkeypatch.setenv("CAIB_AGENT_API_BIND", "0.0.0.0")
    assert Settings.from_env(require_token=True).bind == "0.0.0.0"


# --- the endpoints carry the core's behaviour through -------------------


def test_events_defaults_and_filters(client):
    body = client.get("/v1/events", headers=AUTH).json()
    assert body["count"] == 7
    assert body["include_raw"] is False
    assert "raw" not in body["events"][0]

    filtered = client.get("/v1/events?module=gcp_audit&outcome=success", headers=AUTH).json()
    assert filtered["count"] == 1


def test_raw_stays_opt_in_over_http(client):
    body = client.get("/v1/events?module=gcp_audit&include_raw=true", headers=AUTH).json()
    assert body["include_raw"] is True
    assert "protoPayload" in body["events"][0]["raw"]


def test_the_limit_cap_is_not_negotiable_over_http(client):
    body = client.get("/v1/events?limit=100000", headers=AUTH).json()
    assert body["limit"] == 500


def test_alerts_and_summary(client):
    assert client.get("/v1/alerts?status=open", headers=AUTH).json()["count"] == 1
    summary = client.get("/v1/alerts/summary", headers=AUTH).json()
    assert summary["total"] == 3
    assert summary["by_status"] == {"open": 1, "ack": 1, "closed": 1}


def test_top_actors_is_not_shadowed_by_the_actor_path_parameter(client):
    """/v1/actors/top and /v1/actors/{actor} share a prefix; declaration order
    is what keeps them apart."""
    top = client.get("/v1/actors/top", headers=AUTH).json()
    assert "actors" in top and "by_action" not in top

    one = client.get("/v1/actors/alice@example.com", headers=AUTH).json()
    assert one["actor"] == "alice@example.com"
    assert {r["action"] for r in one["by_action"]} == {"GetObject"}


ARN = "arn:aws:iam::123456789012:user/carol"


@pytest.fixture()
def arn_client(seeded_db, agent_ro_url):
    """The seeded app plus one event from an IAM ARN actor — CloudTrail's usual
    actor shape, and one with a `/` in it."""
    import datetime

    import psycopg
    from caib_core import loader
    from psycopg.types.json import Jsonb

    with psycopg.connect(seeded_db) as conn:
        conn.execute(
            "INSERT INTO ingest.landing (received_at, module, payload) VALUES (now(), %s, %s)",
            ("aws_cloudtrail", Jsonb({
                "event_time": datetime.datetime.now(datetime.UTC).isoformat(),
                "event_id": "arn-actor", "actor": ARN, "action": "StopLogging",
                "outcome": "success", "severity": "high", "attrs": {},
            })),
        )
        conn.commit()
        loader.run_batch(conn, 10, drop_raw=False)
    app = create_app(Settings(pg_url=agent_ro_url, token=TOKEN))
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize("path", (f"/v1/actors/{ARN}", "/v1/actors/" + ARN.replace("/", "%2F")))
def test_actor_activity_reaches_actors_with_a_slash(arn_client, path):
    """A plain {actor} path segment 404'd for every IAM ARN, encoded or not."""
    response = arn_client.get(path, headers=AUTH)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["actor"] == ARN
    assert [r["action"] for r in body["by_action"]] == ["StopLogging"]


def test_open_alerts_do_not_age_out_over_http(client, seeded_db):
    import psycopg

    with psycopg.connect(seeded_db) as conn:
        conn.execute(
            "INSERT INTO detect.alerts (rule_id, dedup_key, severity, title, first_seen,"
            " last_seen) VALUES ('aws_cloudtrail.root_console_login', 'old', 'high',"
            " 'Root login', now() - interval '120 days', now() - interval '120 days')"
        )
        conn.commit()
    assert client.get("/v1/alerts?status=open", headers=AUTH).json()["count"] == 2
    assert client.get("/v1/alerts/summary", headers=AUTH).json()["by_status"]["open"] == 2


def test_a_foreign_origin_is_refused_before_auth(agent_ro_url, seeded_db):
    settings = Settings(pg_url=agent_ro_url, token=TOKEN,
                        allowed_origins=frozenset({"http://localhost:6274"}))
    with TestClient(create_app(settings)) as c:
        evil = c.get("/v1/rules", headers={**AUTH, "Origin": "https://evil.example"})
        assert evil.status_code == 403
        assert evil.json()["error"] == "forbidden_origin"
        assert c.get("/v1/rules", headers={**AUTH, "Origin": "http://localhost:6274"}
                     ).status_code == 200
        assert c.get("/v1/rules", headers=AUTH).status_code == 200  # no Origin: CLI


def test_module_health_and_rules(client):
    health = client.get("/v1/health/modules", headers=AUTH).json()
    assert {m["module"] for m in health["modules"]} == {"aws_cloudtrail", "gcp_audit"}
    assert {s["service"] for s in health["services"]} == {"loader", "detect"}

    rules = client.get("/v1/rules", headers=AUTH).json()
    assert rules["count"] > 0


# --- errors -------------------------------------------------------------


def test_a_rejected_filter_is_a_400_in_the_api_error_shape(client):
    response = client.get("/v1/events?severity=warn", headers=AUTH)
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "bad_request"
    assert "info|low|medium|high|critical" in body["detail"]


def test_an_unparseable_parameter_is_a_400_not_fastapis_422(client):
    response = client.get("/v1/events?limit=twelve", headers=AUTH)
    assert response.status_code == 400
    assert response.json()["error"] == "bad_request"


def test_errors_never_carry_a_traceback(client):
    for path in ("/v1/events?severity=warn", "/v1/events?limit=0", "/v1/events"):
        response = client.get(path, headers=AUTH)
        assert "Traceback" not in response.text
        assert "caib_core" not in response.text


def test_readyz_reports_a_dead_database_without_leaking_why(agent_ro_url):
    app = create_app(Settings(pg_url="postgresql://nobody@127.0.0.1:1/nothing", token=TOKEN))
    with TestClient(app) as client:
        response = client.get("/readyz")
        assert response.status_code == 503
        assert response.json()["error"] == "database_unavailable"
        assert "nobody" not in response.text
