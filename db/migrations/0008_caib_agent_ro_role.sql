-- Least-privilege read-only role for the agent query layer (docs/AGENT_API.md).
--
-- Why a SECOND read-only role instead of reusing caib_ro: caib_ro is Grafana's
-- datasource and holds SELECT on ALL TABLES in caib/ingest/detect/ops, granted
-- afresh on every migrate by caib_core.migrate.sync_grants(). That breadth is
-- deliberate — the provisioned Overview dashboard reads ingest.landing and
-- ingest.dead_letter (see the 2026-08-24 DECISIONS entry). Narrowing caib_ro
-- would silently break shipped panels; pointing the agent layer at caib_ro
-- would hand an LLM the unsanitised vendor payloads sitting in ingest.*.
-- So the agent layer gets its own role with an enumerated grant list.
--
-- The role OBJECT (CREATE ROLE, GRANT CONNECT, ALTER ROLE ... SET) is created
-- in caib_core.migrate.bootstrap() as superuser, not here: this file runs under
-- SET LOCAL ROLE caib_app, and caib_app is neither superuser nor CREATEROLE, so
-- the only privileges it can hand out are those on objects it owns. That is
-- exactly the schema and table grants below — which is the security-relevant
-- half, and the half worth keeping in a file someone can review.
--
-- Passwords are never in migration files (spec §Data model): migrate syncs them
-- from CAIB_AGENT_RO_PASSWORD via ALTER ROLE, same as caib_app and caib_ro.
--
-- Re-runnable: GRANT is idempotent, so replaying this file is a no-op.

-- Schema visibility only. USAGE alone grants nothing on the tables inside.
GRANT USAGE ON SCHEMA caib   TO caib_agent_ro;
GRANT USAGE ON SCHEMA detect TO caib_agent_ro;
GRANT USAGE ON SCHEMA ops    TO caib_agent_ro;

-- The entire read surface of the agent query layer, one line per table. Adding
-- a line here is the only way to widen it; tests/test_query_core.py asserts
-- this exact set and fails if it drifts.
GRANT SELECT ON caib.events     TO caib_agent_ro;  -- search_events, actor_activity
GRANT SELECT ON caib.events_5m  TO caib_agent_ro;  -- module_health
GRANT SELECT ON caib.actors_1h  TO caib_agent_ro;  -- top_actors
GRANT SELECT ON detect.alerts   TO caib_agent_ro;  -- get_alerts, alert_summary
GRANT SELECT ON ops.heartbeat   TO caib_agent_ro;  -- module_health

-- Deliberately NOT granted, and each for a reason:
--   ingest.landing, ingest.dead_letter  raw vendor envelopes, unsanitised; the
--                                       dead-letter table is by definition the
--                                       stuff whose shape we got wrong
--   ingest.cursors                      poller state, includes API pagination
--                                       tokens for some vendors
--   detect.outbox                       notification payloads plus delivery
--                                       state; nothing an analyst agent needs
--   detect.rule_state                   engine watermarks; internal
-- No USAGE on schema ingest is granted at all, so those tables are not even
-- nameable by this role.

-- Future tables must NOT become agent-readable just by existing. There is no
-- ALTER DEFAULT PRIVILEGES ... GRANT for caib_agent_ro anywhere, so this is
-- already the state; the explicit REVOKE below records the intent in the same
-- file as the grants, so a later migration that adds a blanket default grant
-- reads as a contradiction rather than as an oversight. It is documentation,
-- not enforcement — enforcement is the grant-set assertion in the tests.
ALTER DEFAULT PRIVILEGES FOR ROLE caib_app IN SCHEMA caib
  REVOKE SELECT ON TABLES FROM caib_agent_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE caib_app IN SCHEMA detect
  REVOKE SELECT ON TABLES FROM caib_agent_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE caib_app IN SCHEMA ops
  REVOKE SELECT ON TABLES FROM caib_agent_ro;
