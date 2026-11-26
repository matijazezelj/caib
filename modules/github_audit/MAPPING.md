# github_audit — field mapping

Source: one element of the JSON array returned by
`GET /orgs/{org}/audit-log`. Field names are from GitHub's documented
response schema and per-action "Additional fields" lists; values are
unverified until the first live run (see API_NOTES.md).

| Envelope | Vendor source | Notes |
|---|---|---|
| `event_time` | `@timestamp` (fallback `created_at`) | epoch **milliseconds** → RFC3339 with ms, UTC |
| `event_id` | `_document_id` | dedup basis |
| `actor` | `actor` | login of who did it; null for some system events |
| `src_ip` | `actor_ip` | only present when the enterprise enabled IP disclosure; else null |
| `action` | `action` | `<category>.<operation>`, unmodified |
| `target` | `user` → `repo` → `repository` → `team` → `name` → `org` | first non-empty: the affected user, else the repo, ... |
| `outcome` | — | always `success`: the audit log records actions that happened and has no result field |
| `severity` | — | always `info` (conservative default) |
| `raw` | whole record, verbatim | |

## attrs (all optional; omitted when null/empty or not a scalar)

| Key | Vendor source | Used by |
|---|---|---|
| `category` | text before the first `.` of `action` | dashboard |
| `org` | `org` | org_owner_added, security_control_disabled |
| `business` | `business` (enterprise slug) | |
| `repo` | `repo` (`owner/name`) | repo_made_public, branch_protection_removed, security_control_disabled |
| `user` | `user` (affected user) | |
| `team` | `team` | |
| `operation_type` | `operation_type` (create/access/modify/remove/...) | |
| `visibility` | `visibility` | repo_made_public |
| `previous_visibility` | `previous_visibility` | repo_made_public (context) |
| `public_repo` | `public_repo` (bool) | |
| `permission` | `permission` | org_owner_added |
| `old_permission` | `old_permission` | org_owner_added (context) |
| `repository_selection` | `repository_selection` | broad_access_granted |
| `programmatic_access_type` | `programmatic_access_type` (absent for browser sessions) | branch_protection_removed (context), dashboard |
| `user_programmatic_access_name` | `user_programmatic_access_name` (token name) | broad_access_granted |
| `oauth_application_name` | `oauth_application_name` | broad_access_granted |
| `integration` | `integration` (GitHub App slug) | broad_access_granted |
| `transport_protocol_name` | `transport_protocol_name` (git events: http/ssh) | |
| `user_agent` | `user_agent` | |
| `actor_is_agent` | `actor_is_agent` (bool) | |
| `actor_country` | `actor_location.country_code` → `actor_location.country_name` | |
| `branch` | `name`, **only** for `protected_branch.*` actions | branch_protection_removed (context) |
| `token_scopes` | `token_scopes` → `managed_token_scopes` → `managed_oauth_scopes`; lists joined with `,` | broad_access_granted |

Everything else (lists such as `repositories`, `config`, ruleset bodies) stays
in `raw` only. A rule that needs one of those must first surface it here.

## Severity map

Everything `info`. Detection rules, not ingest severity, decide what matters.
