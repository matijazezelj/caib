## What and why

<!-- What this changes and why. Link the issue if there is one. -->

## How it was tested

<!-- Commands you ran and what they showed. -->

## Checklist

- [ ] `uv run ruff check .` and `uv run pytest -q` pass
- [ ] Deviations from the spec or empirical findings have a new `DECISIONS.md` entry (append-only)
- [ ] No merged migration was edited; schema changes are a new migration
- [ ] Fixtures hold example data only; docs-built ones are marked SYNTHETIC
- [ ] Docs (README, module README/MAPPING/API_NOTES) match the change
