# azure_activity — API notes

Built 2026-08-24 from the documented ARM Activity Log REST API. **Not in the
original spec** — added to cover all three major clouds (see DECISIONS.md).
**Live verification pending** — no Azure tenant was available at build time;
fixtures are SYNTHETIC, built to the documented event schema.

## Endpoint

`GET https://management.azure.com/subscriptions/{subscriptionId}`
`/providers/Microsoft.Insights/eventtypes/management/values`
`?api-version=2015-04-01&$filter=eventTimestamp ge '<since>' and eventTimestamp le '<before>'`

- Pagination: response `nextLink` is a complete URL; follow it verbatim.
- Retention: the Activity Log keeps 90 days via this API.
- The filter supports at most a 90-day span; the poller's windows are minutes
  wide, far below that.

## Auth

OAuth2 client credentials against
`https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token` with
`scope=https://management.azure.com/.default`. App registration + the
**Reader** role on the subscription (or narrower: `Microsoft.Insights/eventtypes/values/read`).
Token cached by the framework's `OAuth2ClientCredentials` with early refresh.

## Cursoring

Same window model as cloudflare_audit: `{"since": iso}` between windows;
`{"since", "before", "next_link"}` while paging. Windows sealed at
`now() - 300s` (Activity Log ingestion lag is minutes). Next window starts at
the previous `before`.

## Semantics worth knowing

- Operations emit **multiple phases** (Started/Accepted/Succeeded/Failed) as
  separate events with distinct `eventDataId`s. All are ingested; envelope
  `outcome` is `unknown` for non-terminal phases and rules filter on
  `attrs.status = 'Succeeded'`.
- This is the management plane only. Entra ID (sign-ins, directory changes)
  is a separate API (Microsoft Graph) — a future `entra_audit` module; see
  docs/NEW_MODULE_HOWTO.md.

## To verify live

- [ ] `$filter` grammar accepted exactly as built (`ge`/`le` with quoted timestamps).
- [ ] `nextLink` host/format (assumed absolute URL).
- [ ] Actual ingestion lag vs the 300s window lag.
- [ ] Throttling behavior (ARM returns 429 + Retry-After; framework honors it).
