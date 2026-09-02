"""Detection engine.

Rules are YAML + SQL in detect/rules/**/*.yaml, loaded at start and on SIGHUP,
validated with EXPLAIN before the loop starts (a broken rule blocks startup).

Per rule, per tick, ONE transaction does: rule SQL over the ingested_at
watermark slice -> rate-cap check -> alert upserts -> outbox enqueue ->
watermark advance. A crash anywhere inside rolls the whole window back; the
re-run folds into the same alerts and enqueues once — no double-alerts, no
double-notifications, by construction.
"""

import argparse
import dataclasses
import datetime
import logging
import os
import pathlib
import re
import signal
import time

import psycopg
import yaml
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from caib_core import heartbeat, notify, retention

log = logging.getLogger("caib.detect")

SEVERITIES = ("info", "low", "medium", "high", "critical")

RULE_REQUIRED = ("id", "module", "title", "severity", "interval", "sql")
RULE_ALLOWED = set(RULE_REQUIRED) | {"enabled", "lookback", "notify_recurrence"}

LOOKBACK_RE = re.compile(r"^(\d+)\s*([smhd])$")
LOOKBACK_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}

UPSERT_SQL = """
INSERT INTO detect.alerts (rule_id, dedup_key, severity, title, entity, context,
                           first_seen, last_seen, event_count)
VALUES (%(rule_id)s, %(dedup_key)s, %(severity)s, %(title)s, %(entity)s, %(context)s,
        %(seen)s, %(seen)s, %(event_count)s)
ON CONFLICT (rule_id, dedup_key) DO UPDATE SET
  last_seen   = GREATEST(detect.alerts.last_seen, EXCLUDED.last_seen),
  event_count = detect.alerts.event_count + EXCLUDED.event_count,
  context     = EXCLUDED.context,
  status      = CASE WHEN detect.alerts.status = 'closed' THEN 'open'
                     ELSE detect.alerts.status END
RETURNING id, (xmax = 0) AS is_new, first_seen, last_seen, event_count
"""


@dataclasses.dataclass
class Rule:
    id: str
    module: str
    title: str
    severity: str
    interval_s: int
    sql: str
    lookback: str | None = None
    enabled: bool = True
    notify_recurrence: bool = False

    @property
    def wrapped_sql(self) -> str:
        return f"WITH r AS (\n{self.sql}\n) SELECT * FROM r LIMIT %(cap)s"


@dataclasses.dataclass
class Cfg:
    safety_lag_s: float = 60.0
    max_alerts_per_tick: int = 500
    webhook_url: str = ""
    min_severity: str = "info"
    tick_s: float = 15.0
    first_run_lookback_s: float = 3600.0
    notify_format: str = "json"

    @classmethod
    def from_env(cls) -> "Cfg":
        webhook_url = os.environ.get("CAIB_NOTIFY_WEBHOOK_URL", "")
        min_severity = os.environ.get("CAIB_NOTIFY_MIN_SEVERITY", "info")
        if min_severity not in SEVERITIES:
            # Fail here, not at the first alert: _severity_notifies() would
            # raise ValueError out of a rule's transaction hours later, and the
            # engine would look healthy until something finally fired.
            raise SystemExit(
                f"CAIB_NOTIFY_MIN_SEVERITY={min_severity!r} is not one of "
                f"{'|'.join(SEVERITIES)}"
            )
        return cls(
            safety_lag_s=float(os.environ.get("CAIB_DETECT_SAFETY_LAG", "60")),
            max_alerts_per_tick=int(os.environ.get("CAIB_DETECT_MAX_ALERTS_PER_TICK", "500")),
            webhook_url=webhook_url,
            min_severity=min_severity,
            tick_s=float(os.environ.get("CAIB_DETECT_TICK_S", "15")),
            notify_format=notify.resolve_format(
                os.environ.get("CAIB_NOTIFY_FORMAT", ""), webhook_url
            ),
        )


def lookback_interval(raw: str) -> str:
    """'15m' -> '15 minutes'; anything else passes through for ::interval."""
    if m := LOOKBACK_RE.match(raw.strip()):
        return f"{m.group(1)} {LOOKBACK_UNITS[m.group(2)]}"
    return raw


def parse_rule(path: pathlib.Path) -> Rule:
    doc = yaml.safe_load(path.read_text())
    if not isinstance(doc, dict):
        raise TypeError(f"{path}: not a mapping")
    missing = [k for k in RULE_REQUIRED if k not in doc]
    if missing:
        raise ValueError(f"{path}: missing {missing}")
    unknown = set(doc) - RULE_ALLOWED
    if unknown:
        raise ValueError(f"{path}: unknown keys {sorted(unknown)}")
    if doc["severity"] not in SEVERITIES:
        raise ValueError(f"{path}: bad severity {doc['severity']!r}")
    if not isinstance(doc["interval"], int) or doc["interval"] <= 0:
        raise ValueError(f"{path}: interval must be a positive int (seconds)")
    return Rule(
        id=doc["id"],
        module=doc["module"],
        title=doc["title"],
        severity=doc["severity"],
        interval_s=doc["interval"],
        sql=doc["sql"],
        lookback=doc.get("lookback"),
        enabled=doc.get("enabled", True),
        notify_recurrence=doc.get("notify_recurrence", False),
    )


