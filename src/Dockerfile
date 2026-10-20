# Build stage
FROM python:3.11-slim as builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libjpeg-dev \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies for FastAPI app
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# Create an isolated venv for SpotiFLAC to avoid dependency conflicts (pydantic, httpx, dotenv)
RUN python -m venv /opt/spotiflac-venv \
    # pydoll 3.x made ShadowRoot.inner_html a method; SpotiFLAC 5.0.1's Turnstile
    # solver still awaits it as a property, so it never finds the checkbox
    && /opt/spotiflac-venv/bin/pip install --no-cache-dir "spotiflac==5.0.1" "pydoll-python>=2.19.0,<3" \
    && mkdir -p /opt/spotiflac-venv/lib/python3.11/site-packages/webview \
    && echo "class Dummy:\n    def __getattr__(self, name):\n        return Dummy()\n    def __call__(self, *args, **kwargs):\n        return Dummy()\nimport sys\nsys.modules[__name__] = Dummy()" > /opt/spotiflac-venv/lib/python3.11/site-packages/webview/__init__.py \
    && sed -i 's/time_to_wait_captcha: float = 5/time_to_wait_captcha: float = 30/g' /opt/spotiflac-venv/lib/python3.11/site-packages/pydoll/browser/tab.py \
    && { f=/opt/spotiflac-venv/lib/python3.11/site-packages/pydoll/sync/_generated.py; \
         [ ! -f "$f" ] || sed -i 's/time_to_wait_captcha: float=5/time_to_wait_captcha: float=30/g' "$f"; }

# Production stage
FROM python:3.11-slim

WORKDIR /app

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libjpeg62-turbo \
    zlib1g \
    nodejs \
    npm \
    xvfb \
    xauth \
    chromium \
    && echo '#!/bin/sh\nexec /usr/bin/chromium --no-sandbox "$@"' > /usr/bin/google-chrome \
    && chmod +x /usr/bin/google-chrome \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Create non-root user
RUN useradd -m -u 1000 spotiflac

# Copy Python packages from builder
COPY --from=builder --chown=spotiflac:spotiflac /root/.local /home/spotiflac/.local
COPY --from=builder /opt/spotiflac-venv /opt/spotiflac-venv

# Copy application code
COPY --chown=spotiflac:spotiflac app/ ./app/
COPY --chown=spotiflac:spotiflac migrations/ ./migrations/
COPY --chown=spotiflac:spotiflac scripts/ ./scripts/
COPY --chown=spotiflac:spotiflac requirements.txt .

# Create necessary directories
RUN mkdir -p /data /music /app/downloads /home/spotiflac/.spotiflac \
    && chown -R spotiflac:spotiflac /app /data /music /home/spotiflac/.spotiflac

# Set environment variables
ENV PATH=/home/spotiflac/.local/bin:$PATH
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV DATABASE_URL=sqlite:///./data/spotiflac.db
ENV MUSIC_LIBRARY_PATH=/music
ENV DOWNLOAD_PATH=/app/downloads

# Switch to non-root user
USER spotiflac

# Expose port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

# Run the application
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
