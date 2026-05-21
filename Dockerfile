FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Upgrade build tooling first to pick up patched setuptools/wheel/pip — the
# base image ships setuptools 65.5.0 which has known CVEs (PYSEC-2022-43012,
# PYSEC-2025-49, CVE-2024-6345). Only affects build-time use of setuptools'
# package_index module, but we patch it anyway as a defence-in-depth measure.
RUN pip install --upgrade pip "setuptools>=78.1.1" wheel

# Install dependencies first (better layer caching).
COPY pyproject.toml ./
RUN pip install -e ".[dev]"

# Copy the rest of the project.
COPY . .

# Create a non-root user with ownership of the app dir and the host-mounted
# volumes (audit_logs, src, tests). Running as non-root limits damage from
# any container escape and is required by many security policies.
RUN useradd --create-home --shell /bin/bash --uid 1000 pipeline \
    && chown -R pipeline:pipeline /app
USER pipeline

# Default LLM config — the docker-compose stack overrides these to point at
# the ollama sidecar. Standalone `docker run` will use ollama at localhost.
ENV LLM_PROVIDER=ollama \
    LLM_MODEL=qwen2.5-coder:1.5b \
    LLM_BASE_URL=http://host.docker.internal:11434/v1 \
    LLM_API_KEY=ollama \
    SPEC_FILE=specs/user_authentication.yaml \
    PIPELINE_AUTO_APPROVE=1

ENTRYPOINT ["python", "run_pipeline.py"]
CMD ["specs/user_authentication.yaml", "--auto-approve"]
