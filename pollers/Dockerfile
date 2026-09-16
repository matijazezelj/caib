# Build from the repo root: docker build -f pollers/Dockerfile .
#
# Same two-stage shape as core/Dockerfile — see the comments there.
FROM ghcr.io/astral-sh/uv:0.12.15 AS uv

FROM python:3.12-slim AS builder
COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY pyproject.toml uv.lock ./
COPY core/pyproject.toml core/pyproject.toml
COPY pollers/pyproject.toml pollers/pyproject.toml
COPY core/caib_core core/caib_core
COPY pollers/caib_pollers pollers/caib_pollers

RUN uv sync --frozen --no-dev --no-editable --package caib-pollers

FROM python:3.12-slim
WORKDIR /app

RUN groupadd --system --gid 10001 caib \
    && useradd --system --uid 10001 --gid 10001 --no-create-home --home-dir /app caib

COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

USER caib

ENTRYPOINT ["caib-poller"]
