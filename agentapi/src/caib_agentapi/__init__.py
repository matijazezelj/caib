"""CAIB agent query layer.

Two read-only transports over `caib_core.query`: an HTTP/JSON API
(`caib_agentapi.app`, console script `caib-agent-api`) and an MCP stdio server
(`caib_agentapi.mcp_server`, console script `caib-agent-mcp`). Both connect to
PostgreSQL as `caib_agent_ro` and neither can write anything — see
docs/AGENT_API.md.
"""
