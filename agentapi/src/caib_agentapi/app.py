"""Read-only HTTP/JSON API over caib_core.query, plus MCP over Streamable HTTP.

Seven endpoints, one per query-core function, the two probes, and `/mcp`: the
same seven tools as `caib_agentapi.mcp_server`, served by the MCP SDK's
Streamable HTTP transport behind the same bearer token. There is no
business logic here: routes parse query strings, hand them to the core, and
render what comes back. Anything that decides what an answer may contain — the
LIMIT caps, the lookback ceiling, whether `raw` is included — lives in
`caib_core.query` so that the MCP transport gets the identical behaviour.

Auth is a bearer token from CAIB_AGENT_API_TOKEN and the service refuses to
start without one (see `Settings.from_env`). The listener binds loopback by
default. Neither of those is the real boundary though: the real boundary is
that the connection underneath is `caib_agent_ro`, which holds SELECT on five
tables and cannot write. Read docs/AGENT_API.md before exposing this.
"""

import argparse
import contextlib
import hmac
import http
import logging
import os
import urllib.error
import urllib.request

import psycopg
from caib_core import query
from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp
from mcp.server.transport_security import TransportSecuritySettings
from starlette.routing import Route

from caib_agentapi.config import DEFAULT_PORT, Settings, make_pool
from caib_agentapi.mcp_server import create_server

log = logging.getLogger("caib.agentapi")


