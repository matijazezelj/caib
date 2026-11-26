"""Google Workspace audit-log poller (Admin SDK Reports API, activities.list).

Built 2026-11-26 from Google's published docs, no tenant available — see
modules/google_workspace/API_NOTES.md for what is verified vs assumed.

Auth: a service account with domain-wide delegation impersonates an admin
(JWT bearer grant, scope admin.reports.audit.readonly). The JWT is RS256-signed
with `cryptography`.

Cursoring: one sub-cursor per stream (an application, optionally narrowed to
one eventName, e.g. ``token:authorize``). Each window is
``[since - overlap, min(since + 24h, now - lag)]``: the overlap re-reads the
recent past because Google documents lag of "a couple of minutes" for most
applications and "a couple of hours" for token, with "a small chance" of more.
Re-read events carry the same ``event_id`` and the loader drops them
(sha256(module:event_id)); an in-process seen-set additionally keeps the
steady-state overlap from re-landing at all. The seen-set is an optimisation
only: after a restart the first overlap re-lands and the loader dedups it.
"""

import base64
import datetime
import json
import os
import time

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from caib_pollers.framework import Ctx, FetchResult

API_BASE = "https://admin.googleapis.com/admin/reports/v1/activity/users/all/applications"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/admin.reports.audit.readonly"
JWT_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"

DEFAULT_STREAMS = "login,admin,drive,token:authorize,saml"
PAGE_SIZE = 1000  # documented maximum
# Seal windows a little behind now; the overlap below is what absorbs lag.
WINDOW_LAG = datetime.timedelta(seconds=120)
# Google's lag table: login/admin/drive/saml "near real time (couple of
# minutes)", token "a couple of hours". Overlap = generous multiple of that.
DEFAULT_OVERLAP = datetime.timedelta(minutes=30)
OVERLAP = {"token": datetime.timedelta(hours=6)}
# Backfill as a series of bounded windows, not one long page-token chain.
MAX_WINDOW = datetime.timedelta(hours=24)
# endTime-less queries reach back 180 days at most; data is kept ~6 months.
RETENTION = datetime.timedelta(days=179)
TIME_FMT = "%Y-%m-%dT%H:%M:%SZ"

# Event parameters surfaced as attrs (whitelist — every key is in MAPPING.md).
PARAM_ATTRS = {
    # login
    "login_type": "login_type",
    "login_challenge_method": "login_challenge_method",
    "login_failure_type": "login_failure_type",
    "is_suspicious": "is_suspicious",
    "is_second_factor": "is_second_factor",
    "affected_email_address": "affected_email_address",
    "email_forwarding_destination_address": "forwarding_destination",
    # admin
    "USER_EMAIL": "user_email",
    "GROUP_EMAIL": "group_email",
    "ROLE_NAME": "role_name",
    "SETTING_NAME": "setting_name",
    "NEW_VALUE": "new_value",
    "OLD_VALUE": "old_value",
    "ORG_UNIT_NAME": "org_unit",
    "DOMAIN_NAME": "domain_name",
    "API_CLIENT_NAME": "api_client_name",
    "API_SCOPES": "api_scopes",
    # drive
    "doc_id": "doc_id",
    "doc_title": "doc_title",
    "doc_type": "doc_type",
    "owner": "owner",
    "visibility": "visibility",
    "old_visibility": "old_visibility",
    "new_value": "new_value",
    "old_value": "old_value",
    "target_domain": "target_domain",
    "target_user": "target_user",
    # token
    "app_name": "app_name",
    "client_id": "client_id",
    "client_type": "client_type",
    "scope": "scope",
    # saml
    "application_name": "saml_app",
    "initiated_by": "initiated_by",
    "failure_type": "failure_type",
    "saml_status_code": "saml_status_code",
    "orgunit_path": "org_unit",
}

