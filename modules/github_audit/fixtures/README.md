# github_audit fixtures — SYNTHETIC

Both pages were written by hand from GitHub's documented response schema and
event reference (`GET /orgs/{org}/audit-log`, "Audit log events for your
organization", 2026-11-26). No live organization was available at build time.

The API returns a bare JSON array, so the marker lives here and in
`../API_NOTES.md`, not inside the files. Field *names* follow the docs; field
*values* (`permission: "admin"`, `repository_selection: "all"`,
`actor_location.country_code`, the shape of `managed_token_scopes`) are
guesses that the verify-live checklist in API_NOTES.md lists one by one.

Replace both files with sanitized real responses at first live run.

- `audit_log_page1.json`: repo made public (fires), made private (benign),
  member promoted to owner (fires), owner demoted (benign), branch protection
  removed (fires), private repo created (benign).
- `audit_log_page2.json`: 2FA requirement disabled (fires), secret scanning
  disabled (fires), fine-grained PAT granted to all repos (fires) and to
  selected repos (benign), app installed on all repos (fires), classic PAT
  with `admin:org` SSO-authorized (fires), a `git.push` (benign, only with
  `include=git|all`).
