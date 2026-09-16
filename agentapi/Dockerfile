# Build from the repo root: docker build -f agentapi/Dockerfile .
#
# Same two-stage shape as core/Dockerfile — see the comments there.
#
# A third image rather than adding caib-agentapi to caib-core:dev. That would
# have been the smaller pin surface (no new FROM lines, no new dependabot
# directory, no new CI build step), but it would put FastAPI, uvicorn, the MCP
# SDK and their transitive tree — cryptography, pyjwt, starlette, jsonschema —
# inside the image that runs migrate, the loader and the detection engine. Those
# three write to the audit database; the agent layer only reads from it, as a
# different role. Keeping the web stack out of the writers' image is worth one
# more copy of two pins that scripts/check_image_pins.sh already forces to agree
# with core's and pollers'.
FROM ghcr.io/astral-sh/uv:0.12.15 AS uv

FROM python:3.14-slim AS builder
COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY pyproject.toml uv.lock ./
COPY core/pyproject.toml core/pyproject.toml
COPY pollers/pyproject.toml pollers/pyproject.toml
COPY agentapi/pyproject.toml agentapi/pyproject.toml
COPY core/caib_core core/caib_core
COPY pollers/caib_pollers pollers/caib_pollers
COPY agentapi/src agentapi/src

RUN uv sync --frozen --no-dev --no-editable --package caib-agentapi

FROM python:3.14-slim
WORKDIR /app

RUN groupadd --system --gid 10001 caib \
    && useradd --system --uid 10001 --gid 10001 --no-create-home --home-dir /app caib

COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# list_rules reads the rule YAML off disk, the same way the detection engine
# does. Baked in like core's copy; the dev compose bind-mounts the repo's over it.
COPY detect/rules /app/detect/rules
ENV CAIB_RULES_DIR=/app/detect/rules

USER caib

ENTRYPOINT ["caib-agent-api"]