# Which parameter (first present) names the object acted on, per application.
TARGET_PARAMS = {
    "admin": ("USER_EMAIL", "GROUP_EMAIL", "API_CLIENT_NAME", "DOMAIN_NAME"),
    "drive": ("doc_id",),
    "token": ("client_id", "app_name"),
    "saml": ("application_name",),
    "login": ("email_forwarding_destination_address", "affected_email_address"),
}

FAILURE_EVENTS = {"login_failure", "deny", "risky_sensitive_action_blocked"}


# --- envelope -----------------------------------------------------------------


def _param_value(param: dict):
    for key in ("value", "boolValue", "intValue"):
        if key in param:
            return param[key]
    for key in ("multiValue", "multiIntValue"):
        if key in param:
            return ",".join(str(v) for v in param[key])
    return None  # messageValue / multiMessageValue: left in raw


def _params(event: dict) -> dict:
    out = {}
    for param in event.get("parameters") or []:
        name = param.get("name")
        if name:
            out[name] = _param_value(param)
    return out


def _event_time(value) -> str | None:
    """id.time is RFC3339 in Google's samples; the discovery doc's prose says
    epoch seconds. Accept both (verify-live item)."""
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        return datetime.datetime.fromtimestamp(int(value), datetime.UTC).strftime(TIME_FMT)
    return value


def _outcome(app: str, event: dict) -> str:
    status = (event.get("status") or {}).get("eventStatus")
    if status == "FAILED":
        return "failure"
    if status in ("SUCCEEDED", "SUCCEEDED_WITH_WARNINGS"):
        return "success"
    name = event.get("name")
    if name in FAILURE_EVENTS:
        return "failure"
    if name == "login_success":
        return "success"
    if app in ("login", "saml"):
        return "unknown"  # warnings, challenges, account changes: not pass/fail
    return "success"  # admin/drive/token entries record completed actions


def envelope(activity: dict, index: int = 0) -> dict:
    """Envelope for activity.events[index] — one landing row per event."""
    aid = activity.get("id") or {}
    app = aid.get("applicationName") or ""
    actor = activity.get("actor") or {}
    events = activity.get("events") or [{}]
    event = events[index]
    params = _params(event)
    event_time = _event_time(aid.get("time"))
    qualifier = aid.get("uniqueQualifier")

    target = next(
        (params[k] for k in TARGET_PARAMS.get(app, ()) if params.get(k) not in (None, "")),
        None,
    )
    attrs = {
        "application": app,
        "event_type": event.get("type"),
        "caller_type": actor.get("callerType"),
        "actor_profile_id": actor.get("profileId"),
        "owner_domain": activity.get("ownerDomain"),
        "region_code": (activity.get("networkInfo") or {}).get("regionCode"),
    }
    for name, value in params.items():
        if name in PARAM_ATTRS and value not in (None, ""):
            attrs[PARAM_ATTRS[name]] = value
    return {
        "event_time": event_time,
        # (time, uniqueQualifier) identifies an activity; index the event in it.
        "event_id": f"{app}:{event_time}:{qualifier}:{index}" if qualifier else None,
        "actor": actor.get("email") or actor.get("key") or actor.get("profileId"),
        "src_ip": activity.get("ipAddress"),
        "action": event.get("name") or "unknown",
        "target": str(target) if target is not None else None,
        "outcome": _outcome(app, event),
        "severity": "info",
        "attrs": {k: v for k, v in attrs.items() if v not in (None, "")},
        "raw": activity,
    }


def envelopes(activity: dict) -> list[dict]:
    return [envelope(activity, i) for i in range(max(1, len(activity.get("events") or [])))]


# --- auth: service account JWT bearer, RS256 via `cryptography` -------------


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _rsa_key(pem: str) -> rsa.RSAPrivateKey:
    """The service account's PEM key (PKCS#8 as Google issues it, or PKCS#1)."""
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise TypeError("service account private_key is not an RSA key")
    return key


def _rs256_sign(message: bytes, pem: str) -> bytes:
    """RSASSA-PKCS1-v1_5 with SHA-256 (JWT RS256)."""
    return _rsa_key(pem).sign(message, padding.PKCS1v15(), hashes.SHA256())