def load_rules(rules_dir: pathlib.Path) -> list[Rule]:
    rules = [parse_rule(p) for p in sorted(rules_dir.glob("**/*.yaml"))]
    ids = [r.id for r in rules]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"duplicate rule ids: {sorted(dupes)}")
    return rules


def validate_rules(conn: psycopg.Connection, rules: list[Rule]) -> None:
    now = datetime.datetime.now(datetime.UTC)
    for rule in rules:
        params = {
            "wm_from": now - datetime.timedelta(hours=1),
            "wm_to": now,
            "lookback": lookback_interval(rule.lookback or "15m"),
            "cap": 1,
        }
        try:
            with conn.transaction(force_rollback=True):
                conn.execute("EXPLAIN " + rule.wrapped_sql, params)
        except psycopg.Error as exc:
            raise SystemExit(f"rule {rule.id}: SQL does not validate: {exc}") from exc


def _severity_notifies(cfg: Cfg, severity: str) -> bool:
    return SEVERITIES.index(severity) >= SEVERITIES.index(cfg.min_severity)


def _enqueue(conn: psycopg.Connection, alert_id: int, kind: str, payload: dict) -> None:
    conn.execute(
        "INSERT INTO detect.outbox (alert_id, kind, payload) VALUES (%s, %s, %s)",
        (alert_id, kind, Jsonb(payload)),
    )


def _set_watermark(conn: psycopg.Connection, rule_id: str, wm_to: datetime.datetime) -> None:
    conn.execute(
        "INSERT INTO detect.rule_state (rule_id, watermark) VALUES (%s, %s)"
        " ON CONFLICT (rule_id) DO UPDATE SET watermark = EXCLUDED.watermark",
        (rule_id, wm_to),
    )


def _upsert_alert(
    conn: psycopg.Connection, rule: Rule, cfg: Cfg, row: dict, kind_new: str = "new"
) -> None:
    context = row.get("context") or {}
    event_count = context.get("events") if isinstance(context.get("events"), int) else 1
    rec = conn.execute(
        UPSERT_SQL,
        {
            "rule_id": rule.id,
            "dedup_key": str(row["dedup_key"]),
            "severity": rule.severity,
            "title": rule.title,
            "entity": Jsonb(row.get("entity") or {}),
            "context": Jsonb(context),
            "seen": row["last_event_time"],
            "event_count": event_count,
        },
    ).fetchone()
    alert_id, is_new, first_seen, last_seen, total = rec
    kind = kind_new if is_new else "recurrence"
    wants_notify = (kind != "recurrence") or rule.notify_recurrence
    if wants_notify and _severity_notifies(cfg, rule.severity):
        _enqueue(
            conn,
            alert_id,
            kind,
            {
                "alert_id": alert_id,
                "rule_id": rule.id,
                "module": rule.module,
                "title": rule.title,
                "severity": rule.severity,
                "kind": kind,
                "dedup_key": str(row["dedup_key"]),
                "entity": row.get("entity") or {},
                "context": context,
                "first_seen": first_seen.isoformat(),
                "last_seen": last_seen.isoformat(),
                "event_count": total,
            },
        )


def evaluate_rule(conn: psycopg.Connection, rule: Rule, cfg: Cfg) -> dict | None:
    """One watermark window for one rule, in one transaction. Returns stats or
    None when the window hasn't opened yet."""
    with conn.transaction():
        now = conn.execute("SELECT now()").fetchone()[0]
        state = conn.execute(
            "SELECT watermark FROM detect.rule_state WHERE rule_id = %s FOR UPDATE",
            (rule.id,),
        ).fetchone()
        wm_from = state[0] if state else now - datetime.timedelta(
            seconds=cfg.first_run_lookback_s
        )
        wm_to = now - datetime.timedelta(seconds=cfg.safety_lag_s)
        if wm_to <= wm_from:
            return None

        params = {
            "wm_from": wm_from,
            "wm_to": wm_to,
            "lookback": lookback_interval(rule.lookback or "15m"),
            "cap": cfg.max_alerts_per_tick + 1,
        }
        with conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(rule.wrapped_sql, params).fetchall()

        overflow = len(rows) > cfg.max_alerts_per_tick
        if overflow:
            # One meta-alert instead of thousands; the watermark still advances.
            _upsert_alert(
                conn,
                rule,
                cfg,
                {
                    "dedup_key": "overflow",
                    "entity": {"rule_id": rule.id},
                    "context": {
                        "rows": len(rows),
                        "wm_from": wm_from.isoformat(),
                        "wm_to": wm_to.isoformat(),
                    },
                    "last_event_time": now,
                },
                kind_new="meta",
            )
        else:
            for row in rows:
                _upsert_alert(conn, rule, cfg, row)
        _set_watermark(conn, rule.id, wm_to)
    return {"rows": len(rows), "overflow": overflow, "wm_to": wm_to}


