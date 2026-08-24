# gcp_audit — field mapping

Remap: `vector/modules/gcp_audit.yaml` (`remap_gcp_audit`). Input is one Cloud
Audit `LogEntry` per Pub/Sub message (Log Router sink), parsed at the event
root by `decoding.codec: json`.

| Envelope | Vendor source (LogEntry) | Notes |
|---|---|---|
| `event_time` | `timestamp` | RFC3339 |
| `event_id` | `insertId` | dedup basis |
| `actor` | `protoPayload.authenticationInfo.principalEmail` | |
| `src_ip` | `protoPayload.requestMetadata.callerIp` | may be `private`/`gce-internal-ip` → loader's try_inet nulls it |
| `action` | `protoPayload.methodName` | |
| `target` | `protoPayload.resourceName` | |
| `outcome` | `protoPayload.status.code` — 0/absent → success, else failure | |
| `severity` | LogEntry `severity` — ERROR → `low`, else `info` | |
| `raw` | whole LogEntry, verbatim | |

## attrs

| Key | Vendor source |
|---|---|
| `service_name` | `protoPayload.serviceName` |
| `log_id` | `logName` (activity / data_access / system_event / policy) |
| `permission_denied` | `protoPayload.status.code == 7` (always present, bool) |
| `roles_added` | comma-joined `role` of `serviceData.policyDelta.bindingDeltas[]` with `action == "ADD"` (absent when none) |

## Severity map

`ERROR` → `low`, everything else `info`. Rules decide what matters.
