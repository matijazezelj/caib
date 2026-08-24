# azure_activity

Azure Activity Log (subscription management plane) → CAIB, via polling the ARM
REST API. The CloudTrail / GCP Audit analog for Azure.

## Cloud-side setup (copy-paste CLI)

```sh
SUBSCRIPTION=$(az account show --query id -o tsv)

# App registration + client secret
APP_ID=$(az ad app create --display-name caib-activity --query appId -o tsv)
az ad sp create --id $APP_ID
SECRET=$(az ad app credential reset --id $APP_ID --years 1 --query password -o tsv)
TENANT=$(az account show --query tenantId -o tsv)

# Reader on the subscription is sufficient to read the Activity Log
az role assignment create --assignee $APP_ID --role Reader \
  --scope /subscriptions/$SUBSCRIPTION

echo "tenant=$TENANT app=$APP_ID secret=$SECRET"
```

Note: client secrets expire (max 2 years). The module's dead-man rule is the
alarm for a quietly expired secret — put the renewal date in your calendar
anyway.

## CAIB-side setup

In `.env`:

```
CAIB_AZURE_TENANT_ID=<tenant guid>
CAIB_AZURE_CLIENT_ID=<app id>
CAIB_AZURE_CLIENT_SECRET=<secret>
CAIB_AZURE_SUBSCRIPTION_ID=<subscription guid>
# optional; first-run backfill start, default now()-24h (API max: 90 days back)
#CAIB_AZURE_ACTIVITY_BACKFILL=2026-08-01T00:00:00Z
```

Then: `docker compose --profile azure_activity up -d`

## Module-specific dashboard panels

- Events by `category` (Administrative vs Security vs Policy)
- Failed operations by caller (broken automations, enumeration)
