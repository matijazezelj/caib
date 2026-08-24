CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE SCHEMA IF NOT EXISTS caib;
CREATE SCHEMA IF NOT EXISTS ingest;
CREATE SCHEMA IF NOT EXISTS detect;
CREATE SCHEMA IF NOT EXISTS ops;

-- Append-only FIFO. Plain heap ON PURPOSE (never UNLOGGED: doesn't replicate;
-- never a hypertable: it's a queue). Exactly three columns; the Vector postgres
-- sink populates columns by name and absent keys become NULL — a malformed
-- remap must fail loudly on the NOT NULLs.
CREATE TABLE ingest.landing (
  received_at timestamptz NOT NULL,
  module      text        NOT NULL,
  payload     jsonb       NOT NULL
) WITH (autovacuum_vacuum_scale_factor = 0.01, autovacuum_vacuum_cost_delay = 0);
CREATE INDEX landing_received_at ON ingest.landing (received_at);

CREATE TABLE ingest.cursors (
  module     text PRIMARY KEY,
  cursor     jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- Envelopes the loader refuses. Non-empty = mapping bug; on the Overview dashboard.
CREATE TABLE ingest.dead_letter (
  received_at timestamptz NOT NULL DEFAULT now(),
  module      text  NOT NULL,
  payload     jsonb NOT NULL,
  reason      text  NOT NULL
);

CREATE FUNCTION ops.try_inet(t text) RETURNS inet
LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN RETURN t::inet; EXCEPTION WHEN OTHERS THEN RETURN NULL; END $$;

CREATE FUNCTION ops.try_tstz(t text) RETURNS timestamptz
LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN RETURN t::timestamptz; EXCEPTION WHEN OTHERS THEN RETURN NULL; END $$;

-- Space-joined values of an attrs object, for search_text construction.
CREATE FUNCTION ops.attrs_text(a jsonb) RETURNS text
LANGUAGE sql IMMUTABLE AS
$$ SELECT string_agg(value, ' ') FROM jsonb_each_text(a) $$;
