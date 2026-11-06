# syntax=docker/dockerfile:1.7
# Phase 4 reference image. Build from the repository root:
#   docker build --build-arg SOURCE_REVISION=$(git rev-parse HEAD) -t delivery-recovery:local .
# The base image digests are pinned; the Python dependency lock is Linux/Python 3.12.

FROM node:24-bookworm-slim@sha256:c86ecead9e55a5210ac1727de38a4ca840bf03dcaf34dc9749addbf383b1920c AS web

WORKDIR /build/web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM python:3.12-slim@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30 AS runtime

ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.title="delivery-recovery-assistant" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      org.opencontainers.image.source="local-reference"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DELIVERY_RECOVERY_DATA_DIR=/data

WORKDIR /app

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data \
    && chown -R app:app /data

COPY requirements/phase4-py312.lock /app/requirements.lock
RUN python -m pip install --no-cache-dir -r /app/requirements.lock \
    && python -m pip check \
    && python -m pip uninstall -y pip \
    && test ! -e /usr/local/bin/pip \
    && ! python -m pip --version >/dev/null 2>&1

COPY --chown=app:app pyproject.toml README.md LICENSE ./
COPY --chown=app:app src ./src
COPY --chown=app:app scripts/run_phase3_demo.py ./scripts/run_phase3_demo.py
COPY --chown=app:app scripts/phase4_container_smoke.py ./scripts/phase4_container_smoke.py
COPY --chown=app:app scripts/phase4_smoke_payload.json ./scripts/phase4_smoke_payload.json
COPY --chown=app:app scripts/phase4_backup_restore.py ./scripts/phase4_backup_restore.py
COPY --chown=app:app --from=web /build/web/dist ./web/dist

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=15s --start-period=60s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=2).status == 200 else 1)"]

CMD ["python", "scripts/run_phase3_demo.py", "--host", "0.0.0.0", "--port", "8000", "--data-dir", "/data"]
