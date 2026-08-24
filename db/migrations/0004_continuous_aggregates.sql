-- caib:no-transaction
-- Continuous aggregates cannot be created inside a transaction block, so this
-- file runs statement-by-statement in autocommit. A crash mid-file re-runs the
-- whole file: every statement here MUST be idempotent (IF NOT EXISTS).

CREATE MATERIALIZED VIEW IF NOT EXISTS caib.events_5m
WITH (timescaledb.continuous) AS
SELECT time_bucket('5 minutes', event_time) AS bucket,
       module, action, outcome, count(*) AS n
FROM caib.events GROUP BY 1, 2, 3, 4
WITH NO DATA;

CREATE MATERIALIZED VIEW IF NOT EXISTS caib.actors_1h
WITH (timescaledb.continuous) AS
SELECT time_bucket('1 hour', event_time) AS bucket,
       module, actor, count(*) AS n,
       count(*) FILTER (WHERE outcome = 'failure') AS failures
FROM caib.events GROUP BY 1, 2, 3
WITH NO DATA;

SELECT add_continuous_aggregate_policy('caib.events_5m',
  start_offset => INTERVAL '1 day', end_offset => INTERVAL '5 minutes',
  schedule_interval => INTERVAL '5 minutes', if_not_exists => true);

SELECT add_continuous_aggregate_policy('caib.actors_1h',
  start_offset => INTERVAL '1 day', end_offset => INTERVAL '1 hour',
  schedule_interval => INTERVAL '1 hour', if_not_exists => true);

-- Retention on the caggs themselves: 400 days (cheap; outlives raw).
SELECT add_retention_policy('caib.events_5m', drop_after => INTERVAL '400 days',
                            if_not_exists => true);
SELECT add_retention_policy('caib.actors_1h', drop_after => INTERVAL '400 days',
                            if_not_exists => true);
