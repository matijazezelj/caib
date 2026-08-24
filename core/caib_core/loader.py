"""Move ingest.landing -> caib.events in batches.

The loader is the ONLY writer to caib.events. Dedup happens here (ON CONFLICT
DO NOTHING on the sha256 PK) so nothing upstream can fail on conflicts. Rows
that violate the envelope contract (bad event_time, out-of-enum outcome or
severity, non-object attrs) go to ingest.dead_letter instead of poisoning the
batch: one bad envelope must not crash-loop the loader.
"""

import argparse
import logging
import os
import time

import psycopg

log = logging.getLogger("caib.loader")

BATCH_SQL = """
WITH batch AS (
  SELECT ctid FROM ingest.landing
  ORDER BY received_at
  LIMIT %(batch)s
  FOR UPDATE SKIP LOCKED
), moved AS (
  DELETE FROM ingest.landing l USING batch b
  WHERE l.ctid = b.ctid
  RETURNING l.module, l.payload
), parsed AS (
  SELECT module, payload,
         ops.try_tstz(payload->>'event_time') AS et,
         coalesce(payload->>'outcome', 'unknown')  AS outcome,
         coalesce(payload->>'severity', 'info')    AS severity,
         coalesce(payload->'attrs', '{}'::jsonb)   AS attrs
  FROM moved
), checked AS (
  SELECT *,
         CASE
           WHEN et IS NULL THEN 'unparseable event_time'
           WHEN outcome NOT IN ('success','failure','unknown') THEN 'invalid outcome'
           WHEN severity NOT IN ('info','low','medium','high','critical') THEN 'invalid severity'
           WHEN jsonb_typeof(attrs) <> 'object' THEN 'attrs not an object'
         END AS reject_reason
  FROM parsed
), ins AS (
  INSERT INTO caib.events (event_time, ingested_at, module, dedup_hash, actor, src_ip,
                           action, target, outcome, severity, attrs, raw, search_text)
  SELECT et, now(), module,
         sha256(convert_to(module || ':' ||
           coalesce(payload->>'event_id',
                    coalesce(payload->>'event_time','') || '|' ||
                    coalesce(payload->>'actor','')      || '|' ||
                    coalesce(payload->>'action','')     || '|' ||
                    coalesce(payload->>'target','')), 'UTF8')),
         payload->>'actor',
         ops.try_inet(payload->>'src_ip'),
         coalesce(payload->>'action', 'unknown'),
         payload->>'target',
         outcome,
         severity,
         attrs,
         CASE WHEN %(drop_raw)s THEN NULL ELSE payload->'raw' END,
         lower(concat_ws(' ',
           payload->>'actor', payload->>'action', payload->>'target',
           payload->>'src_ip', payload->>'outcome',
           ops.attrs_text(attrs)))
  FROM checked WHERE reject_reason IS NULL
  ON CONFLICT (event_time, dedup_hash) DO NOTHING
  RETURNING 1
), dead AS (
  INSERT INTO ingest.dead_letter (module, payload, reason)
  SELECT module, payload, reject_reason FROM checked WHERE reject_reason IS NOT NULL
  RETURNING 1
)
SELECT (SELECT count(*) FROM moved)::int AS moved,
       (SELECT count(*) FROM ins)::int   AS inserted,
       (SELECT count(*) FROM dead)::int  AS dead
"""


def run_batch(
    conn: psycopg.Connection, batch_size: int, drop_raw: bool
) -> tuple[int, int, int]:
    """One loader iteration in one transaction. Returns (moved, inserted, dead);
    moved - inserted - dead = duplicates silently dropped."""
    with conn.transaction():
        row = conn.execute(
            BATCH_SQL, {"batch": batch_size, "drop_raw": drop_raw}
        ).fetchone()
    return row[0], row[1], row[2]


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    parser = argparse.ArgumentParser(prog="caib-core loader")
    parser.add_argument(
        "--once", action="store_true", help="drain landing completely, then exit"
    )
    args = parser.parse_args(argv)

    batch_size = int(os.environ.get("CAIB_LOADER_BATCH", "5000"))
    interval_s = float(os.environ.get("CAIB_LOADER_INTERVAL_S", "5"))
    drop_raw = os.environ.get("CAIB_DROP_RAW", "").lower() in ("1", "true", "yes")

    with psycopg.connect(os.environ["CAIB_PG_URL"]) as conn:
        while True:
            moved, inserted, dead = run_batch(conn, batch_size, drop_raw)
            if moved:
                log.info(
                    "moved=%d inserted=%d dead=%d dupes=%d",
                    moved, inserted, dead, moved - inserted - dead,
                )
            if moved == batch_size:
                continue  # backlog: drain without sleeping
            if args.once:
                return
            time.sleep(interval_s)
