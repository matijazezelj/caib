CREATE TABLE detect.rule_state (
  rule_id   text PRIMARY KEY,
  watermark timestamptz NOT NULL
);

CREATE TABLE detect.alerts (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  rule_id     text NOT NULL,
  dedup_key   text NOT NULL,
  severity    text NOT NULL,
  title       text NOT NULL,
  entity      jsonb NOT NULL DEFAULT '{}'::jsonb,
  context     jsonb NOT NULL DEFAULT '{}'::jsonb,
  first_seen  timestamptz NOT NULL,
  last_seen   timestamptz NOT NULL,
  event_count int  NOT NULL DEFAULT 1,
  status      text NOT NULL DEFAULT 'open' CHECK (status IN ('open','ack','closed')),
  UNIQUE (rule_id, dedup_key)
);
CREATE INDEX alerts_status_time ON detect.alerts (status, last_seen DESC);

-- Transactional outbox: written in the same tx as the alert upsert,
-- drained by the notify sender. Crash-safe by construction.
CREATE TABLE detect.outbox (
  id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  created_at timestamptz NOT NULL DEFAULT now(),
  alert_id   bigint NOT NULL,
  kind       text NOT NULL CHECK (kind IN ('new','recurrence','meta')),
  payload    jsonb NOT NULL,
  sent_at    timestamptz
);
CREATE INDEX outbox_unsent ON detect.outbox (id) WHERE sent_at IS NULL;
