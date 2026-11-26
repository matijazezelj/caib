"""Detection-continuity probe lander for the HA failover test.

Lands envelopes that `aws_cloudtrail.root_console_login` fires on, one dedup
key (actor|src_ip) per --key, so the test can assert the alert's event_count
against the exact number of events that reached caib.events.

Exactness: every envelope has a fixed event_time and event_id, reused on
retry, so an ambiguous COMMIT that is retried dedups in the loader (its
conflict key is (event_time, dedup_hash)) instead of becoming a second row.

  --count N          one transaction of N envelopes, retried until it commits
  --rate R --out F   one envelope per 1/R s until SIGTERM; writes the number
                     committed to F. Never exits with an attempt in doubt.
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


def envelope(run: str, key: str, seq: int, ts: str) -> dict:
    return {
        "event_time": ts,
        "event_id": f"ha-detect-{run}-{key}-{seq}",
        "actor": f"ha-root-{run}-{key}",
        "src_ip": "198.51.100.200",
        "action": "ConsoleLogin",
        "outcome": "success",
        "severity": "info",
        "attrs": {"user_type": "Root", "event_source": "signin.amazonaws.com",
                  "ha_probe": run, "seq": seq},
    }


def now_ts() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def land(conn: psycopg.Connection, envs: list[dict]) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO ingest.landing (received_at, module, payload)"
            " VALUES (now(), 'aws_cloudtrail', %s)",
            [(Jsonb(e),) for e in envs],
        )
    conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--count", type=int)
    parser.add_argument("--rate", type=float)
    parser.add_argument("--out")
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args()
    if (args.count is None) == (args.rate is None):
        parser.error("exactly one of --count / --rate")

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    conn = None
    deadline = time.monotonic() + args.timeout
    seq = 0
    pending: list[dict] | None = None  # in doubt: retry exactly these
    committed = 0
    while True:
        if pending is None:
            if args.count is not None:
                if committed:
                    break
                ts = now_ts()
                pending = [envelope(args.run, args.key, seq + i, ts)
                           for i in range(args.count)]
            else:
                if stop:
                    break
                pending = [envelope(args.run, args.key, seq, now_ts())]
        try:
            if conn is None or conn.closed:
                conn = psycopg.connect(args.url, connect_timeout=3)
            land(conn, pending)
            committed += len(pending)
            seq += len(pending)
            pending = None
            if args.rate:
                time.sleep(1.0 / args.rate)
        except psycopg.Error as exc:
            print(f"probe key={args.key} seq={seq} failed ({type(exc).__name__});"
                  " retrying", flush=True)
            if conn is not None:
                try:
                    conn.close()
                except psycopg.Error:
                    pass
            conn = None
            if args.count is not None and time.monotonic() > deadline:
                sys.exit(f"probe key={args.key}: could not land {args.count} in"
                         f" {args.timeout:.0f}s")
            time.sleep(0.5)

    if args.out:
        with open(args.out, "w") as f:
            f.write(str(committed))
    print(f"probe key={args.key}: {committed} committed", flush=True)


if __name__ == "__main__":
    main()
