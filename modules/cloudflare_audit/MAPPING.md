# cloudflare_audit — field mapping

Envelope fields (see spec §The envelope contract):

| Envelope | Vendor source (v2 record) | Notes |
|---|---|---|
| `event_time` | `action.time` (fallback `when`) | RFC3339 |
| `event_id` | `id` | dedup basis |
| `actor` | `actor.email` → `actor.token_name` → `actor.type` | first non-empty |
| `src_ip` | `actor.ip_address` | |
| `action` | `action.type` | vendor verb, unmodified |
| `target` | `resource.id` | |
| `outcome` | `action.result` | bool true/false → success/failure; strings pass through; else unknown |
| `severity` | — | always `info` (conservative default) |
| `raw` | whole record, verbatim | |

## attrs (all optional; omitted when null/empty)

| Key | Vendor source |
|---|---|
| `actor_type` | `actor.type` (user / account / Cloudflare_admin / system) |
| `actor_context` | `actor.context` (dash / API) |
| `resource_type` | `resource.type` |
| `resource_product` | `resource.product` |
| `resource_scope` | `resource.scope` (user / account / zone) |
| `zone_name` | `zone.name` |
| `http_method` | `raw.method` |
| `http_status` | `raw.status_code` |
| `http_uri` | `raw.uri` |

## Severity map

Everything `info`. Detection rules, not ingest severity, decide what matters.
