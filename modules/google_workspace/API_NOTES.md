# google_workspace — API notes

Built 2026-11-26 from Google's published documentation. **Live verification
pending** — no Workspace tenant or service account was available at build
time. **All fixtures in `fixtures/` are SYNTHETIC**: hand-built to the shapes
in the discovery document and the per-application event appendix pages, not
captured. Replace them with sanitized real responses at the first live run.

Sources read on 2026-11-26 (what "documented" means below):

- Reports API discovery document,
  `https://admin.googleapis.com/$discovery/rest?version=reports_v1`
  (revision `20261117`): method path, parameters, scopes, `Activity` schema.
- Event appendix pages under
  `https://developers.google.com/workspace/admin/reports/v1/appendix/activity/`:
  `login`, `admin-user-settings`, `admin-delegated-admin-settings`,
  `admin-security-settings`, `admin-domain-settings`, `drive`, `token`, `saml`.
- Limits and quotas: `.../workspace/admin/reports/v1/limits`.
- Service-account OAuth: `https://developers.google.com/identity/protocols/oauth2/service-account`.
- Lag and retention: Google Workspace Admin Help "Data retention and lag
  times", `https://support.google.com/a/answer/7061566`.

## Endpoint

`GET https://admin.googleapis.com/admin/reports/v1/activity/users/all/applications/{applicationName}`

- Params used: `startTime`, `endTime` (RFC3339; we send `YYYY-MM-DDTHH:MM:SSZ`),
  `maxResults=1000` (documented max), `pageToken`, optional `eventName`,
  optional `customerId`.
- Response: `{kind, etag, items: [Activity], nextPageToken?}`. `items` is
  absent on an empty page (handled as `[]`).
- Activity: `{kind, etag, id{time, uniqueQualifier, applicationName,
  customerId}, actor{email, profileId, callerType, key, applicationInfo},
  ipAddress, ownerDomain, networkInfo{regionCode, subdivisionCode, ipAsn},
  events: [{type, name, parameters[{name, value | intValue | boolValue |
  multiValue | multiIntValue | messageValue}], status{eventStatus, ...}}]}`.

## Streams (the application set, and why)

Default `CAIB_GOOGLE_WORKSPACE_STREAMS=login,admin,drive,token:authorize,saml`
(`app` or `app:eventName`).

| Stream | Why it is in | Rules |
|---|---|---|
| `login` | Sign-ins, failures, and Google's own risk verdicts (`suspicious_login*`, `gov_attack_warning`, `account_disabled_hijacked`), plus mailbox forwarding out of the domain. Near-real-time. | login_risk_signal, email_forwarding_out_of_domain |
| `admin` | Every Admin console change: admin/role grants, domain-wide delegation, 2SV enforcement. Low volume and the highest-value stream. | admin_privilege_granted, domain_wide_delegation_granted, two_step_verification_disabled |
| `drive` | Sharing and download trail: exfiltration triage. **The high-volume stream** (every view/edit). Drop it from the list if volume or edition is a problem. | drive_file_made_public |
| `token:authorize` | OAuth grants to third-party apps (consent phishing). Narrowed to `authorize` because the `token` app also emits `activity` — one event per API call an app makes — which would dwarf everything else. | none yet (dashboard/search context; a sensitive-scope rule is a candidate once real grants show the baseline) |
| `saml` | Sign-ins to third-party apps with Google as the IdP. Cheap: empty when SSO is not used. | none (dashboard: failures) |

Left out on purpose: `user_accounts` (2SV/password changes also appear in
`login`), `groups`, `calendar`, `meet`, `chat`, `gmail` (Gmail needs
`startTime`+`endTime` ≤ 30 days apart and carries message metadata; a module
of its own if wanted), `chrome`, `mobile`, `rules`.

## Auth

Service account + **domain-wide delegation**, JWT bearer grant:

- `POST https://oauth2.googleapis.com/token`,
  `grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer`, `assertion=<JWT>`.
- JWT header `{"alg":"RS256","typ":"JWT","kid":<private_key_id>}`; claims
  `iss`=SA `client_email`, `sub`=impersonated admin
  (`CAIB_GOOGLE_WORKSPACE_ADMIN_EMAIL`), `scope`=
  `https://www.googleapis.com/auth/admin.reports.audit.readonly`,
  `aud`=`https://oauth2.googleapis.com/token`, `iat`, `exp`=`iat+3600`
  (documented max 1h).
- A fresh assertion is signed for every token request (a reused one expires
  after 1h). Token cached until 5 min before `expires_in`.
- RS256 (`_rs256_sign`, RSASSA-PKCS1-v1_5 + SHA-256) uses `cryptography`,
  a direct dependency of the pollers package.
- Key delivery: `CAIB_GOOGLE_WORKSPACE_SA_KEY_B64` (base64 of the JSON key).
  An env var rather than a bind-mounted file because the poller runs as uid
  10001 and a 0600 host-owned key file would be unreadable.
- Token endpoint errors (bad key, delegation not granted, wrong scope,
  impersonated user not an admin) raise and crash the poller — compose
  restarts it, the cursor is untouched, the dead-man rule pages.

