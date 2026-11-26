# gcp_audit

GCP Cloud Audit Logs → Log Router → Pub/Sub → Vector → CAIB.

## Cloud-side setup (copy-paste CLI)

```sh
PROJECT=example-project
TOPIC=caib-audit
SUB=caib-audit-sub
SA=caib-vector

gcloud config set project $PROJECT

# 1. Pub/Sub topic + subscription
gcloud pubsub topics create $TOPIC
gcloud pubsub subscriptions create $SUB --topic $TOPIC \
  --ack-deadline 60 --message-retention-duration 7d

# 2. Log Router sink: all Cloud Audit logs -> the topic
gcloud logging sinks create caib-audit-sink \
  pubsub.googleapis.com/projects/$PROJECT/topics/$TOPIC \
  --log-filter='logName:"cloudaudit.googleapis.com"'

# 3. Let the sink's writer identity publish
WRITER=$(gcloud logging sinks describe caib-audit-sink --format='value(writerIdentity)')
gcloud pubsub topics add-iam-policy-binding $TOPIC \
  --member="$WRITER" --role=roles/pubsub.publisher

# 4. Service account for Vector (subscriber only)
gcloud iam service-accounts create $SA
gcloud pubsub subscriptions add-iam-policy-binding $SUB \
  --member="serviceAccount:$SA@$PROJECT.iam.gserviceaccount.com" \
  --role=roles/pubsub.subscriber
gcloud iam service-accounts keys create caib-gcp-credentials.json \
  --iam-account=$SA@$PROJECT.iam.gserviceaccount.com
```

For an org-wide sink use `gcloud logging sinks create --organization=<org-id>`
with the same filter.

## CAIB-side setup

In `.env`:

```
CAIB_GCP_PROJECT=example-project
CAIB_GCP_SUBSCRIPTION=caib-audit-sub
CAIB_GCP_CREDENTIALS_FILE=./caib-gcp-credentials.json
```

Then: `docker compose --profile gcp_audit up -d`

## Module-specific dashboard panels

- Events by `service_name` (which GCP APIs are active)
- `permission_denied` trend (enumeration / broken credentials)
