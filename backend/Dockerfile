FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl build-essential \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src ./src

RUN pip install --upgrade pip \
 && pip install . \
 && pip install .[dev]

ENV PYTHONPATH=/app/src

FROM base AS dev

# The container runs the extraction worker; the HTTP API is not part of
# this repository.
CMD ["python", "-m", "superconductor_db.ingest.worker"]