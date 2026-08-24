# Build from the repo root: docker build -f pollers/Dockerfile .
FROM python:3.12-slim

WORKDIR /app
COPY pollers/pyproject.toml ./
COPY pollers/caib_pollers ./caib_pollers
RUN pip install --no-cache-dir .

ENTRYPOINT ["caib-poller"]
