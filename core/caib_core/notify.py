"""Outbox drain. Rows are written in the same transaction as their alert
upsert; this loop only delivers them, claiming a batch and settling it in two
short transactions with the webhook POSTs in between (see `drain`). A blank
webhook URL means notifications are disabled: rows are marked sent without a
POST so the outbox can't grow unbounded."""

import json
import logging
import urllib.error
import urllib.request

import psycopg

log = logging.getLogger("caib.notify")

BATCH = 50
TIMEOUT_S = 10

# A claimed-but-unsent row whose claim is older than this belonged to a sender
# that died between claiming and settling. Comfortably longer than a batch of
# TIMEOUT_S POSTs would need if every one of them timed out.
CLAIM_TIMEOUT = "5 minutes"

CLAIM_SQL = """
WITH batch AS (
  SELECT id FROM detect.outbox
  WHERE sent_at IS NULL
    AND (claimed_at IS NULL OR claimed_at < now() - %(timeout)s::interval)
  ORDER BY id
  LIMIT %(batch)s
  FOR UPDATE SKIP LOCKED
)
UPDATE detect.outbox o SET claimed_at = now()
FROM batch b WHERE o.id = b.id
RETURNING o.id, o.payload
"""

SEVERITY_EMOJI = {
    "critical": "🟥",
    "high": "🟧",
    "medium": "🟨",
    "low": "🟦",
    "info": "⬜",
}


def resolve_format(fmt_env: str, webhook_url: str) -> str:
    """'json' (default) or 'slack'. Unset auto-detects Slack incoming webhooks
    from the URL so the common case needs no extra config."""
    if fmt_env in ("json", "slack"):
        return fmt_env
    return "slack" if "hooks.slack.com" in webhook_url else "json"


def slack_message(payload: dict) -> dict:
    """Render an outbox payload as Slack Block Kit. `text` is the notification
    fallback; blocks carry the details."""
    severity = payload.get("severity", "info")
    emoji = SEVERITY_EMOJI.get(severity, "⬜")
    title = payload.get("title", "CAIB alert")
    kind = payload.get("kind", "new")
    fields = [
        f"*Severity:* {severity}",
        f"*Module:* {payload.get('module', '-')}",
        f"*Rule:* `{payload.get('rule_id', '-')}`",
        f"*Events:* {payload.get('event_count', 1)}",
        f"*First seen:* {payload.get('first_seen', '-')}",
        f"*Last seen:* {payload.get('last_seen', '-')}",
    ]
    detail = json.dumps(
        {"entity": payload.get("entity", {}), "context": payload.get("context", {})},
        indent=2,
    )
    if len(detail) > 2800:  # Slack section text cap is 3000 chars
        detail = detail[:2800] + "\n…"
    return {
        "text": f"[{severity}] {title}",
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"{emoji} {title}", "emoji": True},
            },
            {
                "type": "section",
                "fields": [{"type": "mrkdwn", "text": f} for f in fields],
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"```{detail}```"},
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"kind: {kind} · dedup: `{payload.get('dedup_key', '-')}` · caib",
                    }
                ],
            },
        ],
    }


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


def _claim(conn: psycopg.Connection, batch: int) -> list[tuple[int, dict]]:
    """Take a batch out for delivery in one short transaction."""
    with conn.transaction():
        rows = conn.execute(
            CLAIM_SQL, {"batch": batch, "timeout": CLAIM_TIMEOUT}
        ).fetchall()
    # UPDATE ... RETURNING has no defined row order and delivery order is a
    # promise the caller relies on, so sort the batch back into id order.
    return sorted(rows, key=lambda row: row[0])


def _settle(
    conn: psycopg.Connection, sent_ids: list[int], unsent_ids: list[int]
) -> None:
    """Record the outcome in one short transaction. Releasing the claim on the
    rows we did not send makes them immediately retryable, rather than making
    the next drain wait out CLAIM_TIMEOUT for a sender that is right here."""
    with conn.transaction():
        if sent_ids:
            conn.execute(
                "UPDATE detect.outbox SET sent_at = now(), claimed_at = NULL"
                " WHERE id = ANY(%s)",
                (sent_ids,),
            )
        if unsent_ids:
            conn.execute(
                "UPDATE detect.outbox SET claimed_at = NULL WHERE id = ANY(%s)",
                (unsent_ids,),
            )


def drain(
    conn: psycopg.Connection, webhook_url: str, batch: int = BATCH, fmt: str = "json"
) -> tuple[int, int]:
    """Deliver up to `batch` unsent outbox rows in id order. Returns
    (sent, failed). Stops at the first failure so delivery stays ordered and a
    down webhook isn't hammered; unsent rows retry on the next drain.

    Claim, POST, settle — three steps, and the POSTs are deliberately outside
    both transactions. Doing them inside one (as this used to) held FOR UPDATE
    on the batch and an idle-in-transaction connection for as long as the
    webhook took to answer, which on a hung endpoint is TIMEOUT_S per row: long
    enough to block autovacuum and to matter on a failover. The cost is that a
    sender crashing mid-flight leaves rows claimed; those come back after
    CLAIM_TIMEOUT. At-least-once, as before — a POST that succeeds and then
    fails to record can be redelivered.

    Call this with no transaction of the caller's own open, or the POSTs land
    back inside it: psycopg turns a nested transaction() into a savepoint.
    """
    rows = _claim(conn, batch)
    sent_ids: list[int] = []
    failed = 0
    for outbox_id, payload in rows:
        body = slack_message(payload) if fmt == "slack" else payload
        if webhook_url and not _post(webhook_url, body):
            failed = len(rows) - len(sent_ids)
            break
        sent_ids.append(outbox_id)
    _settle(conn, sent_ids, [row[0] for row in rows[len(sent_ids):]])
    return len(sent_ids), failed
