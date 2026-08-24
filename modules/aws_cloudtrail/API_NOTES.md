# aws_cloudtrail — API notes

Verified 2026-08-24, empirically (LocalStack S3+SQS replay + `vector test`), on
`timberio/vector:0.57.0-debian`.

## Delivery path

CloudTrail → S3 (`.json.gz`, one JSON object `{"Records":[...]}` per file) →
S3 event notification → SQS → Vector `aws_s3` source (SQS mode).

## Verified behaviors (spec asked; tests cover both)

- **Framing**: with `compression: auto`, one S3 object decompresses to one line
  → **one Vector event** whose `message` is the whole `{"Records":[...]}`
  document. Confirmed via LocalStack end-to-end (3 Records → exactly 3 landing
  rows; `tests/test_vector_cloudtrail.py::test_s3_sqs_localstack_end_to_end`).
- **Array fan-out**: a remap whose root value is an array emits one event per
  element on Vector 0.57.0. Confirmed by `vector test` and the sink row counts.
- **SQS message parsing**: Vector requires the full canonical S3 notification
  shape (`s3SchemaVersion`, `bucket.arn`, `object.eTag`, `object.sequencer`,
  ...). Minimal hand-rolled notifications are rejected with
  `data did not match any variant of untagged enum SqsEvent`.
- **VRL gotcha**: an `if` expression is not valid directly as an object-literal
  value; compute it into a variable first (see the remap).

## Vendor delivery lag

CloudTrail delivers to S3 in ~5–15 min batches. This dominates end-to-end
latency; nothing in CAIB should try to compensate.

## Fixtures

`fixtures/cloudtrail_records.json` — SYNTHETIC, built to the CloudTrail v1.10
record schema (console login without MFA by root, failed AssumeRole with
errorCode, StopLogging). Replace with sanitized real records when available.
