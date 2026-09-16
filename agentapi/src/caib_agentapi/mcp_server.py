"""MCP server over caib_core.query: stdio here, Streamable HTTP via app.py.

The same seven functions the HTTP API exposes, as MCP tools, over the same
query core — so an agent gets identical answers and identical refusals whichever
transport it is holding. Tools only: no sampling, no prompts, no resources.

It connects to PostgreSQL directly as `caib_agent_ro`, not through the HTTP API.
There is no bearer token here and none is needed: stdio has exactly one client,
the process that spawned this one, and it already has whatever access the
operator gave it. The security boundary is the database role — five readable
tables, no write privileges, a 15s statement_timeout — which is the same
boundary the HTTP API has.

Tool descriptions are written for the agent reading them to choose between
tools, not for a human skimming a reference. That means saying what each one is
for, what it will not do, and where the caps are, because an agent that does not
know a result was truncated will report the truncation as the finding.
"""

import contextlib
import logging
import sys
from typing import Annotated

import anyio
import psycopg
import psycopg_pool
from caib_core import query
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from caib_agentapi.config import Settings, make_pool

log = logging.getLogger("caib.agentmcp")

INSTRUCTIONS = """\
CAIB is a self-hosted audit-log SIEM over cloud and SaaS control-plane events
(AWS CloudTrail, GCP audit logs, Azure activity logs, Cloudflare audit logs).

This server is READ-ONLY and cannot be made otherwise: it holds a database role
with SELECT on five tables and no write privilege at all. There is no tool to
acknowledge, close, silence or create an alert, and adding one would take a
database migration, not a flag.

Times are UTC everywhere. Raw events are retained 90 days, so no tool will look
further back than that. Every listing tool caps its own result size and reports
`truncated` and the effective `window` it used — read those before concluding
that something did or did not happen.\
"""

# Reused parameter annotations, so the wording an agent reads is the same in
# every tool that takes the same argument.
Since = Annotated[
    str | None,
    Field(description="Start of the window, ISO-8601 (e.g. 2026-09-01T00:00:00Z). "
                      "Naive timestamps are read as UTC. Clamped to 90 days ago."),
]
AlertSince = Annotated[
    str | None,
    Field(description="Only alerts last seen at or after this, ISO-8601, any status. "
                      "Omit to get every open and acked alert however old, plus closed "
                      "alerts from the past 7 days."),
]
Until = Annotated[
    str | None,
    Field(description="End of the window, ISO-8601. Defaults to now."),
]
Module = Annotated[
    str | None,
    Field(description="Restrict to one source module: aws_cloudtrail, gcp_audit, "
                      "azure_activity or cloudflare_audit. Use module_health to see "
                      "which are actually ingesting."),
]
Severity = Annotated[
    str | None,
    Field(description="Exactly one of: info, low, medium, high, critical. "
                      "Anything else is rejected rather than silently matching nothing."),
]


