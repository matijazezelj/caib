"""Environment configuration shared by both transports.

Everything that can be wrong about a deployment is decided here, at startup,
rather than at the first request: a missing token, an absent connection URL, a
non-numeric port. `caib_core.detect.Cfg.from_env` takes the same line, and for
the same reason — a service that starts and then fails on first use looks
healthy for as long as nobody asks it anything.
"""

import dataclasses
import os

import psycopg_pool

DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8088

# Small on purpose. The API is not a user-facing web app; it serves a handful of
# agents, every query is capped and every connection carries a 15s
# statement_timeout from the role. A big pool here would only buy the ability to
# run more expensive scans at once.
DEFAULT_POOL_MAX = 4


@dataclasses.dataclass(frozen=True)
class Settings:
    pg_url: str
    token: str = ""
    bind: str = DEFAULT_BIND
    port: int = DEFAULT_PORT
    pool_max_size: int = DEFAULT_POOL_MAX

    @classmethod
    def from_env(cls, *, require_token: bool) -> "Settings":
        """Read the environment or exit.

        `require_token` is False for the MCP server: it speaks stdio to a client
        that already started the process, so there is no network listener to put
        a bearer token in front of. The HTTP API passes True, and there is no
        way to make it pass False.
        """
        pg_url = os.environ.get("CAIB_RO_PG_URL", "")
        if not pg_url:
            raise SystemExit(
                "CAIB_RO_PG_URL is not set. It should be a libpq URL for the "
                "caib_agent_ro role — see docs/AGENT_API.md. Do not point this "
                "at caib_app or caib_ro: read-only is enforced by the role."
            )

        token = os.environ.get("CAIB_AGENT_API_TOKEN", "")
        if require_token and not token:
            # No "open mode", not even behind loopback. An unauthenticated port
            # here is every audit event in the deployment, and the failure mode
            # of forgetting the token must be "it did not start", not "it
            # started and served".
            raise SystemExit(
                "CAIB_AGENT_API_TOKEN is empty or unset. caib-agent-api will "
                "not run without one; generate a token (`make .env` does) and "
                "set it. There is no unauthenticated mode."
            )

        port_raw = os.environ.get("CAIB_AGENT_API_PORT", str(DEFAULT_PORT))
        try:
            port = int(port_raw)
        except ValueError as exc:
            raise SystemExit(f"CAIB_AGENT_API_PORT={port_raw!r} is not a number") from exc

        return cls(
            pg_url=pg_url,
            token=token,
            # Loopback unless told otherwise, matching CAIB_BIND_ADDR's default.
            # The compose service overrides this to 0.0.0.0 because there it is
            # the *published* port that decides exposure, and that one binds to
            # CAIB_BIND_ADDR.
            bind=os.environ.get("CAIB_AGENT_API_BIND", DEFAULT_BIND),
            port=port,
        )


def make_pool(settings: Settings) -> psycopg_pool.ConnectionPool:
    """An unopened pool. Callers open it inside their own lifecycle so a
    database that is not up yet fails as a readiness problem rather than as an
    import-time crash."""
    return psycopg_pool.ConnectionPool(
        settings.pg_url,
        min_size=1,
        max_size=settings.pool_max_size,
        open=False,
        # A request should not sit in the pool queue longer than the query
        # itself is allowed to run.
        timeout=15.0,
        kwargs={"connect_timeout": 5},
    )
