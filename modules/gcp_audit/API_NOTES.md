# gcp_audit — API notes

Built 2026-08-24 against the documented Cloud Audit Logs `LogEntry`/`AuditLog`
schema. Remap verified empirically with `vector validate` and a stdin→postgres
end-to-end test on `timberio/vector:0.57.0-debian`
(`tests/test_vector_gcp.py`). **Live Pub/Sub delivery not yet verified** — no
GCP project was available at build time.

## Delivery path

Cloud Audit Logs → Log Router sink (filtered to `cloudaudit.googleapis.com`)
→ Pub/Sub topic → subscription → Vector `gcp_pubsub` source with
`decoding.codec: json` (each message body is one LogEntry, parsed into the
event root — the remap reads LogEntry fields directly, no `.message`).

## Fixtures

`fixtures/log_entries.json` — SYNTHETIC, built to the documented AuditLog
schema: SetIamPolicy with a `bindingDeltas ADD roles/owner`, service-account
key creation, and a `PERMISSION_DENIED` (status.code 7) storage read. Replace
with sanitized real entries at first live verification.

## To verify live

- [ ] `gcp_pubsub` source auth via `credentials_path` on the pinned Vector
  (a service account JSON mounted at `/gcp/credentials.json`; the SA needs
  `roles/pubsub.subscriber` on the subscription).
- [ ] `decoding.codec: json` puts the LogEntry at the event root (assumed from
  Vector docs; the stdin test emulates it).
- [ ] `serviceData.policyDelta` vs newer `metadata`-based IAM audit payloads —
  some services emit binding deltas under `protoPayload.metadata` instead;
  extend the remap if live events show that shape.
- [ ] Log Router: Data Access logs are off by default per service; decide
  which to enable (they can be voluminous and billable).
