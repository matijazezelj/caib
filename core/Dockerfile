# Build from the repo root: docker build -f core/Dockerfile .
#
# Two stages. The builder resolves the workspace from uv.lock and produces a
# self-contained virtualenv; the runtime image gets that venv and nothing
# else — no uv, no build backend, no lockfile, no source tree.

# `FROM ... AS uv` rather than `COPY --from=ghcr.io/astral-sh/uv:...` on
# purpose: dependabot's docker parser only reads FROM lines, so written this
# way the uv pin is watched like every other image.
FROM ghcr.io/astral-sh/uv:0.12.15 AS uv

FROM python:3.14-slim AS builder
COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# The whole workspace has to be on disk for uv to resolve uv.lock, even though
# --package narrows what actually gets installed.
COPY pyproject.toml uv.lock ./
COPY core/pyproject.toml core/pyproject.toml
COPY pollers/pyproject.toml pollers/pyproject.toml
COPY core/caib_core core/caib_core
COPY pollers/caib_pollers pollers/caib_pollers

# --frozen so a stale uv.lock fails the build instead of being re-resolved
# behind our backs; that is the whole point of building from the lock.
# --no-editable installs a built wheel, which is what makes /app/.venv
# relocatable to the runtime stage on its own.
RUN uv sync --frozen --no-dev --no-editable --package caib-core

FROM python:3.14-slim
WORKDIR /app

# uid/gid 10001: above the range distros hand out, so it cannot collide with a
# host account if a bind mount is ever written to.
RUN groupadd --system --gid 10001 caib \
    && useradd --system --uid 10001 --gid 10001 --no-create-home --home-dir /app caib

COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# Baked-in migrations and rules; the dev compose bind-mounts repo copies over these.
COPY db/migrations /app/db/migrations
COPY detect/rules /app/detect/rules
ENV CAIB_MIGRATIONS_DIR=/app/db/migrations \
    CAIB_RULES_DIR=/app/detect/rules

# Nothing in caib_core writes to disk, so nothing is chowned: /app stays
# root-owned and read-only to the runtime user.
USER caib

ENTRYPOINT ["caib-core"]
CMD ["migrate"]
