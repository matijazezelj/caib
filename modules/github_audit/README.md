# github_audit

Polls a GitHub organization's audit log (`GET /orgs/{org}/audit-log`) into
CAIB. **Requires GitHub Enterprise Cloud**: the audit log API is not available
to organizations on the Free or Team plans.

Status: built from GitHub's documentation; fixtures are SYNTHETIC and nothing
has run against a live org yet. See `API_NOTES.md` for the verify-live list.

## Vendor-side setup

The token must belong to an **organization owner** (the endpoint is
owner-only). Either:

- **Classic PAT** — github.com → Settings → Developer settings → Personal
  access tokens (classic) → Generate. Scope: `read:audit_log` only. If the org
  uses SAML SSO, click **Configure SSO** next to the token and authorize it
  for the org.
- **Fine-grained PAT** (preferred: scoped to one org, mandatory expiry) —
  Settings → Developer settings → Fine-grained tokens → Generate. Resource
  owner: the organization. Organization permissions → **Administration:
  Read-only**. Nothing else. The org may require an owner to approve it.

Check it before wiring it in (expects HTTP 200 and a JSON array):

```sh
curl -sS -o /dev/null -w '%{http_code}\n' \
  -H "Authorization: Bearer $CAIB_GITHUB_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  "https://api.github.com/orgs/$CAIB_GITHUB_ORG/audit-log?per_page=1"
```

A GitHub App would avoid a person-bound token, and GitHub documents the
endpoint for App installation tokens (organization "Administration: read"),
but minting installation tokens needs RS256 JWT signing, which the poller
image does not ship yet. Deferred — see API_NOTES.md.

## CAIB-side setup

In `.env`:

```
CAIB_GITHUB_TOKEN=<token>
CAIB_GITHUB_ORG=<org login>
# optional:
CAIB_GITHUB_API_URL=https://api.github.com   # GHE.com: https://api.<subdomain>.ghe.com
CAIB_GITHUB_AUDIT_INCLUDE=web                # web | git | all
CAIB_GITHUB_AUDIT_BACKFILL=2026-11-01T00:00:00Z   # first run only; default now()-24h
```

`include=git|all` adds clone/fetch/push events. GitHub keeps those for 7 days
only and they can be very chatty; start with `web`.

Then:

```sh
docker compose --profile github_audit up -d
```

One-shot drain (tests/demo): `caib-poller --module github_audit --once`.

## Rules (`detect/rules/github_audit/`)

| Rule | Severity | Fires on |
|---|---|---|
| `repo_made_public` | high | `repo.access` with visibility `public` |
| `org_owner_added` | high | `org.update_member` / `org.add_member` with permission `admin` |
| `branch_protection_removed` | high | `protected_branch.destroy`, `repository_ruleset.destroy` |
| `security_control_disabled` | high | secret scanning / push protection / GHAS / 2FA requirement / OAuth app restrictions / SAML / IP allow list turned off (partly derived from SigmaHQ) |
| `broad_access_granted` | medium | PAT or app granted all repositories; classic token with `repo`/`workflow`/`delete_repo`/`admin:*` SSO-authorized; OAuth app approved |
| `dead_man` | high | no github_audit events for 24h |

Audit-log streaming changes (`audit_log_streaming.*`) are enterprise-level
events: GitHub documents them for the enterprise audit log, not the
organization one this module reads, so there is no rule for them here.

## Module-specific dashboard panels

- **Changes by audit category (24h)** — `attrs.category` (repo, org,
  protected_branch, ...).
- **Web vs token-driven changes (24h)** — `attrs.programmatic_access_type`;
  a sudden swing to token-driven changes is worth a look.
- **Recent access & protection changes** — replaces the usual "Recent
  failures" panel, which would always be empty: the audit log has no
  success/failure field.
