# Changelog

Notable changes, newest first. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions will follow
[semver](https://semver.org/spec/v2.0.0.html) once there is a version.

The reasoning behind a change lives in [DECISIONS.md](DECISIONS.md) — this file
is the index, not the record.

## [Unreleased]

Everything so far. Phases 0–8 of `caib-build-spec.md` are the initial build and
are not itemised here; what follows is what changed after it, by pull request.

### Supply chain and CI

- Bumped `actions/checkout` to v7.0.1 and `astral-sh/setup-uv` to v10.0.1, off
  the deprecated Node 20 runtimes, and pinned both to full commit SHAs.
- Added `.github/dependabot.yml`: weekly `github-actions`, `docker` (the three
  Dockerfile directories), `docker-compose` (the six stack image pins) and `uv`
  (the workspace lock, minor and patch grouped).
- Added `scripts/check_image_pins.sh`, which inventories every image reference
  and fails on an untagged/`:latest` image or on the copies of the
  `timescale/timescaledb` pin disagreeing — neither is something dependabot
  catches.
- CI now has a `build` job: `docker build` for all three images (no push) and
  `docker compose config -q` for the base file and each overlay, with all
  profiles enabled. `test` now waits on `lint`.
- Both Python images build from `uv.lock` (`uv sync --frozen --no-dev
  --no-editable --package …`) instead of `pip install .`, so a container ships
  the dependencies the lockfile pins rather than whatever pip resolved that
  day. Both now run as `caib`, uid 10001.
- Added `SECURITY.md`.

### Reliability and ops (#4)

- `detect.outbox`, `detect.alerts` and `ingest.dead_letter` are swept on a
  retention schedule; nothing bounded them before.
- `CAIB_NOTIFY_FORMAT` is actually passed to the detect service in compose. It
  was documented but never wired.
- A bad `CAIB_NOTIFY_MIN_SEVERITY` is rejected at startup rather than at the
  first alert.
- A YAML error in a rule file no longer kills the detect process on SIGHUP.
- A permanently-flooding rule re-notifies the overflow meta-alert on an
  interval instead of once and never again.
- `notify.drain` POSTs the webhook outside the outbox transaction.

### Tests (#3)

- Coverage for the rule and envelope guardrails and for transport retry.

### Docs (#2, #1)

- README says how CAIB compares to the alternatives, and that HA is optional.
- `scripts/preflight.sh` says so when it cannot check for shipped passwords
  instead of passing silently.

[Unreleased]: https://github.com/matijazezelj/caib/commits/main