class ApiError(Exception):
    """Every error this API returns, in one shape: {"error": slug, "detail": …}.

    Slugs are stable and machine-readable; `detail` is prose for whoever is
    reading the transcript. Neither ever carries a traceback — those go to the
    server log, where they belong.
    """

    def __init__(self, status: int, error: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.error = error
        self.detail = detail


def _guard(fn, *args, **kwargs):
    """Run a query-core call and translate its failures.

    QueryError means the caller asked for something impossible: 400. A psycopg
    error means the database said no — unreachable, or the 15s statement_timeout
    fired: 503, because retrying later is the sensible response to both.
    Anything else is our bug: log it with the traceback, tell the client
    nothing.
    """
    try:
        return fn(*args, **kwargs)
    except query.QueryError as exc:
        raise ApiError(http.HTTPStatus.BAD_REQUEST, "bad_request", str(exc)) from exc
    except psycopg.errors.QueryCanceled as exc:
        log.warning("query exceeded statement_timeout: %s", exc)
        raise ApiError(
            http.HTTPStatus.SERVICE_UNAVAILABLE,
            "query_timeout",
            "the query exceeded the 15s statement timeout; narrow the window or "
            "add filters",
        ) from exc
    except psycopg.Error as exc:
        log.exception("database error")
        raise ApiError(
            http.HTTPStatus.SERVICE_UNAVAILABLE, "database_unavailable",
            "the database could not answer this request",
        ) from exc
    except Exception as exc:
        log.exception("unhandled error serving a query")
        raise ApiError(
            http.HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error",
            "internal error; see the server log",
        ) from exc


def token_ok(settings: Settings, authorization: str) -> bool:
    """Bearer-token check, constant-time.

    compare_digest runs unconditionally and before the scheme check so that a
    request with the wrong scheme costs the same as one with the wrong token.
    Both sides are encoded to bytes because compare_digest rejects str with
    non-ASCII, and a token is whatever the operator generated.
    """
    scheme, _, presented = authorization.partition(" ")
    matches = hmac.compare_digest(presented.encode(), settings.token.encode())
    return matches and scheme.lower() == "bearer"


def origin_allowed(settings: Settings, origin: str | None) -> bool:
    """DNS-rebinding guard (the MCP transport spec asks servers to validate
    Origin). Non-browser clients send no Origin and pass; a browser page may
    only call in from an Origin listed in CAIB_AGENT_ALLOWED_ORIGINS.

    Origin rather than Host: the SDK's own Host allowlist would have to know
    every name the service is reached by — the published port, a reverse
    proxy's hostname — and breaks the moment one is missing. The bearer token
    already stops a rebound page that does not hold it."""
    return origin is None or origin in settings.allowed_origins


def require_token(request: Request) -> None:
    settings: Settings = request.app.state.settings
    if not origin_allowed(settings, request.headers.get("origin")):
        raise ApiError(http.HTTPStatus.FORBIDDEN, "forbidden_origin", "Origin not allowed")
    if not token_ok(settings, request.headers.get("authorization", "")):
        raise ApiError(http.HTTPStatus.UNAUTHORIZED, "unauthorized", "bearer token required")


class GuardedMcp:
    """The MCP Streamable HTTP endpoint behind the API's own auth.

    A plain ASGI wrapper because `/mcp` is not a FastAPI route — FastAPI
    dependencies do not run for it — and the SDK's auth hooks are OAuth
    shaped. Refusals use this API's error body, so a client sees one shape
    whichever half of the service it called.
    """

    def __init__(self, app, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope, receive, send) -> None:
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        error = None
        if not origin_allowed(self.settings, headers.get("origin")):
            error = ApiError(
                http.HTTPStatus.FORBIDDEN, "forbidden_origin", "Origin not allowed"
            )
        elif not token_ok(self.settings, headers.get("authorization", "")):
            error = ApiError(
                http.HTTPStatus.UNAUTHORIZED, "unauthorized", "bearer token required"
            )
        if error is None:
            await self.app(scope, receive, send)
            return
        response = JSONResponse(
            status_code=error.status,
            content={"error": error.error, "detail": error.detail},
            headers={"WWW-Authenticate": "Bearer"} if error.status == 401 else None,
        )
        await response(scope, receive, send)


v1 = APIRouter(prefix="/v1", dependencies=[Depends(require_token)])


@v1.get("/events")
def get_events(
    request: Request,
    q: str | None = Query(None, description="substring match against search_text"),
    module: str | None = None,
    actor: str | None = None,
    action: str | None = None,
    outcome: str | None = None,
    severity: str | None = None,
    since: str | None = Query(None, description="ISO-8601; default 24h ago"),
    until: str | None = Query(None, description="ISO-8601; default now"),
    limit: int | None = Query(None, description="default 100, capped at 500"),
    include_raw: bool = Query(False, description="include the raw vendor event"),
) -> dict:
    with request.app.state.pool.connection() as conn:
        return _guard(
            query.search_events, conn, q=q, module=module, actor=actor, action=action,
            outcome=outcome, severity=severity, since=since, until=until, limit=limit,
            include_raw=include_raw,
        )


@v1.get("/alerts")
def get_alerts(
    request: Request,
    status: str | None = Query(None, description="open | ack | closed"),
    severity: str | None = None,
    rule_id: str | None = None,
    since: str | None = Query(
        None,
        description="ISO-8601, on last_seen, any status; default: every open/ack alert "
                    "plus closed ones from the past 7d",
    ),
    limit: int | None = Query(None, description="default 100, capped at 500"),
) -> dict:
    with request.app.state.pool.connection() as conn:
        return _guard(
            query.get_alerts, conn, status=status, severity=severity,
            rule_id=rule_id, since=since, limit=limit,
        )


@v1.get("/alerts/summary")
def get_alert_summary(
    request: Request,
    since: str | None = Query(
        None, description="ISO-8601; default: every open/ack alert plus closed from 7d"
    ),
) -> dict:
    with request.app.state.pool.connection() as conn:
        return _guard(query.alert_summary, conn, since=since)


# Declared before /actors/{actor:path}: FastAPI matches routes in declaration
# order, so the other way round an actor literally named "top" — or, more to the
# point, this fixed path — would be swallowed by the path parameter.
@v1.get("/actors/top")
def get_top_actors(
    request: Request,
    since: str | None = Query(None, description="ISO-8601; default 24h ago"),
    module: str | None = None,
    limit: int | None = Query(None, description="default 20, capped at 100"),
) -> dict:
    with request.app.state.pool.connection() as conn:
        return _guard(query.top_actors, conn, since=since, module=module, limit=limit)


# `:path`, not a plain segment: CloudTrail actors are IAM ARNs
# (arn:aws:iam::123456789012:user/carol), and a plain `{actor}` cannot match a
# `/` — not even percent-encoded, because the server decodes %2F before routing.
# Without it, actor_activity 404'd for every AWS principal.
@v1.get("/actors/{actor:path}")
def get_actor_activity(
    request: Request,
    actor: str,
    since: str | None = Query(None, description="ISO-8601; default 24h ago"),
    until: str | None = Query(None, description="ISO-8601; default now"),
    limit: int | None = Query(None, description="default 200, capped at 500"),
) -> dict:
    with request.app.state.pool.connection() as conn:
        return _guard(
            query.actor_activity, conn, actor=actor, since=since, until=until, limit=limit
        )


@v1.get("/health/modules")
def get_module_health(request: Request) -> dict:
    with request.app.state.pool.connection() as conn:
        return _guard(query.module_health, conn)


@v1.get("/rules")
def get_rules() -> dict:
    # No connection: the rule set is YAML on disk. See caib_core.query.list_rules.
    return _guard(query.list_rules)


def create_app(settings: Settings) -> FastAPI:
    pool = make_pool(settings)
    mcp_server = create_server(pool)
    # Building the SDK's app is what creates its session manager; its routes
    # are not used — /mcp is registered below, behind GuardedMcp. Stateless
    # with JSON responses: no server-initiated messages exist here, and no
    # session affinity means any worker or restart can answer any request.
    # DNS-rebinding protection is off in the SDK because GuardedMcp does it
    # (origin_allowed); left on, it pins Host to localhost and 421s every
    # request arriving through a reverse proxy.
    mcp_server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.pool.open()
        try:
            async with mcp_server.session_manager.run():
                yield
        finally:
            app.state.pool.close()

    app = FastAPI(
        title="CAIB agent query API",
        version="1",
        description="Read-only query surface over the CAIB audit log.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.pool = pool

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
        return JSONResponse(
            status_code=exc.status,
            content={"error": exc.error, "detail": exc.detail},
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's own 422 body names internal locations; re-render it in this
        # API's single error shape instead.
        fields = ", ".join(".".join(str(p) for p in e["loc"][1:]) for e in exc.errors())
        return JSONResponse(
            status_code=http.HTTPStatus.BAD_REQUEST,
            content={"error": "bad_request", "detail": f"invalid query parameter: {fields}"},
        )

    # Unauthenticated on purpose, and both are safe to be: /healthz answers from
    # process state alone, /readyz reports reachability and nothing about the
    # data. The compose healthcheck calls /healthz, and making it carry the
    # bearer token would put the token in `docker inspect` output for no gain.
    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        try:
            with app.state.pool.connection() as conn:
                conn.execute("SELECT 1")
        except Exception:
            log.exception("readiness check failed")
            return JSONResponse(
                status_code=http.HTTPStatus.SERVICE_UNAVAILABLE,
                content={"error": "database_unavailable", "detail": "no database round-trip"},
            )
        return JSONResponse(status_code=http.HTTPStatus.OK, content={"status": "ready"})

    app.include_router(v1)
    app.router.routes.append(
        Route(
            "/mcp",
            endpoint=GuardedMcp(StreamableHTTPASGIApp(mcp_server.session_manager), settings),
            methods=["GET", "POST", "DELETE"],
        )
    )
    return app


def _port_from_env() -> int:
    raw = os.environ.get("CAIB_AGENT_API_PORT", str(DEFAULT_PORT))
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit(f"CAIB_AGENT_API_PORT={raw!r} is not a number") from exc


def _healthcheck(port: int) -> None:
    """`caib-agent-api --healthcheck`, for the compose healthcheck.

    Mirrors `caib-core healthcheck --service X`: the image carries its own
    prober rather than a curl or wget the runtime stage would otherwise have no
    reason to ship. Always loopback — it is checking this process.
    """
    url = f"http://127.0.0.1:{port}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            if response.status != 200:
                raise SystemExit(f"{url}: HTTP {response.status}")
    except (urllib.error.URLError, OSError) as exc:
        raise SystemExit(f"{url}: {exc}") from exc


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="caib-agent-api")
    parser.add_argument(
        "--healthcheck", action="store_true",
        help="GET /healthz on this host and exit non-zero if it does not answer",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    if args.healthcheck:
        # Deliberately before Settings.from_env: probing a running process must
        # not need the database URL or the bearer token.
        _healthcheck(_port_from_env())
        return

    settings = Settings.from_env(require_token=True)
    import uvicorn

    log.info("caib-agent-api listening on %s:%d", settings.bind, settings.port)
    uvicorn.run(create_app(settings), host=settings.bind, port=settings.port, log_level="info")
