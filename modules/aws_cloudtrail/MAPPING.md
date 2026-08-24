# aws_cloudtrail — field mapping

Remap: `vector/modules/aws_cloudtrail.yaml` (`remap_aws_cloudtrail`).

| Envelope | Vendor source (CloudTrail record) | Notes |
|---|---|---|
| `event_time` | `eventTime` | already RFC3339 UTC |
| `event_id` | `eventID` | dedup basis (vendor-stable UUID) |
| `actor` | `userIdentity.arn` → `userIdentity.type` | root events may lack arn |
| `src_ip` | `sourceIPAddress` | may be a service domain (e.g. `cloudtrail.amazonaws.com`) → loader's try_inet nulls it |
| `action` | `eventName` | |
| `target` | — (null) | CloudTrail has no single target field; specifics stay in `raw.requestParameters` |
| `outcome` | `errorCode` present → `failure`, else `success` | |
| `severity` | — | always `info` |
| `raw` | whole record, verbatim | |

## attrs

| Key | Vendor source |
|---|---|
| `user_type` | `userIdentity.type` (Root / IAMUser / AssumedRole / ...) |
| `event_source` | `eventSource` |
| `aws_region` | `awsRegion` |
| `error_code` | `errorCode` (absent on success) |
| `mfa` | `additionalEventData.MFAUsed` (console sign-in events only) |
| `policy_arn` | `requestParameters.policyArn` (policy attach/detach events only) |
| `world_open` | `true` when `requestParameters` contains `0.0.0.0/0` (else absent) |

## Severity map

Everything `info`; rules decide what matters.
