# google_workspace — field mapping

Poller: `pollers/caib_pollers/modules/google_workspace.py`. Source is the Admin
SDK Reports API `activities.list` (one request per stream: `login`, `admin`,
`drive`, `token` narrowed to `eventName=authorize`, `saml`).

**One envelope per event.** A Reports API *activity* carries an `events[]`
array; each element becomes its own landing row (`raw` holds the whole
activity on every one of them). Fixtures are SYNTHETIC — see API_NOTES.md.

| Envelope | Vendor source (activity) | Notes |
|---|---|---|
| `event_time` | `id.time` | RFC3339 in Google's samples; an all-digit value is treated as epoch seconds (the discovery doc's prose says so) |
| `event_id` | `<id.applicationName>:<id.time>:<id.uniqueQualifier>:<index in events[]>` | dedup basis. Docs: uniqueQualifier is "unique qualifier if multiple events have the same time", so the time is part of the key. No uniqueQualifier → `null` → loader's canonical hash |
| `actor` | `actor.email`, else `actor.key` (callerType `KEY`, e.g. `SYSTEM`), else `actor.profileId` | |
| `src_ip` | `ipAddress` | absent for system actions |
| `action` | `events[i].name` | vendor's verb unmodified (`login_success`, `GRANT_ADMIN_PRIVILEGE`, `change_document_visibility`, ...) |
| `target` | first present parameter, per application (table below) | `null` when the event has none |
| `outcome` | `events[i].status.eventStatus` FAILED → failure, SUCCEEDED[_WITH_WARNINGS] → success; else name `login_failure` / `deny` / `risky_sensitive_action_blocked` → failure; `login_success` → success; other `login`/`saml` events → unknown; `admin`/`drive`/`token` → success (entries record completed actions) | |
| `severity` | — | always `info`; rules decide what matters |
| `raw` | whole activity, verbatim | |

### target by application

| Application | Parameter (first present wins) |
|---|---|
| `admin` | `USER_EMAIL`, `GROUP_EMAIL`, `API_CLIENT_NAME`, `DOMAIN_NAME` |
| `drive` | `doc_id` |
| `token` | `client_id`, `app_name` |
| `saml` | `application_name` |
| `login` | `email_forwarding_destination_address`, `affected_email_address` |

## attrs

Every key below, and nothing else. Event parameters are whitelisted, not
flattened generically. Multi-valued parameters (`multiValue`,
`multiIntValue`) are joined with `,`. `messageValue` parameters stay in `raw`.

| Key | Vendor source | Applications |
|---|---|---|
| `application` | `id.applicationName` — **rules filter on it**: `login` and `saml` share event names (`login_success`, `login_failure`) | all |
| `event_type` | `events[i].type` (`login`, `account_warning`, `USER_SETTINGS`, `acl_change`, `auth`, ...) | all |
| `caller_type` | `actor.callerType` (`USER`, `KEY`) | all |
| `actor_profile_id` | `actor.profileId` | all |
| `owner_domain` | `ownerDomain` | drive, admin |
| `region_code` | `networkInfo.regionCode` (ISO 3166-1 alpha-2) | where present |
| `login_type` | param `login_type` | login |
| `login_challenge_method` | param `login_challenge_method` (multi) | login |
| `login_failure_type` | param `login_failure_type` | login |
| `is_suspicious` | param `is_suspicious` (bool) | login |
| `is_second_factor` | param `is_second_factor` (bool) | login |
| `affected_email_address` | param `affected_email_address` | login (account warnings) |
| `forwarding_destination` | param `email_forwarding_destination_address` | login |
| `user_email` | param `USER_EMAIL` | admin |
| `group_email` | param `GROUP_EMAIL` | admin |
| `role_name` | param `ROLE_NAME` | admin |
| `setting_name` | param `SETTING_NAME` | admin |
| `new_value` | param `NEW_VALUE` (admin) / `new_value` (drive, multi) | admin, drive |
| `old_value` | param `OLD_VALUE` (admin) / `old_value` (drive, multi) | admin, drive |
| `org_unit` | param `ORG_UNIT_NAME` (admin) / `orgunit_path` (saml) | admin, saml |
| `domain_name` | param `DOMAIN_NAME` | admin |
| `api_client_name` | param `API_CLIENT_NAME` (the DWD client id) | admin |
| `api_scopes` | param `API_SCOPES` | admin |
| `doc_id`, `doc_title`, `doc_type`, `owner` | params of the same name | drive |
| `visibility`, `old_visibility`, `target_domain`, `target_user` | params of the same name | drive |
| `app_name`, `client_id`, `client_type` | params of the same name | token |
| `scope` | param `scope` (multi) | token |
| `saml_app` | param `application_name` | saml |
| `initiated_by`, `failure_type`, `saml_status_code` | params of the same name | saml |

## Rules → attrs used

| Rule | Reads |
|---|---|
| `admin_privilege_granted` | `application`, `role_name` |
| `domain_wide_delegation_granted` | `application`, `api_scopes` |
| `two_step_verification_disabled` | `application`, `new_value`, `org_unit` |
| `login_risk_signal` | `application` |
| `email_forwarding_out_of_domain` | `application` |
| `drive_file_made_public` | `application`, `new_value`, `doc_title` |
