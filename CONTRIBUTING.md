# Contributing to CAIB

Thanks for looking. CAIB is a small spare-time project, so short, focused pull
requests are the easiest to review.

## Setup

You need Docker (with the Compose plugin) and [`uv`](https://docs.astral.sh/uv/).

```sh
uv sync              # dev environment for the whole workspace
make test            # ruff + pytest; pytest starts a throwaway TimescaleDB container
make up && make demo # the stack with synthetic data, if you want to look at it
```

## Before you open a PR

- `uv run ruff check .` passes. CI runs it first and fails the build on any
  finding.
- `uv run pytest -q` passes, and new behaviour comes with a test.
  The Vector end-to-end tests can time out on a local Linux Docker setup
  (loopback-bound ports); CI is the arbiter for those.
- Read `caib-build-spec.md` before changing anything architectural, and
  `docs/NEW_MODULE_HOWTO.md` before adding a source.

## Things that are append-only

- **`DECISIONS.md`**: every deviation from the spec or empirical finding gets a
  new entry. Never edit or delete an old one.
- **`db/migrations/`**: never edit a migration that has been merged. Add a new
  numbered file instead.

## Fixtures

Fixtures must never contain real data: no real account or project IDs,
emails, IPs, tokens or customer content. Use example values (`123456789012`,
`example-project`, `example.com`, RFC 5737 addresses). A fixture built from
vendor docs rather than captured from a live API is marked **SYNTHETIC** in
the module's `API_NOTES.md`, and is replaced with a sanitized real response
once someone verifies the module live.

## Detection rules

Rules live in `detect/rules/<module>/`; see `detect/rules/README.md`. If a
rule is based on a SigmaHQ rule, cite the real upstream ID, title, author and
link, and note the Detection Rule License 1.1.

## Security issues

Do not open a public issue for a vulnerability. See [SECURITY.md](SECURITY.md).

## Licence

CAIB is Apache-2.0. By contributing you agree that your contribution is
licensed under the same terms.
