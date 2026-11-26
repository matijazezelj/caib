# github_audit — API notes

Built 2026-11-26 from GitHub's documentation (Enterprise Cloud docs, REST API
version `2022-11-28`). **No live organization was available**: nothing here
has been run against GitHub, and the fixtures are **SYNTHETIC**. "Documented"
below means read in the docs, not observed. Every guess is in the checklist
at the bottom.

Pages read (fetched as markdown via `docs.github.com/api/article/body`):

- REST → Organizations → "Get the audit log for an organization"
  (`/en/enterprise-cloud@latest/rest/orgs/orgs`)
- "Reviewing the audit log for your organization" (search syntax, retention,
  API section)
- "Audit log events for your organization" (action names, per-action fields)
- "Permissions required for fine-grained personal access tokens" and
  "... for GitHub Apps"
- "Audit log events for your enterprise" (only to confirm where
  `audit_log_streaming.*` lives)

## Required plan

**GitHub Enterprise Cloud.** Documented: "To use the audit log API, your
organization must use GitHub Enterprise Cloud." Free/Team orgs can view the
log in the web UI but cannot poll it. The authenticated user must be an
**organization owner**.

## Endpoint

`GET https://api.github.com/orgs/{org}/audit-log`

Headers sent: `Authorization: Bearer <token>`,
`Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28`.
`CAIB_GITHUB_API_URL` overrides the base for GHE.com data residency
(`https://api.<subdomain>.ghe.com` — from memory of the GHE.com docs, not
re-read for this build).

Documented query parameters: `phrase` (search phrase), `include`
(`web` | `git` | `all`, default `web`), `after` / `before` (cursors "as given
in the Link header"), `order` (`desc` | `asc`, default `desc`), `per_page`
(max 100, default 30).

## Auth

Documented:

- Classic PAT or OAuth app token: scope `read:audit_log`.
- Fine-grained PAT: organization permission **Administration: read**
  ("Permissions required for fine-grained PATs", row
  `GET /orgs/{org}/audit-log | read | PAT`).
- GitHub App: user access tokens and installation access tokens, organization
  permission **Administration: read** (row `... | read | UAT, IAT`).

Implemented: a static bearer token only (`CAIB_GITHUB_TOKEN`, either PAT
kind). **GitHub App auth is deferred**: an installation token expires after an
hour and is minted with an RS256-signed JWT, which needs `pyjwt` +
`cryptography` in the poller image. The pollers package depends only on
`httpx` and `psycopg`, and adding a dependency means touching `uv.lock`.
Worth doing once someone wants it: it removes the person-bound owner token.

## Cursoring

The documented time filter is the `created` search qualifier, ISO 8601 with
optional `THH:MM:SS+00:00`, and `A..B` ranges. The poller sends:

```
phrase=created:<since>..<before>   (both as YYYY-MM-DDTHH:MM:SS+00:00)
include=<web|git|all>  order=asc  per_page=100  [after=<cursor>]
```

- Windows are sealed at `now() - 300s`; the next window starts exactly at the
  previous `before`. A `..` range is (per the general search syntax)
  inclusive at both ends, so the boundary second may be fetched twice;
  dedup on `_document_id` absorbs that. Gaps are what matter, and there are
  none by construction.
- Inside a window: take `after` from the Link header's `rel="next"` URL
  (httpx `resp.links`) and pass it back with the same phrase. No next link
  (or an empty page) seals the window.
- Poller cursor dict: `{"since": iso}` between windows;
  `{"since", "before", "page_cursor"}` while paging. Same model as
  cloudflare_audit.
- `CAIB_GITHUB_AUDIT_BACKFILL` sets the first window's start (default
  now()-24h).

## Time fields

`@timestamp` and `created_at` are documented as integers. Every GitHub audit
log example and export the author has seen uses **epoch milliseconds**; the
envelope assumes that and writes RFC3339 with milliseconds.

## Rate limits

Documented: **1,750 queries per hour per user and IP address** for this
endpoint, signalled "typically [by] a 403 or 429". Steady state the poller
makes ~12 requests/hour (one per 5-minute window when a window fits in one
page), so only a large backfill can get near the limit: 1,750 pages × 100 =
175k events/hour.

Behaviour on a limit: `Ctx.get` honours `Retry-After` on **429**, but a
**403** is a non-retryable 4xx in the framework, so the process crashes,
compose restarts it, and the cursor is untouched (at-least-once, no gap).
That is the framework's crash-loud contract working as designed, but it
means a 403 rate limit and a revoked token look the same in the logs. Not
changed here because the framework is shared by every poller; a 403-with-
`x-ratelimit-remaining: 0` retry would be a framework change of its own.

## Retention and backfill window

