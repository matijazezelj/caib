# Build from the repo root: docker build -f core/Dockerfile .
FROM python:3.12-slim

WORKDIR /app
COPY core/pyproject.toml ./
COPY core/caib_core ./caib_core
RUN pip install --no-cache-dir .

# Baked-in migrations; the dev compose bind-mounts the repo copy over this.
COPY db/migrations /app/db/migrations
ENV CAIB_MIGRATIONS_DIR=/app/db/migrations

ENTRYPOINT ["caib-core"]
CMD ["migrate"]
