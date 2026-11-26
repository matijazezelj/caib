"""google_workspace poller: envelope mapping, service-account auth (pure-Python
RS256, checked against `cryptography`), windowed/overlapping cursor, and the
poll -> load -> re-poll -> dedup path against a real throwaway DB.

Fixtures are SYNTHETIC (built from Google's docs; see API_NOTES.md)."""

import base64
import datetime
import importlib.util
import json
import logging
import pathlib
import subprocess
import sys

import httpx
import psycopg
import pytest
from caib_core import loader
from caib_pollers import framework
from caib_pollers.framework import Ctx, validate_envelope
from caib_pollers.modules import google_workspace as gw
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from psycopg.types.json import Jsonb

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "modules" / "google_workspace" / "fixtures"
UTC = datetime.UTC


def page(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def all_envelopes() -> list[dict]:
    out = []
    for path in sorted(FIXTURES.glob("*.json")):
        for activity in json.loads(path.read_text())["items"]:
            out.extend(gw.envelopes(activity))
    return out


def by_action(action: str, app: str | None = None) -> dict:
    (env,) = [
        e for e in all_envelopes()
        if e["action"] == action and (app is None or e["attrs"]["application"] == app)
    ][:1]
    return env


# --- envelope mapping ---------------------------------------------------------


def test_every_fixture_envelope_is_valid_and_ids_unique():
    envs = all_envelopes()
    assert len(envs) == 20  # 19 activities, one with two events
    assert all(validate_envelope(e) is None for e in envs)
    ids = [e["event_id"] for e in envs]
    assert len(set(ids)) == len(ids)
    for e in envs:  # attrs are flat scalars, never nested
        assert all(not isinstance(v, (dict, list)) for v in e["attrs"].values())


def test_login_success_mapping():
    env = gw.envelope(page("login_page1")["items"][0])
    assert env["event_time"] == "2026-11-25T09:12:03.512Z"
    assert env["event_id"] == "login:2026-11-25T09:12:03.512Z:-4412345678901234567:0"
    assert env["actor"] == "alice@example.com"
    assert env["src_ip"] == "203.0.113.10"
    assert env["action"] == "login_success"
    assert env["target"] is None
    assert env["outcome"] == "success"
    assert env["severity"] == "info"
    assert env["attrs"] == {
        "application": "login",
        "event_type": "login",
        "caller_type": "USER",
        "actor_profile_id": "100000000000000000001",
        "region_code": "HR",
        "login_type": "google_password",
        "login_challenge_method": "password,google_authenticator",
        "is_suspicious": False,
    }
    assert env["raw"]["id"]["uniqueQualifier"] == "-4412345678901234567"


def test_login_failure_and_warning_outcomes():
    fail = gw.envelope(page("login_page1")["items"][1])
    assert fail["outcome"] == "failure"
    assert fail["attrs"]["login_failure_type"] == "login_failure_invalid_password"
    suspicious = gw.envelope(page("login_page1")["items"][2])
    assert suspicious["outcome"] == "unknown"  # a warning, not a pass/fail
    assert suspicious["target"] == "carol@example.com"
    assert "login_timestamp" not in suspicious["attrs"]  # not whitelisted


def test_forwarding_destination_is_the_target():
    env = by_action("email_forwarding_out_of_domain")
    assert env["target"] == "drop@exfil.example.net"
    assert env["attrs"]["forwarding_destination"] == "drop@exfil.example.net"


def test_admin_mapping_and_key_actor():
    grant = by_action("GRANT_ADMIN_PRIVILEGE")
    assert (grant["actor"], grant["target"], grant["outcome"]) == (
        "admin@example.com", "mallory@example.com", "success")
    assert grant["attrs"]["event_type"] == "USER_SETTINGS"
    dwd = by_action("AUTHORIZE_API_CLIENT_ACCESS")
    assert dwd["target"] == "109876543210987654321"
    assert dwd["attrs"]["api_scopes"] == (
        "https://mail.google.com/,https://www.googleapis.com/auth/drive")
    system = by_action("CREATE_USER")
    assert system["actor"] == "SYSTEM"
    assert system["attrs"]["caller_type"] == "KEY"
    assert system["src_ip"] is None


def test_multi_event_activity_fans_out_with_indexed_ids():
    activity = page("drive")["items"][0]
    envs = gw.envelopes(activity)
    assert [e["action"] for e in envs] == [
        "change_document_access_scope", "change_document_visibility"]
    assert [e["event_id"][-2:] for e in envs] == [":0", ":1"]
    assert envs[1]["target"] == "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789synth"
    assert envs[1]["attrs"]["new_value"] == "people_with_link"  # multiValue joined
    assert envs[1]["attrs"]["old_value"] == "private"


def test_token_and_saml_mapping():
    tok = [e for e in all_envelopes() if e["attrs"]["application"] == "token"][1]
    assert tok["target"] == "987654321098-synthetic.apps.googleusercontent.com"
    assert tok["attrs"]["scope"] == (
        "https://mail.google.com/,https://www.googleapis.com/auth/userinfo.email")
    assert tok["attrs"]["app_name"] == "Mail Organizer Pro"
    saml_fail = by_action("login_failure", app="saml")
    assert saml_fail["outcome"] == "failure"
    assert saml_fail["target"] == "Salesforce"
    assert saml_fail["attrs"]["failure_type"] == "failure_app_not_configured_for_user"


def test_event_status_and_epoch_time_variants():
    activity = {
        "id": {"time": "1764063075", "uniqueQualifier": "1", "applicationName": "admin"},
        "actor": {"email": "a@example.com"},
        "events": [{"name": "X", "status": {"eventStatus": "FAILED"}}],
    }
    env = gw.envelope(activity)
    assert env["event_time"] == "2025-11-25T09:31:15Z"
    assert env["outcome"] == "failure"
    # No uniqueQualifier -> loader's canonical hash instead of a guessed id.
    activity["id"].pop("uniqueQualifier")
    assert gw.envelope(activity)["event_id"] is None


# --- auth ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def pem(key, fmt) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM, fmt, serialization.NoEncryption()
    ).decode()


