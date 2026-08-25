"""The provisioned dashboards are the one shipped artifact no code path
exercises: a panel whose SQL is broken, or which quietly scans the whole
retention window, only shows up in front of a user.
"""

import json
import pathlib
import re

import psycopg

ROOT = pathlib.Path(__file__).resolve().parent.parent
DASH_DIR = ROOT / "grafana" / "provisioning" / "dashboards" / "json"

# caib.events but not caib.events_5m ('_' is a word character, so \b handles it).
RAW_EVENTS_RE = re.compile(r"\bcaib\.events\b")


def panel_queries() -> list[tuple[str, str, str]]:
    out = []
    for path in sorted(DASH_DIR.glob("*.json")):
        doc = json.loads(path.read_text())
        for panel in doc["panels"]:
            for target in panel.get("targets", []):
                if sql := target.get("rawSql"):
                    out.append((path.name, panel["title"], sql))
    return out


def expand_macros(sql: str) -> str:
    """Grafana macros -> plain SQL, so the query can be EXPLAINed."""
    sql = re.sub(r"\$__timeFilter\(([^)]*)\)", r"\1 > now() - interval '1 day'", sql)
    sql = re.sub(
        r"\$__timeGroup\(([^,]*),\s*'[^']*'\)", r"time_bucket(interval '1 hour', \1)", sql
    )
    return re.sub(r"\$\{\w+:sqlstring\}", "'x'", sql)


def test_every_panel_sql_explains(migrated_db):
    queries = panel_queries()
    assert len(queries) > 20, "dashboards missing?"
    broken = []
    with psycopg.connect(migrated_db) as conn:
        for dash, title, sql in queries:
            try:
                with conn.transaction(force_rollback=True):
                    conn.execute("EXPLAIN " + expand_macros(sql))
            except psycopg.Error as exc:
                broken.append(f"{dash} / {title}: {exc}")
    assert not broken, "\n".join(broken)


def test_raw_event_panels_are_time_bounded():
    """Panels reading caib.events must carry a time predicate. This does not
    prove the range stays inside the 7-day rowstore window (the time picker owns
    that), only that no panel can walk all 90 days of chunks by construction."""
    unbounded = [
        f"{dash} / {title}"
        for dash, title, sql in panel_queries()
        if RAW_EVENTS_RE.search(sql)
        and "$__timeFilter(" not in sql
        and "event_time >" not in sql
    ]
    assert not unbounded, unbounded
