"""Sigma -> CAIB rule converter, existence shape only (spec Phase 8).

Supported Sigma subset (the shape of most public cloud rules):
- detection selections that are field maps (AND across fields); a list value
  for a field is OR within that field; a list of maps is OR across maps
- field modifiers: contains, startswith, endswith; `*` wildcards in plain
  values translate to LIKE
- condition strings of the form `sel`, `sel1 and sel2`, `sel and not filter`
  (chained and / and not; no `or`, no `1 of`, no parentheses)

Anything else raises UnsupportedSigma with a reason — convert those by hand.
Threshold/absence shapes are deliberately out of scope.

Usage:
  uv run python -m caib_core.sigma --module aws_cloudtrail path/to/sigma.yml \
      > detect/rules/aws_cloudtrail/<name>.yaml
"""

import argparse
import pathlib
import re
import sys

import yaml

# Sigma field -> SQL expression, per module. Only documented attrs (MAPPING.md).
FIELD_MAPS: dict[str, dict[str, str]] = {
    "aws_cloudtrail": {
        "eventName": "action",
        "eventSource": "attrs->>'event_source'",
        "awsRegion": "attrs->>'aws_region'",
        "errorCode": "attrs->>'error_code'",
        "sourceIPAddress": "host(src_ip)",
        "userIdentity.type": "attrs->>'user_type'",
        "userIdentity.arn": "actor",
        "eventType": "raw->>'eventType'",
    },
    "gcp_audit": {
        "gcp.audit.method_name": "action",
        "methodName": "action",
        "gcp.audit.principal_email": "actor",
        "principalEmail": "actor",
        "serviceName": "attrs->>'service_name'",
        "resourceName": "target",
    },
    "azure_activity": {
        "operationName": "action",
        "caller": "actor",
        "resourceId": "target",
        "category": "attrs->>'category'",
        "status": "attrs->>'status'",
    },
    "cloudflare_audit": {
        "action": "action",
        "actor": "actor",
        "resourceType": "attrs->>'resource_type'",
    },
}

SEVERITY_MAP = {
    "informational": "info",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "critical": "critical",
}

CONDITION_RE = re.compile(r"^\s*(\w+)(\s+and\s+(not\s+)?\w+)*\s*$", re.IGNORECASE)


class UnsupportedSigma(ValueError):
    pass


def _sql_literal(value) -> str:
    # psycopg named-param queries need literal % doubled.
    return str(value).replace("'", "''").replace("%", "%%")


def _field_condition(sql_expr: str, raw_field: str, value) -> str:
    parts = raw_field.split("|")
    modifiers = parts[1:]
    values = value if isinstance(value, list) else [value]
    if any(m not in ("contains", "startswith", "endswith", "all") for m in modifiers):
        raise UnsupportedSigma(f"unsupported modifiers on {raw_field}: {modifiers}")

    joiner = " AND " if "all" in modifiers else " OR "
    conds = []
    for v in values:
        if v is None:
            conds.append(f"{sql_expr} IS NULL")
            continue
        lit = _sql_literal(v)
        if "contains" in modifiers:
            conds.append(f"{sql_expr} LIKE '%%{lit}%%'")
        elif "startswith" in modifiers:
            conds.append(f"{sql_expr} LIKE '{lit}%%'")
        elif "endswith" in modifiers:
            conds.append(f"{sql_expr} LIKE '%%{lit}'")
        elif "*" in str(v):
            conds.append(f"{sql_expr} LIKE '{lit.replace('*', '%%')}'")
        else:
            conds.append(f"{sql_expr} = '{lit}'")
    body = joiner.join(conds)
    return f"({body})" if len(conds) > 1 else body


