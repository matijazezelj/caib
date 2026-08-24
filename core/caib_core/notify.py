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


def drain(
    conn: psycopg.Connection, webhook_url: str, batch: int = BATCH, fmt: str = "json"
) -> tuple[int, int]:
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
            body = slack_message(payload) if fmt == "slack" else payload
            if webhook_url and not _post(webhook_url, body):
                failed = len(rows) - len(sent_ids)
                break
            sent_ids.append(outbox_id)
        if sent_ids:
            conn.execute(
                "UPDATE detect.outbox SET sent_at = now() WHERE id = ANY(%s)",
                (sent_ids,),
            )
    return len(sent_ids), failed
