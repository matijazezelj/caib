# Detection rules

One YAML file per rule, `detect/rules/<module>/<name>.yaml`.

## File schema

```yaml
id: aws_cloudtrail.root_console_login    # <module>.<name>, unique across the repo
enabled: true                            # optional, default true
module: aws_cloudtrail
title: "Root account console login"
severity: high                           # info|low|medium|high|critical
interval: 60                             # seconds between evaluations
lookback: 15m                            # optional; exposed as %(lookback)s (Ns/Nm/Nh/Nd)
notify_recurrence: false                 # optional; folds notify too when true
sql: |
  ...
```

## SQL contract

The SQL must return exactly these columns:

| Column | Meaning |
|---|---|
| `dedup_key` | text; one alert per (rule, dedup_key) — recurrences fold |
| `entity` | jsonb; who/what this is about |
| `context` | jsonb; evidence. If it has an integer `events` key, that increments the alert's `event_count`; otherwise each firing counts 1 |
| `last_event_time` | timestamptz; advances the alert's `last_seen` |

Params available: `%(wm_from)s`, `%(wm_to)s` (the ingested_at watermark slice),
`%(lookback)s` (string, cast with `::interval`).

## The three sanctioned shapes

**Existence** — filter the watermark slice:

```sql
... WHERE ingested_at > %(wm_from)s AND ingested_at <= %(wm_to)s AND <condition>
```

**Threshold/correlation** — watermark slice as trigger, aggregate over event
time: trigger CTE on the slice, aggregate joined events over
`event_time > %(wm_to)s::timestamptz - %(lookback)s::interval`, with a
`HAVING count(*) >= N`. Recurrence folds via the upsert; put a `date_trunc`
bucket in `dedup_key` only when a fresh alert per window is genuinely wanted.

**Absence (dead-man)** — ignores the watermark params; fires when a module has
zero events in a trailing window:

```sql
SELECT '<module>' AS dedup_key, ...
WHERE NOT EXISTS (SELECT 1 FROM caib.events
                  WHERE module = '<module>'
                    AND ingested_at > now() - interval '2 hours')
```

**Every module ships one dead-man rule.** It is the alarm for expired tokens
and silently crash-looping pollers — the number-one SaaS-poller failure mode.
It stays open until a human closes it.

## Notes

- Rule SQL goes through psycopg named parameters: escape a literal `%` as `%%`
  (e.g. `LIKE '%%AdministratorAccess%%'`).
- Rules read only documented `attrs` keys (see the module's MAPPING.md), never
  ad-hoc paths into `raw`.
- Where a rule maps to a public Sigma cloud rule, note the Sigma ID in a
  comment.
- A rule over the per-tick cap (`CAIB_DETECT_MAX_ALERTS_PER_TICK`) produces one
  meta-alert instead of a flood, and the watermark still advances.
