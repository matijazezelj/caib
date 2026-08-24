# cloudflare_audit

Polls Cloudflare Audit Logs v2 for one account into CAIB.

## Vendor-side setup

1. Cloudflare dashboard → My Profile → API Tokens → Create Token.
2. Grant the account-level **Audit Logs: Read** permission, scoped to the
   account you want to ingest. Nothing else.
3. Note your **Account ID** (dashboard → account home, right sidebar).

## CAIB-side setup

In `.env`:

```
CAIB_CLOUDFLARE_API_TOKEN=<token>
CAIB_CLOUDFLARE_ACCOUNT_ID=<account id>
# optional; first-run backfill start, default now()-24h:
CAIB_CLOUDFLARE_AUDIT_BACKFILL=2026-08-01T00:00:00Z
```

Then:

```sh
docker compose --profile cloudflare_audit up -d
```

One-shot drain (tests/demo): `caib-poller --module cloudflare_audit --once`.

## Module-specific dashboard panels (Phase 5)

- Actions by `resource_product` (where in Cloudflare things are changing)
- `actor_context` split (dash vs API) — token-driven changes stand out
