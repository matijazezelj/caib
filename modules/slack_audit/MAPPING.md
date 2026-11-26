# slack_audit — field mapping

Envelope fields (see spec §The envelope contract). Source paths refer to one
entry of `GET /audit/v1/logs` → `entries[]`. Shapes are from the docs and the
fixtures are SYNTHETIC; see API_NOTES.md.

| Envelope | Vendor source | Notes |
|---|---|---|
| `event_time` | `date_create` | unix seconds → RFC3339 UTC; unparseable → null → dead-letter |
| `event_id` | `id` | UUID, used for dedup |
| `actor` | `actor.user.email` → `actor.user.id` → `actor.type` | first non-empty; `USLACKUSER` / `USLACKSECURITY` appear as ids |
| `src_ip` | `context.ip_address` | |
| `action` | `action` | vendor verb, unmodified |
| `target` | `entity.<entity.type>.id` | `message` entity (no id) → `channel/timestamp`; else `.name` |
| `outcome` | derived from `action` | `user_login_failed`, `file_download_blocked` → failure; `anomaly` → unknown; everything else → success (entries record completed actions) |
| `severity` | — | always `info`; rules decide what matters |
| `raw` | whole entry, verbatim | |

## attrs (all optional; omitted when null/empty)

| Key | Vendor source |
|---|---|
| `actor_type` | `actor.type` |
| `actor_id` | `actor.<actor.type>.id` (Slack user id, e.g. `W…`/`U…`) |
| `actor_team` | `actor.<actor.type>.team` |
| `entity_type` | `entity.type` (user, channel, file, app, workspace, enterprise, …) |
| `entity_name` | `entity.<type>.name` → `.title` → `.email` |
| `channel_privacy` | `entity.channel.privacy` (public / private / …) |
| `location_type` | `context.location.type` (workspace / enterprise) |
| `location_id` | `context.location.id` (`T…` / `E…`) |
| `location_name` | `context.location.name` |
| `location_domain` | `context.location.domain` |
| `user_agent` | `context.ua` |
| `session_id` | `context.session_id` |
| `app_id` | `context.app.id` (only on app-triggered events) |
| `app_name` | `context.app.name` |
| `anomaly_reasons` | `details.reason[]` joined with `,` (only on `action = 'anomaly'`) |

`details` is not otherwise mapped. Its keys vary by action and are mostly
undocumented, so it stays in `raw` until a live capture pins them down.

## Severity map

Everything `info`.
