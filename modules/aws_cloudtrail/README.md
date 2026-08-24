# aws_cloudtrail

CloudTrail → S3 → SQS → Vector → CAIB.

## Cloud-side setup (copy-paste CLI)

Assumes an org/account trail delivering to a dedicated bucket. Adjust names.

```sh
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
BUCKET=caib-cloudtrail-$ACCOUNT_ID
QUEUE=caib-cloudtrail-events
REGION=us-east-1

# 1. Trail -> S3 (skip if you already have a trail)
aws s3 mb s3://$BUCKET --region $REGION
aws cloudtrail create-trail --name caib-trail \
  --s3-bucket-name $BUCKET --is-multi-region-trail
aws cloudtrail start-logging --name caib-trail

# 2. SQS queue that S3 notifies
QUEUE_URL=$(aws sqs create-queue --queue-name $QUEUE --query QueueUrl --output text)
QUEUE_ARN=$(aws sqs get-queue-attributes --queue-url $QUEUE_URL \
  --attribute-names QueueArn --query Attributes.QueueArn --output text)

aws sqs set-queue-attributes --queue-url $QUEUE_URL --attributes '{
  "Policy": "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"s3.amazonaws.com\"},\"Action\":\"sqs:SendMessage\",\"Resource\":\"'"$QUEUE_ARN"'\",\"Condition\":{\"ArnEquals\":{\"aws:SourceArn\":\"arn:aws:s3:::'"$BUCKET"'\"}}}]}"
}'

# 3. S3 -> SQS notifications for new objects
aws s3api put-bucket-notification-configuration --bucket $BUCKET \
  --notification-configuration '{
    "QueueConfigurations": [
      {"QueueArn": "'"$QUEUE_ARN"'", "Events": ["s3:ObjectCreated:*"]}
    ]
  }'
```

IAM for the Vector consumer (least privilege):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow",
     "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"],
     "Resource": "<QUEUE_ARN>"},
    {"Effect": "Allow",
     "Action": ["s3:GetObject"],
     "Resource": "arn:aws:s3:::<BUCKET>/*"}
  ]
}
```

## CAIB-side setup

In `.env`:

```
CAIB_CLOUDTRAIL_SQS_URL=<QUEUE_URL>
CAIB_AWS_REGION=us-east-1
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
```

Then: `docker compose --profile aws_cloudtrail up -d`

## Module-specific dashboard panels (Phase 5)

- Events by `event_source` (which AWS APIs are active)
- Console sign-ins split by `mfa`
- `error_code` table (AccessDenied spikes)
