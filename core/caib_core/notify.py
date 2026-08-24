"""Outbox drain. Rows are written in the same transaction as their alert
upsert; this loop only delivers them. A blank webhook URL means notifications
are disabled: rows are marked sent without a POST so the outbox can't grow
unbounded."""

import json
import logging
import urllib.error
import urllib.request

import psycopg

log = logging.getLogger("caib.notify")

BATCH = 50
TIMEOUT_S = 10


def _post(url: str, payload: dict) -> bool:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log.warning("webhook POST failed: %s", exc)
        return False


def drain(conn: psycopg.Connection, webhook_url: str, batch: int = BATCH) -> tuple[int, int]:
    """Deliver up to `batch` unsent outbox rows in id order. Returns
    (sent, failed). Stops at the first failure so delivery stays ordered and a
    down webhook isn't hammered; unsent rows retry on the next drain."""
    with conn.transaction():
        rows = conn.execute(
            "SELECT id, payload FROM detect.outbox WHERE sent_at IS NULL"
            " ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED",
            (batch,),
        ).fetchall()
        sent_ids: list[int] = []
        failed = 0
        for outbox_id, payload in rows:
            if webhook_url and not _post(webhook_url, payload):
                failed = len(rows) - len(sent_ids)
                break
            sent_ids.append(outbox_id)
        if sent_ids:
            conn.execute(
                "UPDATE detect.outbox SET sent_at = now() WHERE id = ANY(%s)",
                (sent_ids,),
            )
    return len(sent_ids), failed