def _selection_sql(selection, field_map: dict[str, str]) -> str:
    if isinstance(selection, list):  # OR across maps
        parts = [_selection_sql(m, field_map) for m in selection]
        return "(" + " OR ".join(parts) + ")"
    if not isinstance(selection, dict):
        raise UnsupportedSigma(f"selection is not a map or list of maps: {selection!r}")
    conds = []
    for raw_field, value in selection.items():
        base_field = raw_field.split("|")[0]
        sql_expr = field_map.get(base_field)
        if sql_expr is None:
            raise UnsupportedSigma(
                f"no field mapping for {base_field!r} — extend FIELD_MAPS or map the"
                " attr in the module's collector first"
            )
        conds.append(_field_condition(sql_expr, raw_field, value))
    return "(" + " AND ".join(conds) + ")"


def convert(sigma: dict, module: str, interval: int = 60) -> dict:
    if module not in FIELD_MAPS:
        raise UnsupportedSigma(f"no field map for module {module!r}")
    field_map = FIELD_MAPS[module]
    detection = sigma.get("detection")
    if not detection:
        raise UnsupportedSigma("sigma rule has no detection section")
    condition = detection.get("condition", "selection")
    if not CONDITION_RE.match(condition or "") or " or " in condition.lower():
        raise UnsupportedSigma(
            f"unsupported condition {condition!r} (only `sel [and [not] sel]...`)"
        )

    tokens = condition.split()
    clauses: list[str] = []
    negate_next = False
    expect_name = True
    for tok in tokens:
        low = tok.lower()
        if low == "and":
            expect_name = True
            continue
        if low == "not":
            negate_next = True
            continue
        if not expect_name:
            raise UnsupportedSigma(f"unsupported condition {condition!r}")
        if tok not in detection:
            raise UnsupportedSigma(f"condition references unknown selection {tok!r}")
        sql = _selection_sql(detection[tok], field_map)
        # Sigma `not X` means "X does not match"; a missing field is a
        # non-match. SQL NULLs would swallow the row without the coalesce.
        clauses.append(f"NOT coalesce({sql}, false)" if negate_next else sql)
        negate_next = False
        expect_name = False

    where = "\n    AND ".join(clauses)
    title = sigma.get("title", "Converted Sigma rule")
    slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:48]
    severity = SEVERITY_MAP.get(str(sigma.get("level", "medium")).lower(), "medium")

    rule_sql = f"""SELECT
  coalesce(actor, '-')                         AS dedup_key,
  jsonb_build_object('actor', actor)           AS entity,
  jsonb_build_object('events', count(*)::int,
                     'actions', jsonb_agg(DISTINCT action)) AS context,
  max(event_time)                              AS last_event_time
FROM caib.events
WHERE module = '{module}'
  AND ingested_at > %(wm_from)s AND ingested_at <= %(wm_to)s
  AND {where}
GROUP BY actor
"""
    return {
        "id": f"{module}.sigma_{slug}",
        "enabled": True,
        "module": module,
        "title": title,
        "severity": severity,
        "interval": interval,
        "lookback": "15m",
        "sql": rule_sql,
        "_sigma_id": sigma.get("id"),  # stripped before dump, emitted as comment
    }


def to_yaml(rule: dict) -> str:
    sigma_id = rule.pop("_sigma_id", None)
    header = f"# Converted from Sigma rule {sigma_id}\n" if sigma_id else ""
    header += "# Generated by caib_core.sigma — review before enabling in production.\n"
    return header + yaml.safe_dump(rule, sort_keys=False, default_style=None)


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m caib_core.sigma")
    parser.add_argument("sigma_file", type=pathlib.Path)
    parser.add_argument("--module", required=True, choices=sorted(FIELD_MAPS))
    parser.add_argument("--interval", type=int, default=60)
    args = parser.parse_args()
    sigma = yaml.safe_load(args.sigma_file.read_text())
    try:
        rule = convert(sigma, args.module, args.interval)
    except UnsupportedSigma as exc:
        raise SystemExit(f"cannot convert: {exc}") from exc
    sys.stdout.write(to_yaml(rule))


if __name__ == "__main__":
    main()
