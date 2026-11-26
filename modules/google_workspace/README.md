# google_workspace

Google Workspace audit logs (Admin SDK Reports API) via the CAIB poller.
Streams: `login`, `admin`, `drive`, `token` (OAuth grants), `saml`.

**Status: SYNTHETIC.** Built from Google's docs; fixtures are hand-built and
live verification is pending. Read [API_NOTES.md](API_NOTES.md) (checklist at
the bottom) before trusting it, and [MAPPING.md](MAPPING.md) for the fields.

## Google-side setup

You need a Workspace super admin (or an admin with the **Reports** privilege)
and a Google Cloud project.

1. Service account and key (Cloud Shell or any `gcloud`):

   ```sh
   PROJECT=my-project
   gcloud services enable admin.googleapis.com --project "$PROJECT"
   gcloud iam service-accounts create caib-workspace-reader \
       --project "$PROJECT" --display-name "CAIB Workspace audit reader"
   gcloud iam service-accounts keys create caib-workspace-key.json \
       --iam-account "caib-workspace-reader@$PROJECT.iam.gserviceaccount.com"
   # The numeric client ID for step 2:
   gcloud iam service-accounts describe \
       "caib-workspace-reader@$PROJECT.iam.gserviceaccount.com" --format 'value(oauth2ClientId)'
   ```

   If the org policy `iam.disableServiceAccountKeyCreation` is enforced, key
   creation fails; an exception for this project is needed (keyless
   alternatives are not supported by this poller yet).

2. Domain-wide delegation (Admin console, no CLI): **Security → Access and
   data control → API controls → Manage domain-wide delegation → Add new**.
   Client ID: the number from step 1. OAuth scopes:

   ```
   https://www.googleapis.com/auth/admin.reports.audit.readonly
   ```

   (This grant is itself what the `domain_wide_delegation_granted` rule
   alerts on — expect one alert when you set this up.)

3. Pick the admin the service account impersonates: a super admin or a user
   with an admin role that includes **Reports**.

## CAIB settings (`.env`)

```sh
CAIB_GOOGLE_WORKSPACE_SA_KEY_B64=$(base64 -w0 caib-workspace-key.json)   # then delete the file
CAIB_GOOGLE_WORKSPACE_ADMIN_EMAIL=admin@example.com
#CAIB_GOOGLE_WORKSPACE_CUSTOMER_ID=          # optional
#CAIB_GOOGLE_WORKSPACE_STREAMS=login,admin,drive,token:authorize,saml
#CAIB_GOOGLE_WORKSPACE_BACKFILL=2026-11-01T00:00:00Z   # default now-24h, max ~180d
```

Drop `drive` from `CAIB_GOOGLE_WORKSPACE_STREAMS` on editions without the
Drive audit log (Business Starter and other non-Business/Enterprise plans,
per Google) or if its volume is too much. On such an edition the `drive`
request errors and the poller crash-loops until you do.

Run it:

```sh
docker compose --profile google_workspace up -d poller-google-workspace
# or once, from a checkout:
uv run caib-poller --module google_workspace --once
```

## Rules (`detect/rules/google_workspace/`)

| Rule | Severity | Fires on |
|---|---|---|
| `admin_privilege_granted` | high | `GRANT_ADMIN_PRIVILEGE`, `GRANT_DELEGATED_ADMIN_PRIVILEGES`, `ASSIGN_ROLE` |
| `domain_wide_delegation_granted` | high | `AUTHORIZE_API_CLIENT_ACCESS` |
| `two_step_verification_disabled` | high | 2SV enforce/allow set to `false` |
| `login_risk_signal` | high | `suspicious_login*`, `gov_attack_warning`, `account_disabled_hijacked` |
| `email_forwarding_out_of_domain` | high | `email_forwarding_out_of_domain` |
| `drive_file_made_public` | medium | link sharing → `people_with_link` / `public_on_the_web` |
| `dead_man` | high | no events in 24h |

Five derive from SigmaHQ rules (IDs and attribution in each file); the Drive
rule is CAIB's own. `token` and `saml` are ingested for investigation and the
dashboard; no rule reads them yet.

## Dashboard

`CAIB Module: google_workspace` — the standard module panels plus:

- **Events by application (24h)** — volume and failures per stream; a stream
  at zero is the first sign of a scope or edition problem.
- **Admin console changes (24h)** — every `admin` event with setting, old and
  new value.