def load_service_account() -> dict:
    """The SA key JSON, base64-encoded in CAIB_GOOGLE_WORKSPACE_SA_KEY_B64 (an
    env var, not a mounted file: the poller runs as uid 10001 and a 0600 key
    file owned by the host user would be unreadable)."""
    raw = os.environ.get("CAIB_GOOGLE_WORKSPACE_SA_KEY_B64", "").strip()
    if not raw:
        raise RuntimeError("google_workspace: CAIB_GOOGLE_WORKSPACE_SA_KEY_B64 is not set")
    try:
        key = json.loads(base64.b64decode(raw))
        _rsa_key(key["private_key"])
        if not key.get("client_email"):
            raise KeyError("client_email")
    except (ValueError, KeyError, TypeError, IndexError) as exc:  # never echo key material
        raise RuntimeError(
            "google_workspace: CAIB_GOOGLE_WORKSPACE_SA_KEY_B64 is not a base64 "
            f"service-account JSON key with client_email/private_key ({type(exc).__name__})"
        ) from None
    return key


class ServiceAccountToken:
    """Access-token cache. Every fetch signs a FRESH assertion: a JWT's exp is
    at most iat + 1h, so a cached assertion would fail the second refresh."""

    def __init__(self, key: dict, subject: str, scope: str = SCOPE,
                 early_refresh_s: float = 300.0):
        self.client_email = key["client_email"]
        self.private_key = key["private_key"]
        self.key_id = key.get("private_key_id")
        self.subject = subject
        self.scope = scope
        self.early_refresh_s = early_refresh_s
        self._token: str | None = None
        self._expires_at = 0.0

    def assertion(self, now: int) -> str:
        header = {"alg": "RS256", "typ": "JWT"}
        if self.key_id:
            header["kid"] = self.key_id
        claims = {
            "iss": self.client_email,
            "sub": self.subject,  # the impersonated admin (domain-wide delegation)
            "scope": self.scope,
            "aud": TOKEN_URL,
            "iat": now,
            "exp": now + 3600,
        }
        signing_input = (
            _b64url(json.dumps(header, separators=(",", ":")).encode())
            + "."
            + _b64url(json.dumps(claims, separators=(",", ":")).encode())
        )
        signature = _rs256_sign(signing_input.encode(), self.private_key)
        return signing_input + "." + _b64url(signature)

    def token(self, http: httpx.Client) -> str:
        if self._token and time.monotonic() < self._expires_at - self.early_refresh_s:
            return self._token
        resp = http.post(
            TOKEN_URL,
            data={"grant_type": JWT_GRANT, "assertion": self.assertion(int(time.time()))},
        )
        resp.raise_for_status()  # bad key / missing delegation must crash loudly
        body = resp.json()
        self._token = body["access_token"]
        self._expires_at = time.monotonic() + float(body.get("expires_in", 3600))
        return self._token


# --- poller -------------------------------------------------------------------


