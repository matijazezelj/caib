"""Read-only HTTP/JSON API over caib_core.query.

Seven endpoints, one per query-core function, plus the two probes. There is no
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

from caib_agentapi.config import DEFAULT_PORT, Settings, make_pool

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


def require_token(request: Request) -> None:
    """Bearer-token auth, constant-time.

    compare_digest runs unconditionally and before the scheme check so that a
    request with the wrong scheme costs the same as one with the wrong token.
    Both sides are encoded to bytes because compare_digest rejects str with
    non-ASCII, and a token is whatever the operator generated.
    """
    settings: Settings = request.app.state.settings
    scheme, _, presented = request.headers.get("authorization", "").partition(" ")
    matches = hmac.compare_digest(presented.encode(), settings.token.encode())
    if not (matches and scheme.lower() == "bearer"):
        raise ApiError(http.HTTPStatus.UNAUTHORIZED, "unauthorized", "bearer token required")


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
    since: str | None = Query(None, description="ISO-8601; default 7d ago, on last_seen"),
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
    since: str | None = Query(None, description="ISO-8601; default 7d ago"),
) -> dict:
    with request.app.state.pool.connection() as conn:
        return _guard(query.alert_summary, conn, since=since)


# Declared before /actors/{actor}: FastAPI matches routes in declaration order,
# so the other way round an actor literally named "top" — or, more to the point,
# this fixed path — would be swallowed by the path parameter.
@v1.get("/actors/top")
def get_top_actors(
    request: Request,
    since: str | None = Query(None, description="ISO-8601; default 24h ago"),
    module: str | None = None,
    limit: int | None = Query(None, description="default 20, capped at 100"),
) -> dict:
    with request.app.state.pool.connection() as conn:
        return _guard(query.top_actors, conn, since=since, module=module, limit=limit)


@v1.get("/actors/{actor}")
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
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.pool.open()
        try:
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
    app.state.pool = make_pool(settings)

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
