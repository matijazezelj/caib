CREATE TABLE caib.events (
  event_time  timestamptz NOT NULL,
  ingested_at timestamptz NOT NULL,
  module      text        NOT NULL,
  dedup_hash  bytea       NOT NULL,   -- sha256(module||':'||event_id-or-canonical)
  actor       text,
  src_ip      inet,
  action      text        NOT NULL,
  target      text,
  outcome     text        NOT NULL DEFAULT 'unknown'
              CHECK (outcome IN ('success','failure','unknown')),
  severity    text        NOT NULL DEFAULT 'info'
              CHECK (severity IN ('info','low','medium','high','critical')),
  attrs       jsonb       NOT NULL DEFAULT '{}'::jsonb,
  raw         jsonb,
  search_text text,                       -- loader-built; see Analyst search
  PRIMARY KEY (event_time, dedup_hash)   -- partition column must be in the PK
) WITH (toast_tuple_target = 256);       -- push jsonb out of line: narrow hot rows

SELECT create_hypertable('caib.events', 'event_time',
                         chunk_time_interval => INTERVAL '1 day');

CREATE INDEX events_module_time ON caib.events (module, event_time DESC);
CREATE INDEX events_actor_time  ON caib.events (actor, event_time DESC);
CREATE INDEX events_ingested_brin ON caib.events USING brin (ingested_at);
-- No GIN on attrs unless a detection provably needs it; ad-hoc search goes
-- through search_text (see Analyst search). Its index arrives in Phase 8.

ALTER TABLE caib.events SET (
  timescaledb.compress,
  timescaledb.compress_segmentby = 'module',
  timescaledb.compress_orderby   = 'event_time DESC'
);
SELECT add_compression_policy('caib.events', compress_after => INTERVAL '7 days');
SELECT add_retention_policy  ('caib.events', drop_after     => INTERVAL '90 days');
