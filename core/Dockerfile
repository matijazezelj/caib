# Build from the repo root: docker build -f core/Dockerfile .
FROM python:3.12-slim

WORKDIR /app
COPY core/pyproject.toml ./
COPY core/caib_core ./caib_core
RUN pip install --no-cache-dir .

# Baked-in migrations and rules; the dev compose bind-mounts repo copies over these.
COPY db/migrations /app/db/migrations
COPY detect/rules /app/detect/rules
ENV CAIB_MIGRATIONS_DIR=/app/db/migrations \
    CAIB_RULES_DIR=/app/detect/rules

ENTRYPOINT ["caib-core"]
CMD ["migrate"]