def _connection_lost(conn: psycopg.Connection) -> bool:
    return bool(conn.closed or conn.broken)


def advance_watermarks(conn: psycopg.Connection, rules: list[Rule]) -> None:
    """Backfill mode: mark everything already ingested as evaluated. Mandatory
    after any historical import, or every rule fires on day one."""
    with conn.transaction():
        now = conn.execute("SELECT now()").fetchone()[0]
        for rule in rules:
            _set_watermark(conn, rule.id, now)
    log.info("advanced %d watermarks to %s", len(rules), now)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    parser = argparse.ArgumentParser(prog="caib-core detect")
    parser.add_argument("--once", action="store_true",
                        help="evaluate every enabled rule once, drain outbox, exit")
    parser.add_argument("--advance-watermarks", action="store_true",
                        help="set all watermarks to now() without evaluating")
    args = parser.parse_args(argv)

    cfg = Cfg.from_env()
    retention_cfg = retention.Cfg.from_env()
    rules_dir = pathlib.Path(os.environ.get("CAIB_RULES_DIR", "detect/rules"))
    rules = load_rules(rules_dir)
    log.info("loaded %d rules from %s", len(rules), rules_dir)

    reload_requested = False

    def on_sighup(signum, frame):
        nonlocal reload_requested
        reload_requested = True

    signal.signal(signal.SIGHUP, on_sighup)

    with psycopg.connect(os.environ["CAIB_PG_URL"]) as conn:
        validate_rules(conn, rules)
        if args.advance_watermarks:
            advance_watermarks(conn, rules)
            return

        next_run: dict[str, float] = {}
        next_retention = 0.0  # sweep on the first tick, then hourly
        notify_backoff = 1.0
        while True:
            if reload_requested:
                reload_requested = False
                try:
                    fresh = load_rules(rules_dir)
                    validate_rules(conn, fresh)
                    rules = fresh
                    log.info("reloaded %d rules", len(rules))
                except SystemExit as exc:
                    # validate_rules' own failure mode; it carries a message.
                    log.error("rule reload failed, keeping old set: %s", exc)
                except Exception:
                    # Anything else the rules directory can throw — a YAML
                    # syntax error, an unreadable file, a bad type — must not
                    # take detection down with it. Only ValueError and
                    # SystemExit used to be caught, so a stray tab in a rule
                    # file killed a running engine.
                    log.exception("rule reload failed, keeping old set")

            now_mono = time.monotonic()
            for rule in rules:
                if not rule.enabled:
                    continue
                if not args.once and now_mono < next_run.get(rule.id, 0):
                    continue
                try:
                    stats = evaluate_rule(conn, rule, cfg)
                    if stats and stats["rows"]:
                        log.info("rule=%s rows=%d overflow=%s",
                                 rule.id, stats["rows"], stats["overflow"])
                except psycopg.Error:
                    # A rule's own SQL failing is survivable — its window retries.
                    # A dead connection is not: every later tick would fail the
                    # same way while the process stayed up, so detection would
                    # stop with nothing to notice it. Crash and let compose
                    # restart us, like the loader and the pollers do.
                    if _connection_lost(conn):
                        raise
                    log.exception("rule %s failed; window will be retried", rule.id)
                next_run[rule.id] = now_mono + rule.interval_s

            try:
                _sent, failed = notify.drain(conn, cfg.webhook_url, fmt=cfg.notify_format)
                notify_backoff = 1.0 if not failed else min(notify_backoff * 2, 300)
                if failed:
                    log.warning("notify: %d failed, backoff %.0fs", failed, notify_backoff)
            except psycopg.Error:
                if _connection_lost(conn):
                    raise
                log.exception("notify drain failed")

            if now_mono >= next_retention:
                next_retention = now_mono + retention.SWEEP_INTERVAL_S
                try:
                    deleted = retention.sweep(conn, retention_cfg)
                    if any(deleted.values()):
                        log.info(
                            "retention: %s",
                            " ".join(f"{k}={v}" for k, v in deleted.items()),
                        )
                except psycopg.Error:
                    if _connection_lost(conn):
                        raise
                    log.exception("retention sweep failed")

            heartbeat.beat(conn, "detect")

            if args.once:
                return
            time.sleep(cfg.tick_s if notify_backoff == 1.0 else notify_backoff)
