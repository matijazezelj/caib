# azure_activity — field mapping

Poller: `pollers/caib_pollers/modules/azure_activity.py`. Source is the ARM
Activity Log REST API (subscription management plane).

| Envelope | Vendor source (Activity Log event) | Notes |
|---|---|---|
| `event_time` | `eventTimestamp` | RFC3339 |
| `event_id` | `eventDataId` | dedup basis (GUID) |
| `actor` | `caller` | email or service principal id |
| `src_ip` | `httpRequest.clientIpAddress` | |
| `action` | `operationName.value` | e.g. `Microsoft.Authorization/roleAssignments/write` |
| `target` | `resourceId` | full ARM resource id |
| `outcome` | `status.value` — Succeeded → success, Failed → failure, else unknown | Started/Accepted phases are `unknown`; rules filter on `attrs.status = 'Succeeded'` |
| `severity` | — | always `info` |
| `raw` | whole event, verbatim | |

## attrs

| Key | Vendor source |
|---|---|
| `category` | `category.value` (Administrative / Security / Policy / ...) |
| `resource_group` | `resourceGroupName` |
| `resource_provider` | `resourceProviderName.value` |
| `level` | `level` (Informational / Warning / Error / Critical) |
| `status` | `status.value` raw (incl. Started/Accepted) |
| `sub_status` | `subStatus.value` (usually the HTTP status text) |

## Severity map

Everything `info`. Rules decide what matters.