## Cursoring, lag and dedup

Cursor: `{"streams": {"<stream>": {"since": iso}}}` between windows;
`{"since", "start", "end", "page_token"}` while paging a window.

- Window = `[since − overlap, min(since + 24h, now − 120s)]`; the next window's
  `since` is this window's `end`.
- **Overlap** absorbs late-arriving events. Documented lag: login, admin,
  drive, SAML "near real time (couple of minutes)"; token "a couple of hours";
  "a small chance" of more, "in rare cases events might not be reported".
  Overlap is 30 min by default and 6 h for `token`.
- **Dedup**: re-read events have the same `event_id`; the loader drops them
  (`sha256(module:event_id)`). The poller also keeps an in-process seen-set
  for the overlap span, so the steady state does not re-land the overlap at
  all. After a restart the first overlap re-lands and the loader dedups it.
- Backfill: first run starts at `CAIB_GOOGLE_WORKSPACE_BACKFILL` (default
  now − 24h) and drains in ≤24h windows (`has_more` loops immediately).
  `since` is clamped to now − 179 days (data is kept ~6 months; queries reach
  back 180 days).
- A sealed stream opens its next window only after `interval_s` (300s) has
  accumulated, so while one stream drains pages the others send nothing.
- A `400` on a request that carries a `pageToken` restarts that stream's
  window instead of crash-looping on a dead token. 401/403 still crash.

## Rate limits

- 2,400 queries/min per user per project (default, raisable); quota
  exhaustion is documented as HTTP **503** → the framework retries with
  backoff.
- `activities.list` filter queries (any request with `eventName`, `filters`,
  `userKey` other than all, `actorIpAddress`, `orgUnitID`, `groupIdFilter`):
  250/min. Only the `token:authorize` stream is one; it runs once per cycle.
- Steady state: 5 requests per 300s cycle plus extra pages.

## Edition / plan

- Reports API needs a Workspace edition with audit logs (any paid edition;
  not consumer Gmail). The discovery doc says the **Drive activity report is
  only available for Business and Enterprise customers** — on another edition
  the `drive` request will error and, since 4xx crashes the poller, take the
  whole module down. Remedy: drop `drive` from `CAIB_GOOGLE_WORKSPACE_STREAMS`.
- The impersonated admin needs the **Reports** admin privilege (super admin
  has it).

## To verify live

Auth
- [ ] JWT bearer grant accepted with our RS256 signature (first
      `--once` run proves it).
- [ ] Error shape when delegation is missing (`unauthorized_client` 401?) and
      when the impersonated user lacks the Reports privilege (403 at the API?).
- [ ] `expires_in` is 3600.

Endpoint / pagination
- [ ] `users/all` + `customerId` omitted works for a single-customer tenant.
- [ ] `nextPageToken` is absent (not empty) on the last page.
- [ ] `pageToken` must be sent with the same `startTime`/`endTime` (we do);
      how long a token stays valid — does the 400-restart path ever fire?
- [ ] Order of items (assumed newest-first; we don't depend on it).

Time fields / ids
- [ ] `id.time` format: RFC3339 with milliseconds (samples) vs epoch seconds
      (discovery prose). Code accepts both.
- [ ] `id.uniqueQualifier` is stable across re-fetches of the same activity
      and unique per (application, time).
- [ ] `events[]` order and count are stable across re-fetches (the event
      index is part of `event_id`).
- [ ] `startTime`/`endTime` inclusive vs exclusive (overlap makes it moot,
      but record it).

Lag / retention
- [ ] Measured lag per stream vs the documented one, especially `token`
      ("a couple of hours") against the 6 h overlap and `drive`/`login`
      against 30 min. Anything later than the overlap is lost.
- [ ] Backfill reaches back ~180 days as documented.

Rate limits
- [ ] Quota errors really come back as 503 (retried) and not 403
      `rateLimitExceeded` (which would crash instead of retrying).

Payload shapes (each affects a rule)
- [ ] `login`: `is_suspicious` arrives as `boolValue`;
      `login_challenge_method` as `multiValue`.
- [ ] `login`: `email_forwarding_out_of_domain` carries
      `email_forwarding_destination_address` (the appendix shows it only in
      the message format, not the parameter table).
- [ ] `login`: account-warning events carry `affected_email_address`, and the
      actor is the affected user.
- [ ] `admin`: `ENFORCE_STRONG_AUTHENTICATION` / `ALLOW_STRONG_AUTHENTICATION`
      `NEW_VALUE` is literally `false`/`true` (rule compares lower-cased).
- [ ] `admin`: `ASSIGN_ROLE` `ROLE_NAME` for custom roles (display name vs id).
- [ ] `drive`: `change_document_visibility` `new_value` is a `multiValue`
      list and uses `people_with_link` / `public_on_the_web`.
- [ ] `token`: `scope` is a `multiValue` list of scope URLs.
- [ ] `saml`: parameter names (`application_name`, `failure_type`,
      `saml_status_code`, `orgunit_path`).
- [ ] Whether `events[].status` is populated for any of the five apps.

Plan / edition
- [ ] Which edition the test tenant is, and whether `drive` works on it.
