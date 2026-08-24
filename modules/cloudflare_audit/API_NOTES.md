# cloudflare_audit — API notes

Verified 2026-08-24 against Cloudflare docs (fundamentals → Audit Logs v2, API
reference → Accounts › Logs › Audit).

## Endpoint

`GET https://api.cloudflare.com/client/v4/accounts/{account_id}/logs/audit`

This is **Audit Logs v2**. The spec's starting point (`GET
/client/v4/accounts/{id}/audit_logs`) is the legacy v1 API; v2 is the current
one (standardized format across ~95% of Cloudflare products, captures both
user- and system-initiated events) and is what this module uses.

## Auth

API token, `Authorization: Bearer <token>`, with the account-level Audit Logs
Read permission. Set `CAIB_CLOUDFLARE_API_TOKEN` and
`CAIB_CLOUDFLARE_ACCOUNT_ID`.

## Cursoring

- `since` / `before`: RFC3339 timestamps (required window bounds).
- Pagination inside a window via `result_info.cursor` — pass back as `cursor`;
  absent/empty cursor means the window is done.
- `limit`: default 100 (we request 1000 — see "to verify"), `direction=asc`.
- Retention: 18 months via API.

Poller cursor dict: `{"since": iso}` between windows;
`{"since": iso, "before": iso, "page_cursor": str}` while paging. Windows are
sealed at `now() - 300s` (Cloudflare indexes records asynchronously); the next
window starts exactly at the previous `before`.

## Record shape (v2)

Top-level `id` (dedup basis), plus objects:
`action{type, description, result, time}`,
`actor{id, email, type, context, ip_address, token_id, token_name}`,
`account{id, name}`, `resource{id, type, product, scope, request, response}`,
`zone{id, name}`, `raw{cf_ray_id, method, status_code, uri, user_agent}`.

## Fixtures

**SYNTHETIC** — constructed from the documented v2 schema, not captured from a
live account (none was available at build time). Replace with sanitized real
responses at first live verification.

## To verify live (`caib-poller --module cloudflare_audit --once`)

- [ ] Maximum accepted `limit` (module requests 1000; docs state default 100).
- [ ] Exact token permission name in the dashboard token builder.
- [ ] `action.result` runtime type (bool vs string) — envelope handles both.
- [ ] Rate-limit headers on 429 (framework honors `Retry-After`).
