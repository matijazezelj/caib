-- Supporting indexes for the plain-table retention sweep (caib_core.retention).
--
-- caib.events is a hypertable and carries an add_retention_policy. detect.outbox,
-- detect.alerts and ingest.dead_letter are plain heaps, so no Timescale policy
-- applies to them and nothing bounded their growth. The sweep is three DELETEs;
-- these indexes are what keep each one an index scan over the tail instead of a
-- seq scan over the whole table.

-- outbox_unsent already covers the drain's question (sent_at IS NULL); the sweep
-- asks the opposite one.
CREATE INDEX outbox_sent_at ON detect.outbox (sent_at) WHERE sent_at IS NOT NULL;

-- alerts_status_time (status, last_seen DESC) would serve this too, but the
-- partial index stays tiny: closed is the only status the sweep may touch.
CREATE INDEX alerts_closed_last_seen ON detect.alerts (last_seen) WHERE status = 'closed';

CREATE INDEX dead_letter_received_at ON ingest.dead_letter (received_at);

-- When this alert last had an outbox row enqueued for it. A rule that overflows
-- its rate cap on every tick keeps upserting the same 'overflow' dedup_key, so
-- every overflow after the first folded in as a 'recurrence' and was never
-- notified — the alert you least want to hear once and never again. The engine
-- now re-notifies the meta alert on an interval, and this is the clock it reads.
ALTER TABLE detect.alerts ADD COLUMN last_notified_at timestamptz;
