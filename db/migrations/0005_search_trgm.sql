-- Analyst search (spec §Analyst search): substring/ILIKE recall over the
-- loader-built search_text. On a hypertable this index physically exists only
-- on uncompressed chunks, so it covers the hot rowstore window by
-- construction; compressed history stays a time-bounded columnstore scan.
-- pg_trgm is a trusted extension: caib_app's CREATE on the database suffices.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX events_search_trgm ON caib.events
  USING gin (search_text gin_trgm_ops);
