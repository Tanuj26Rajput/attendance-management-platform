# syntax=docker/dockerfile:1

# ----------------------------- base -----------------------------------------
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# ----------------------------- deps -----------------------------------------
FROM base AS deps

COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements.txt

# ----------------------------- runtime --------------------------------------
FROM base AS runtime

COPY --from=deps /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY alembic.ini pyproject.toml ./
COPY alembic ./alembic
COPY app ./app
COPY dashboard ./dashboard

RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /srv
USER appuser

# ----------------------------- api ------------------------------------------
# The two services share every layer above and differ only in how they are
# started and how they are checked. They are separate stages rather than one
# image with two commands because a HEALTHCHECK is baked into the image: a
# single shared stage means the dashboard container inherits the API's probe,
# curls a port nothing is listening on, and is reported unhealthy forever --
# which, with `depends_on: service_healthy`, is enough to wedge a deploy.
FROM runtime AS api

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/v1/health || exit 1

# Exec form, so uvicorn is PID 1 and receives SIGTERM directly instead of
# having it swallowed by a shell. The graceful timeout gives in-flight
# requests a bounded window to finish before workers are closed.
# --proxy-headers makes uvicorn honour X-Forwarded-For, which is what puts the
# real client IP in the audit trail instead of the load balancer's. Which
# proxies may be believed is deliberately NOT hardcoded: uvicorn reads
# FORWARDED_ALLOW_IPS from the environment and defaults to 127.0.0.1, so a
# container exposed directly cannot have its audit IPs spoofed by a forged
# header. Set that variable to your ingress's address when you add one.
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--timeout-graceful-shutdown", "30", \
     "--proxy-headers"]

# ----------------------------- dashboard ------------------------------------
FROM runtime AS dashboard

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:8501/_stcore/health || exit 1

CMD ["streamlit", "run", "dashboard/app.py", \
     "--server.port=8501", \
     "--server.address=0.0.0.0", \
     "--server.headless=true", \
     "--browser.gatherUsageStats=false"]