@pytest.mark.parametrize("fmt", [serialization.PrivateFormat.PKCS8,
                                 serialization.PrivateFormat.TraditionalOpenSSL])
def test_rs256_signature_verifies_with_cryptography(rsa_key, fmt):
    msg = b"header.claims"
    sig = gw._rs256_sign(msg, pem(rsa_key, fmt))
    # Raises InvalidSignature if our PKCS#1 v1.5 encoding is wrong.
    rsa_key.public_key().verify(sig, msg, padding.PKCS1v15(), hashes.SHA256())


def test_module_does_not_need_cryptography():
    """The poller image has only httpx + psycopg; the module must not import
    cryptography or jwt (they exist in the dev venv via other packages)."""
    code = (
        "import sys; import caib_pollers.modules.google_workspace; "
        "bad = {'cryptography', 'jwt', 'google'} & set(m.split('.')[0] for m in sys.modules); "
        "assert not bad, bad"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def sa_key(rsa_key) -> dict:
    return {
        "type": "service_account",
        "client_email": "caib-reader@synthetic-project.iam.gserviceaccount.com",
        "private_key_id": "synthetic-kid",
        "private_key": pem(rsa_key, serialization.PrivateFormat.PKCS8),
    }


def decode_jwt(assertion: str, rsa_key) -> dict:
    head, claims, sig = assertion.split(".")
    pad = lambda s: s + "=" * (-len(s) % 4)
    rsa_key.public_key().verify(
        base64.urlsafe_b64decode(pad(sig)), f"{head}.{claims}".encode(),
        padding.PKCS1v15(), hashes.SHA256(),
    )
    header = json.loads(base64.urlsafe_b64decode(pad(head)))
    assert header == {"alg": "RS256", "typ": "JWT", "kid": "synthetic-kid"}
    return json.loads(base64.urlsafe_b64decode(pad(claims)))


def test_token_refresh_signs_a_fresh_assertion(rsa_key, monkeypatch):
    posts = []

    def handler(request):
        posts.append(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json={"access_token": f"tok-{len(posts)}",
                                         "expires_in": 3600})

    clock = [1_800_000_000]
    monkeypatch.setattr(gw.time, "time", lambda: clock[0])
    auth = gw.ServiceAccountToken(sa_key(rsa_key), "admin@example.com")
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        assert auth.token(http) == "tok-1"
        assert auth.token(http) == "tok-1"  # cached
        auth._expires_at = framework.time.monotonic() + 100  # inside early refresh
        clock[0] += 3500
        assert auth.token(http) == "tok-2"

    assert len(posts) == 2
    assert {p["grant_type"] for p in posts} == {gw.JWT_GRANT}
    first, second = (decode_jwt(p["assertion"], rsa_key) for p in posts)
    assert first == {
        "iss": "caib-reader@synthetic-project.iam.gserviceaccount.com",
        "sub": "admin@example.com",
        "scope": "https://www.googleapis.com/auth/admin.reports.audit.readonly",
        "aud": "https://oauth2.googleapis.com/token",
        "iat": 1_800_000_000,
        "exp": 1_800_003_600,
    }
    assert second["iat"] == 1_800_003_500  # re-signed, not replayed


def test_token_endpoint_rejection_raises(rsa_key):
    def handler(request):
        return httpx.Response(401, json={"error": "unauthorized_client"})

    auth = gw.ServiceAccountToken(sa_key(rsa_key), "admin@example.com")
    with (httpx.Client(transport=httpx.MockTransport(handler)) as http,
          pytest.raises(httpx.HTTPStatusError)):
        auth.token(http)


def test_credentials_errors_are_clear_and_leak_nothing(rsa_key, monkeypatch):
    monkeypatch.delenv("CAIB_GOOGLE_WORKSPACE_SA_KEY_B64", raising=False)
    with pytest.raises(RuntimeError, match="is not set"):
        gw.load_service_account()

    broken = dict(sa_key(rsa_key), private_key="-----BEGIN PRIVATE KEY-----\nAAAA\n")
    monkeypatch.setenv("CAIB_GOOGLE_WORKSPACE_SA_KEY_B64",
                       base64.b64encode(json.dumps(broken).encode()).decode())
    with pytest.raises(RuntimeError) as err:
        gw.load_service_account()
    assert "client_email/private_key" in str(err.value)
    assert "AAAA" not in str(err.value) and err.value.__cause__ is None

    good = base64.b64encode(json.dumps(sa_key(rsa_key)).encode()).decode()
    monkeypatch.setenv("CAIB_GOOGLE_WORKSPACE_SA_KEY_B64", good)
    assert gw.load_service_account()["client_email"].startswith("caib-reader@")

    poller = gw.GoogleWorkspacePoller()
    monkeypatch.delenv("CAIB_GOOGLE_WORKSPACE_ADMIN_EMAIL", raising=False)
    with pytest.raises(RuntimeError, match="ADMIN_EMAIL"):
        poller.fetch(None, None)


# --- cursoring ----------------------------------------------------------------


class Resp:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class ReportsStub:
    """Serves fixture pages per application; login has two pages. Activity
    times are moved to just before now (stable per activity), as the real API
    only returns events inside the requested window."""

    def __init__(self, backfill_start, fail=None):
        self.backfill_start = backfill_start
        self.base = datetime.datetime.now(UTC) - datetime.timedelta(minutes=15)
        self.log = logging.getLogger("test")
        self.requests: list[tuple[str, dict]] = []
        self.fail = fail  # (app, status) to raise on a pageToken request

    def get(self, url, *, params=None, headers=None):
        app = url.rsplit("/", 1)[1]
        self.requests.append((app, dict(params)))
        if self.fail and self.fail[0] == app and "pageToken" in params:
            req = httpx.Request("GET", url)
            raise httpx.HTTPStatusError(
                "boom", request=req, response=httpx.Response(self.fail[1], request=req))
        if app == "login":
            name = "login_page2" if "pageToken" in params else "login_page1"
        else:
            name = {"token": "token_authorize"}.get(app, app)
        body = page(name)
        for activity in body["items"]:
            offset = int(activity["id"]["uniqueQualifier"]) % 600
            ts = self.base + datetime.timedelta(seconds=offset)
            activity["id"]["time"] = ts.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        return Resp(body)


@pytest.fixture()
def poller(monkeypatch):
    monkeypatch.setattr(gw.GoogleWorkspacePoller, "_auth_headers",
                        lambda self, ctx: {"Authorization": "Bearer t"})
    monkeypatch.delenv("CAIB_GOOGLE_WORKSPACE_STREAMS", raising=False)
    monkeypatch.delenv("CAIB_GOOGLE_WORKSPACE_CUSTOMER_ID", raising=False)
    return gw.GoogleWorkspacePoller()  # fresh: the seen-set is per instance


def recent_start(hours=1) -> datetime.datetime:
    return (datetime.datetime.now(UTC) - datetime.timedelta(hours=hours)).replace(microsecond=0)


def test_first_fetch_opens_one_window_per_stream(poller):
    start = recent_start()
    ctx = ReportsStub(start)
    result = poller.fetch(None, ctx)

    assert [app for app, _ in ctx.requests] == ["login", "admin", "drive", "token", "saml"]
    params = dict(ctx.requests)
    assert params["token"]["eventName"] == "authorize"
    assert "eventName" not in params["login"]
    assert params["login"]["maxResults"] == 1000
    assert params["login"]["startTime"] == gw._fmt(start - gw.DEFAULT_OVERLAP)
    assert params["token"]["startTime"] == gw._fmt(start - datetime.timedelta(hours=6))
    assert params["admin"]["endTime"] <= gw._fmt(datetime.datetime.now(UTC) - gw.WINDOW_LAG)
    # login has a next page; everything else sealed at its endTime.
    assert result.has_more
    streams = result.next_cursor["streams"]
    assert streams["login"]["page_token"] == "synthetic-page-token-login-2"
    assert streams["admin"] == {"since": params["admin"]["endTime"]}
    assert len(result.envelopes) == 3 + 6 + 4 + 2 + 2


def test_idle_streams_make_no_request_while_one_pages(poller):
    ctx = ReportsStub(recent_start())
    first = poller.fetch(None, ctx)
    ctx.requests.clear()

    second = poller.fetch(first.next_cursor, ctx)
    assert ctx.requests == [("login", ctx.requests[0][1])]  # only the paging stream
    assert ctx.requests[0][1]["pageToken"] == "synthetic-page-token-login-2"
    assert ctx.requests[0][1]["startTime"] == first.next_cursor["streams"]["login"]["start"]
    assert len(second.envelopes) == 3
    assert not second.has_more
    assert second.next_cursor["streams"]["login"] == {
        "since": first.next_cursor["streams"]["login"]["end"]}

    ctx.requests.clear()
    third = poller.fetch(second.next_cursor, ctx)  # nothing due yet
    assert ctx.requests == [] and third.envelopes == [] and not third.has_more


def test_next_window_overlaps_the_previous_one(poller):
    since = recent_start(hours=2)
    cursor = {"streams": {s: {"since": gw._fmt(since)} for s in
                          ("login", "admin", "drive", "token:authorize", "saml")}}
    ctx = ReportsStub(since)
    poller.fetch(cursor, ctx)
    params = dict(ctx.requests)
    assert params["admin"]["startTime"] == gw._fmt(since - gw.DEFAULT_OVERLAP)
    assert params["token"]["startTime"] == gw._fmt(since - gw.OVERLAP["token"])


def test_backfill_is_capped_windows_that_keep_looping(poller, monkeypatch):
    monkeypatch.setenv("CAIB_GOOGLE_WORKSPACE_STREAMS", "admin")
    start = recent_start(hours=24 * 3)
    ctx = ReportsStub(start)
    result = poller.fetch(None, ctx)
    (_, params), = ctx.requests
    assert params["endTime"] == gw._fmt(start + gw.MAX_WINDOW)
    assert result.next_cursor == {"streams": {"admin": {"since": params["endTime"]}}}
    assert result.has_more  # still two days behind: loop immediately


def test_since_older_than_retention_is_clamped(poller, monkeypatch):
    monkeypatch.setenv("CAIB_GOOGLE_WORKSPACE_STREAMS", "admin")
    ctx = ReportsStub(recent_start(hours=24 * 400))
    poller.fetch(None, ctx)
    (_, params), = ctx.requests
    oldest = datetime.datetime.now(UTC) - datetime.timedelta(days=180)
    assert gw._parse(params["startTime"]) > oldest
    assert params["startTime"] < params["endTime"]


def test_rejected_page_token_restarts_the_window(poller):
    ctx = ReportsStub(recent_start(), fail=("login", 400))
    first = poller.fetch(None, ctx)
    second = poller.fetch(first.next_cursor, ctx)
    login_since = first.next_cursor["streams"]["login"]["since"]
    assert second.next_cursor["streams"]["login"] == {"since": login_since}
    assert second.has_more  # due again -> reopens immediately


def test_auth_error_on_page_token_still_crashes(poller):
    ctx = ReportsStub(recent_start(), fail=("login", 403))
    first = poller.fetch(None, ctx)
    with pytest.raises(httpx.HTTPStatusError):
        poller.fetch(first.next_cursor, ctx)


def test_overlap_rereads_are_suppressed_by_the_seen_set(poller):
    ctx = ReportsStub(recent_start())
    first = poller.fetch(None, ctx)
    second = poller.fetch(first.next_cursor, ctx)
    assert len(first.envelopes) + len(second.envelopes) == 20
    # Force every stream due again: the stub returns the same events.
    old = {s: {"since": gw._fmt(recent_start(hours=2))}
           for s in second.next_cursor["streams"]}
    again = poller.fetch({"streams": old}, ctx)
    assert again.envelopes == []
    # A fresh process has no memory: it re-lands, and the loader dedups.
    assert len(gw.GoogleWorkspacePoller().fetch({"streams": old}, ctx).envelopes) == 17


def test_customer_id_and_stream_list_from_env(poller, monkeypatch):
    monkeypatch.setenv("CAIB_GOOGLE_WORKSPACE_STREAMS", " admin , token:revoke ")
    monkeypatch.setenv("CAIB_GOOGLE_WORKSPACE_CUSTOMER_ID", "C03synth0")
    ctx = ReportsStub(recent_start())
    poller.fetch(None, ctx)
    assert [(a, p.get("eventName"), p["customerId"]) for a, p in ctx.requests] == [
        ("admin", None, "C03synth0"), ("token", "revoke", "C03synth0")]


# --- end to end ---------------------------------------------------------------


def test_end_to_end_poll_load_dedup(migrated_db, poller):
    stub = ReportsStub(recent_start())
    ctx = Ctx(module="google_workspace", backfill_start=stub.backfill_start,
              log=logging.getLogger("test"), http=None)

    class FixturePoller:
        module = "google_workspace"
        interval_s = 300

        def __init__(self, inner):
            self.inner = inner

        def fetch(self, cursor, _ctx):
            return self.inner.fetch(cursor, stub)

    same = FixturePoller(poller)
    with psycopg.connect(migrated_db) as conn:
        for _ in range(2):  # page 1 of everything, then login page 2
            framework.run_cycle(conn, same, ctx)
        assert loader.run_batch(conn, 100, drop_raw=False) == (20, 20, 0)

        # Same process re-reads the overlap: nothing even reaches landing.
        conn.execute("DELETE FROM ingest.cursors")
        conn.commit()
        for _ in range(2):
            _, landed, _ = framework.run_cycle(conn, same, ctx)
            assert landed == 0

        # A restarted process re-lands the overlap; the loader drops it all.
        conn.execute("DELETE FROM ingest.cursors")
        conn.commit()
        fresh = FixturePoller(gw.GoogleWorkspacePoller())
        for _ in range(2):
            framework.run_cycle(conn, fresh, ctx)
        assert loader.run_batch(conn, 100, drop_raw=False) == (20, 0, 0)

        rows = conn.execute(
            "SELECT attrs->>'application', count(*) FROM caib.events"
            " WHERE module = 'google_workspace' GROUP BY 1 ORDER BY 1"
        ).fetchall()
        assert rows == [("admin", 6), ("drive", 4), ("login", 6), ("saml", 2), ("token", 2)]


# --- demo seeding -------------------------------------------------------------


def load_demo_seed():
    spec = importlib.util.spec_from_file_location("demo_seed", ROOT / "scripts" / "demo_seed.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_demo_seed_envelopes_load_without_dead_letters(migrated_db):
    """`make demo` needs the compose stack; this exercises the same envelope
    path (fixtures -> poller envelopes() -> demo clone -> loader)."""
    demo = load_demo_seed()
    envs = demo.gw_demo_envelopes()
    assert len(envs) == 20
    now = datetime.datetime.now(UTC)
    clones = [demo.clone(e, now, i) for i, e in enumerate(envs)]
    with psycopg.connect(migrated_db) as conn:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO ingest.landing (received_at, module, payload)"
                " VALUES (now(), 'google_workspace', %s)",
                [(Jsonb(c),) for c in clones],
            )
        conn.commit()
        assert loader.run_batch(conn, 100, drop_raw=False) == (20, 20, 0)
