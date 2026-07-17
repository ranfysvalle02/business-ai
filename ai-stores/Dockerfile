# AI Stores — Dockerfile
# Multi-stage build for optimized image size

# --- Stage 1: Build dependencies ---
FROM python:3.11-slim-bookworm AS builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements file
COPY requirements.txt /tmp/requirements.txt

# Create virtual environment and install dependencies
RUN python -m venv /opt/venv && \
    . /opt/venv/bin/activate && \
    pip install --upgrade pip && \
    pip install --no-cache-dir -r /tmp/requirements.txt

# --- Stage 2: Final image ---
FROM python:3.11-slim-bookworm

WORKDIR /app

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv

# Set environment variables
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY templates /app/templates
COPY static /app/static
COPY manifest.json /app/manifest.json
COPY store_template.json /app/store_template.json
# Copy every top-level module (main.py, ai_editor.py, notifications.py, …) so a
# new module can never be silently left out of the image. .dockerignore keeps
# tests/dev/secrets out of the build context.
COPY *.py /app/

# Create non-root user for security
RUN addgroup --system app && \
    adduser --system --group app && \
    chown -R app:app /app

# Switch to non-root user
USER app

# Expose port
ARG APP_PORT=8000
ENV PORT=$APP_PORT
EXPOSE ${APP_PORT}

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:${PORT}/__mdb/health/live')" || exit 1

CMD ["python", "main.py"]
