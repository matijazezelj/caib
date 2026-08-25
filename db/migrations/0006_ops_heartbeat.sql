-- Liveness beacons for the long-running loops (loader, detect).
--
-- The dead-man rules cover a *source* going quiet, but nothing covered the
-- detect engine itself stopping: a stopped engine evaluates no rules, so it
-- cannot alert on its own silence. Each loop stamps its row every cycle;
-- `caib-core healthcheck` reads it back as a container healthcheck, and the
-- Overview dashboard shows the age.
CREATE TABLE ops.heartbeat (
  service text PRIMARY KEY,
  beat_at timestamptz NOT NULL DEFAULT now()
);