def create_server(pool: psycopg_pool.ConnectionPool) -> MCPServer:
    """Register the seven tools against an already-open pool.

    The caller owns the pool's lifecycle; `main()` opens and closes it around
    the stdio run, and tests hand in their own.
    """

    async def run(fn, **kwargs) -> dict:
        def call():
            with pool.connection() as conn:
                return fn(conn, **kwargs)

        # ToolError text reaches the model; anything else the SDK turns into a
        # generic "Error executing tool" and logs with a traceback. A caller
        # error and an unavailable database are both things an agent can act on
        # — retry narrower, retry later — so they get to be legible. A bug in
        # here is not, and stays on the server.
        try:
            # psycopg is synchronous. Off the event loop, so a slow query cannot
            # stall the stdio session's protocol traffic.
            return await anyio.to_thread.run_sync(call)
        except query.QueryError as exc:
            raise ToolError(str(exc)) from exc
        except psycopg.errors.QueryCanceled as exc:
            raise ToolError(
                "the query hit the 15s statement timeout. Narrow the time window, "
                "add a module or actor filter, or ask for fewer rows."
            ) from exc
        except psycopg.Error as exc:
            log.exception("database error serving an MCP tool call")
            raise ToolError("the CAIB database could not answer this request") from exc

    async def search_events(
        q: Annotated[
            str | None,
            Field(description="Free-text substring, matched case-insensitively against a "
                              "per-event index of actor, action, target, source IP, outcome "
                              "and attribute values. '%' and '_' act as SQL LIKE wildcards."),
        ] = None,
        module: Module = None,
        actor: Annotated[
            str | None,
            Field(description="Exact actor identity, e.g. an IAM ARN or an email address. "
                              "Exact match, not a substring — use q for partial matches."),
        ] = None,
        action: Annotated[
            str | None,
            Field(description="Exact action name as the vendor spells it, "
                              "e.g. GetObject, SetIamPolicy."),
        ] = None,
        outcome: Annotated[
            str | None,
            Field(description="Exactly one of: success, failure, unknown."),
        ] = None,
        severity: Severity = None,
        since: Since = None,
        until: Until = None,
        limit: Annotated[
            int | None,
            Field(description="Rows to return. Default 100, hard cap 500. Asking for more "
                              "returns 500 and sets truncated."),
        ] = None,
        include_raw: Annotated[
            bool,
            Field(description="Include the full original vendor event per row. Off by "
                              "default: it is large and unsanitised. Turn it on only when "
                              "the normalised fields are genuinely not enough."),
        ] = False,
    ) -> dict:
        return await run(
            query.search_events, q=q, module=module, actor=actor, action=action,
            outcome=outcome, severity=severity, since=since, until=until, limit=limit,
            include_raw=include_raw,
        )

    async def get_alerts(
        status: Annotated[
            str | None,
            Field(description="Exactly one of: open, ack, closed. Omit for all three."),
        ] = None,
        severity: Severity = None,
        rule_id: Annotated[
            str | None,
            Field(description="Exact detection rule id, as listed by list_rules "
                              "(e.g. aws_cloudtrail.admin_policy_attached)."),
        ] = None,
        since: AlertSince = None,
        limit: Annotated[
            int | None,
            Field(description="Rows to return. Default 100, hard cap 500."),
        ] = None,
    ) -> dict:
        return await run(
            query.get_alerts, status=status, severity=severity, rule_id=rule_id,
            since=since, limit=limit,
        )

    async def alert_summary(since: AlertSince = None) -> dict:
        return await run(query.alert_summary, since=since)

    async def top_actors(
        since: Since = None,
        module: Module = None,
        limit: Annotated[
            int | None,
            Field(description="Actors to return. Default 20, hard cap 100."),
        ] = None,
    ) -> dict:
        return await run(query.top_actors, since=since, module=module, limit=limit)

    async def actor_activity(
        actor: Annotated[
            str,
            Field(description="The exact actor identity to profile. Get one from "
                              "top_actors or from a search_events / get_alerts result."),
        ],
        since: Since = None,
        until: Until = None,
        limit: Annotated[
            int | None,
            Field(description="Events to return. Default 200, hard cap 500. The per-action "
                              "rollup covers the whole window regardless."),
        ] = None,
    ) -> dict:
        return await run(
            query.actor_activity, actor=actor, since=since, until=until, limit=limit
        )

    async def module_health() -> dict:
        return await run(query.module_health)

    async def list_rules() -> dict:
        # No pool: the rule set is YAML on disk, read through the same call the
        # detection engine makes. See caib_core.query.list_rules.
        return await anyio.to_thread.run_sync(query.list_rules)

    server = MCPServer(
        name="caib",
        title="CAIB audit log (read-only)",
        version="1",
        instructions=INSTRUCTIONS,
    )

    server.add_tool(
        search_events,
        name="search_events",
        description=(
            "Search normalised cloud/SaaS audit events. This is the general-purpose "
            "starting point: free-text q plus exact filters on module, actor, action, "
            "outcome and severity, newest first. Defaults to the last 24 hours — pass "
            "since/until for anything else. Returns the window it actually used and "
            "whether the result was truncated; a truncated result means there is more, "
            "not that you have seen everything."
        ),
    )
    server.add_tool(
        get_alerts,
        name="get_alerts",
        description=(
            "List alerts the detection rules have raised, most recently seen first. An "
            "alert folds every recurrence of the same thing into one row, so read "
            "event_count, first_seen and last_seen together rather than counting rows. "
            "Without since: every open and acked alert however old, plus closed alerts "
            "last seen in the past 7 days. With since: every alert last seen since then, "
            "any status. Read-only: there is no way to acknowledge or close an alert here."
        ),
    )
    server.add_tool(
        alert_summary,
        name="alert_summary",
        description=(
            "Counts of alerts over a window, broken down by status and by severity. Ask "
            "this first when the question is 'how bad is it right now' — it is one cheap "
            "query, where get_alerts would page through rows. Without since, counts every "
            "open and acked alert plus closed ones last seen in the past 7 days."
        ),
    )
    server.add_tool(
        top_actors,
        name="top_actors",
        description=(
            "Busiest actors in a window with their failure counts — who is doing the "
            "most, and who is failing the most. Good for spotting a service account "
            "gone wrong or a brute-force pattern. Defaults to the last 24 hours. Reads a "
            "pre-aggregated hourly rollup when the window starts on an hour boundary and "
            "counts raw events otherwise; the answer says which in 'source'."
        ),
    )
    server.add_tool(
        actor_activity,
        name="actor_activity",
        description=(
            "One actor's timeline: their events newest-first plus a per-action rollup "
            "over the whole window. Use this after top_actors, get_alerts or "
            "search_events has given you an identity worth investigating. The rollup is "
            "computed over the full window even when the event list is truncated, so the "
            "counts stay true."
        ),
    )
    server.add_tool(
        module_health,
        name="module_health",
        description=(
            "Is the data any good right now? Per source module: how long since its last "
            "event, and how many events arrived in the last hour and day. Plus a "
            "heartbeat age per engine loop (loader, detect). Check this before "
            "concluding from an empty search that nothing happened — it may mean nothing "
            "was ingested. Heartbeats are per engine, not per module: a module going "
            "quiet shows up as its lag_seconds growing."
        ),
    )
    server.add_tool(
        list_rules,
        name="list_rules",
        description=(
            "The detection rules currently loaded, with id, module, title, severity and "
            "whether each is enabled. Use it to map a rule_id on an alert back to what "
            "the rule is for, or to check whether something is not alerting because no "
            "rule covers it. The rule SQL itself is not returned."
        ),
    )
    return server


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        # stdout is the MCP transport. Anything written there that is not a
        # JSON-RPC frame corrupts the session, so logs go to stderr.
        stream=sys.stderr,
    )
    settings = Settings.from_env(require_token=False)
    pool = make_pool(settings)
    pool.open()
    try:
        server = create_server(pool)
        anyio.run(server.run_stdio_async)
    finally:
        with contextlib.suppress(Exception):
            pool.close()