- Web events: **180 days** (table "Audit log data available by access
  method"). The same page's API section also says other events "can be
  retained for up to seven months" — the two statements disagree; 180 days is
  the conservative one.
- Git events: **7 days**, REST API only (not GraphQL, not the web UI), and only
  pushes/clones/fetches made over Git itself (a web merge's push is not
  included).
- Without a `created` qualifier the API returns only the last three months;
  the poller always sends one.

## Git events

The task asked to include git events only if the API returns them for orgs.
It does (documented: `include=git|all` on the org endpoint, 7-day retention),
so `CAIB_GITHUB_AUDIT_INCLUDE` exposes the choice. The default is **`web`**:
git events are high-volume, short-lived and none of the shipped rules use
them.

## Payload shape

A bare JSON array of event objects. Documented top-level fields include
`@timestamp`, `_document_id`, `action`, `actor`, `actor_id`,
`actor_location{country_name}`, `created_at`, `operation_type`, `org`,
`org_id`, `business`, `repo`, `user`, `team`, `visibility`,
`previous_visibility`, `public_repo`, `transport_protocol_name`, plus per-action
"Additional fields" (e.g. `permission`/`old_permission` on
`org.update_member`, `repository_selection` on
`personal_access_token.access_granted` and `integration_installation.create`,
`managed_token_scopes` on `org_credential_authorization.grant`). The full
envelope/attrs mapping is in MAPPING.md.

## Audit-log streaming changes

`audit_log_streaming.create|update|destroy|check` are documented only in the
**enterprise** audit log. This module reads the organization endpoint, so it
ships no rule for them. Covering them needs `GET /enterprises/{e}/audit-log`
(enterprise owner token) — a separate module or a second stream, not built.

## Fixtures

**SYNTHETIC** — `fixtures/audit_log_page1.json` and `audit_log_page2.json`
were written by hand from the documented fields (see `fixtures/README.md`
for what each record is for). Replace them with sanitized real responses at
the first live run, then re-run `tests/test_github_audit.py`: the rule tests
assert exact alerts, so a changed literal will show up there.

## To verify live (`caib-poller --module github_audit --once`)

Auth and plan

- [ ] Classic PAT with only `read:audit_log` returns 200 for an org owner.
- [ ] Fine-grained PAT with only org "Administration: read" returns 200
      (and whether the org's PAT-approval policy blocks it until approved).
- [ ] A non-GHEC org returns which status (so the error is recognisable in
      the poller log)?
- [ ] SAML SSO orgs: an un-authorized classic PAT returns 403 — confirm the
      message names SSO.
- [ ] `X-GitHub-Api-Version: 2022-11-28` is still accepted for this endpoint
      (value from recall of the REST docs, not re-read for this build).
- [ ] GHE.com data residency: base URL really is
      `https://api.<subdomain>.ghe.com` (from recall, not re-read).

Endpoint and cursoring

- [ ] `phrase=created:A..B` with `THH:MM:SS+00:00` filters at **second**
      precision (not just by date). Test: a 5-minute window returns only
      events inside it.
- [ ] The `..` range is inclusive at both ends (expect boundary
      duplicates, which dedup absorbs) — and in particular **not** exclusive
      at the upper end in a way that drops events.
- [ ] `order=asc` holds across pages, and the Link `rel="next"` URL carries
      `after=` (not only `page=`).
- [ ] The last page has no `rel="next"` (rather than an empty extra page —
      either is handled, but note which).
- [ ] `phrase` and `include` combine (git events filtered by `created`).
- [ ] `per_page=100` accepted.

Time fields

- [ ] `@timestamp` / `created_at` are epoch **milliseconds** (a seconds value
      would land events in January 1970 and the loader would accept it).
- [ ] Indexing delay: are events visible within 300s (`WINDOW_LAG`)? Compare
      `@timestamp` with the time the event first appears in the API.

Rate limits

- [ ] Rate-limit response: 403 or 429, and with `Retry-After` or only
      `x-ratelimit-reset`?
- [ ] Backfill of a busy org: pages/hour actually reached.

Payload shapes and rule literals (the rules depend on these)

- [ ] `_document_id` present and stable on every event, including git
      events (else dedup falls back to the canonical hash).
- [ ] `repo.access`: `visibility` value for public is the string `public`.
- [ ] `org.update_member` / `org.add_member`: owner role shows as
      `permission = "admin"` (member as `read`?).
- [ ] `repository_selection` vocabulary is `all` / `selected` on
      `personal_access_token.access_granted` and `integration_installation.*`.
- [ ] `org_credential_authorization.grant`: name and type of the scopes field
      (`managed_token_scopes` list vs comma string) for classic PATs and OAuth
      tokens; envelope joins lists with `,`.
- [ ] `protected_branch.destroy`: branch name is in `name`.
- [ ] `actor_location` carries `country_code` (export docs) or only
      `country_name` (REST schema) — attr handles both.
- [ ] `actor_ip` field name once the enterprise enables IP disclosure.
- [ ] `programmatic_access_type` values (dashboard groups on them).
- [ ] Git events: shape of `git.push`/`git.clone` (`actor`, `repo`,
      `transport_protocol_name`).

Retention

- [ ] Oldest reachable event: 180 days or "seven months"?
- [ ] Git events older than 7 days really are gone (affects backfill with
      `include=all`).