def _parse(ts: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(ts)


def _fmt(ts: datetime.datetime) -> str:
    return ts.astimezone(datetime.UTC).strftime(TIME_FMT)


def streams_from_env() -> list[str]:
    raw = os.environ.get("CAIB_GOOGLE_WORKSPACE_STREAMS", "").strip() or DEFAULT_STREAMS
    return [s.strip() for s in raw.split(",") if s.strip()]


class GoogleWorkspacePoller:
    module = "google_workspace"
    interval_s = 300

    def __init__(self):
        self._auth: ServiceAccountToken | None = None
        # stream -> {event_id: event_time}; pruned when a window seals.
        self._seen: dict[str, dict[str, datetime.datetime]] = {}

    def _auth_headers(self, ctx: Ctx) -> dict:
        if self._auth is None:
            subject = os.environ.get("CAIB_GOOGLE_WORKSPACE_ADMIN_EMAIL", "").strip()
            if not subject:
                raise RuntimeError(
                    "google_workspace: CAIB_GOOGLE_WORKSPACE_ADMIN_EMAIL is not set"
                )
            self._auth = ServiceAccountToken(load_service_account(), subject)
        return {"Authorization": f"Bearer {self._auth.token(ctx.http)}"}

    def fetch(self, cursor: dict | None, ctx: Ctx) -> FetchResult:
        headers = self._auth_headers(ctx)
        now = datetime.datetime.now(datetime.UTC)
        state = dict((cursor or {}).get("streams") or {})
        out: list[dict] = []
        has_more = False
        for stream in streams_from_env():
            st, envs = self._fetch_stream(stream, state.get(stream) or {}, now, ctx, headers)
            state[stream] = st
            out.extend(envs)
            # Mid-page, or a capped backfill window left the stream still due.
            if st.get("page_token") or self._due(st, ctx, now):
                has_more = True
        return FetchResult(out, {"streams": state}, has_more=has_more)

    def _due(self, st: dict, ctx: Ctx, now: datetime.datetime) -> bool:
        """A sealed stream opens a new window only once a poll interval has
        accumulated: while another stream pages (has_more loops immediately),
        idle streams make no request."""
        since = _parse(st["since"]) if st.get("since") else ctx.backfill_start
        return (now - WINDOW_LAG) - since >= datetime.timedelta(seconds=self.interval_s)

    def _fetch_stream(self, stream: str, st: dict, now: datetime.datetime,
                      ctx: Ctx, headers: dict) -> tuple[dict, list[dict]]:
        app, _, event_name = stream.partition(":")
        overlap = OVERLAP.get(app, DEFAULT_OVERLAP)
        if st.get("page_token"):
            since, start, end = st["since"], st["start"], st["end"]
            page_token = st["page_token"]
        else:
            if not self._due(st, ctx, now):
                return st, []
            since_dt = _parse(st["since"]) if st.get("since") else ctx.backfill_start
            # Older than Google keeps (or a long outage): resume at the edge.
            since_dt = max(since_dt, now - RETENTION + overlap)
            since = _fmt(since_dt)
            start = _fmt(max(since_dt - overlap, now - RETENTION))
            end = _fmt(min(since_dt + MAX_WINDOW, now - WINDOW_LAG))
            page_token = None

        params: dict = {"startTime": start, "endTime": end, "maxResults": PAGE_SIZE}
        if event_name:
            params["eventName"] = event_name
        if customer := os.environ.get("CAIB_GOOGLE_WORKSPACE_CUSTOMER_ID", "").strip():
            params["customerId"] = customer
        if page_token:
            params["pageToken"] = page_token
        try:
            resp = ctx.get(f"{API_BASE}/{app}", params=params, headers=headers)
        except httpx.HTTPStatusError as exc:
            # A persisted pageToken whose lifetime ran out would 400 forever with
            # the cursor untouched. Restart that window instead (dedup absorbs
            # the re-read). Auth errors (401/403) still crash.
            if page_token and exc.response.status_code == 400:
                ctx.log.warning("google_workspace: stream %s pageToken rejected (400); "
                                "restarting window at %s", stream, since)
                return {"since": since}, []
            raise

        body = resp.json()
        seen = self._seen.setdefault(stream, {})
        fresh: list[dict] = []
        for activity in body.get("items") or []:
            for env in envelopes(activity):
                eid = env["event_id"]
                if eid and eid in seen:
                    continue
                if eid and env["event_time"]:
                    seen[eid] = _parse(env["event_time"])
                fresh.append(env)

        if (next_page := body.get("nextPageToken")) and body.get("items"):
            return {"since": since, "start": start, "end": end, "page_token": next_page}, fresh
        # Window sealed. The next one re-reads [end - overlap, ...]; nothing
        # older than that can come back, so forget it.
        horizon = _parse(end) - overlap
        self._seen[stream] = {k: t for k, t in seen.items() if t >= horizon}
        return {"since": end}, fresh


POLLER = GoogleWorkspacePoller()
