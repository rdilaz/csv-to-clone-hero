# ---------------------------------------------------------------------------
# CSV to Clone Hero - production image.
#
# Single-instance, single-process on purpose: one web process (waitress) with
# internal threads PLUS one background job worker. The 7-day query cache and
# per-user state live in a SQLite volume (see docs/runbook.md). ONE worker is
# the safest possible posture against Enchor's hard ~1-2 req/s per-IP cap for
# a public app sharing one server IP.
#
# Build:  docker build -t csv-to-clonehero:latest .
# Run:    see docker-compose.yml
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Runtime dependencies first (caching layer).
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application. src-layout package; wsgi.py is the entry point.
COPY pyproject.toml README.md ./
COPY src ./src
COPY browser_export ./browser_export
COPY wsgi.py ./

# Python needs rapidfuzz/hatch-free install of the src package into the image.
RUN pip install --no-cache-dir --no-deps .

# Non-root runtime user (best practice for a public service).
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data /app/state \
    && chown -R appuser:appuser /app /data
USER appuser

# SQLite state + transient downloads live on this volume.
VOLUME ["/data"]
ENV DATABASE_URL=sqlite:////data/csv_to_clonehero.db

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"

CMD ["python", "-m", "waitress", "--listen=0.0.0.0:8000", "--threads=8", "wsgi:app"]
