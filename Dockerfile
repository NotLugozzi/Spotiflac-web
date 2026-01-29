# Build stage
FROM python:3.11-slim as builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt


# Production stage
FROM python:3.11-slim

WORKDIR /app

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Create non-root user
RUN useradd -m -u 1000 spotiflac

# Copy Python packages from builder
COPY --from=builder /root/.local /home/spotiflac/.local

# Copy application code
COPY --chown=spotiflac:spotiflac app/ ./app/
COPY --chown=spotiflac:spotiflac requirements.txt .

# Create necessary directories
RUN mkdir -p /data /music /downloads \
    && chown -R spotiflac:spotiflac /app /data /music /downloads

# Set environment variables
ENV PATH=/home/spotiflac/.local/bin:$PATH
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV DATABASE_URL=sqlite:///./data/spotiflac.db
ENV MUSIC_LIBRARY_PATH=/music
ENV DOWNLOAD_PATH=/downloads

# Switch to non-root user
USER spotiflac

# Expose port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

# Run the application
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
