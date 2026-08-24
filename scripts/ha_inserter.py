"""Synthetic landing inserter for the HA failover test.

At-least-once by design: the same sequence number is retried until COMMIT
succeeds, so downstream dedup makes the event count exact — ids 0..N-1 all
present, count == N, proves zero loss. On SIGTERM writes the committed count
to --out and exits.
"""

import argparse
import datetime
import signal
import sys
import time

import psycopg
from psycopg.types.json import Jsonb

stop = False


def on_term(signum, frame):
    global stop
    stop = True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--rate", type=float, default=20.0)
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    committed = 0
    seq = 0
    conn = None
    while not stop:
        try:
            if conn is None or conn.closed:
                conn = psycopg.connect(args.url, connect_timeout=3)
            ts = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            payload = {
                "event_time": ts,
                "event_id": f"ha-{seq}",
                "actor": "ha-test@example.com",
                "action": "synthetic_write",
                "outcome": "success",
                "severity": "info",
                "attrs": {"seq": seq},
            }
            conn.execute(
                "INSERT INTO ingest.landing (received_at, module, payload)"
                " VALUES (now(), 'ha_test', %s)",
                (Jsonb(payload),),
            )
            conn.commit()
            committed = seq + 1
            seq += 1
            time.sleep(1.0 / args.rate)
        except psycopg.Error as exc:
            print(f"insert seq={seq} failed ({type(exc).__name__}); retrying", flush=True)
            if conn is not None:
                try:
                    conn.close()
                except psycopg.Error:
                    conn = None
            conn = None
            time.sleep(0.5)

    with open(args.out, "w") as f:
        f.write(str(committed))
    print(f"inserter done: {committed} committed", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
