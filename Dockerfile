# Multi-stage build for WP-Bot (Track A + Track B + Landing Page)
# Deploy to Railway via: railway up

FROM python:3.12-slim AS base

# Prevent Python from writing .pyc files and enable unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy all source code first
COPY shared-contract/ shared-contract/
COPY track-a/ track-a/
COPY track-b/ track-b/
COPY site/ site/

# Install all packages (source must be present for editable installs)
RUN pip install --no-cache-dir -e ./shared-contract \
    && pip install --no-cache-dir -r track-a/requirements.txt \
    && pip install --no-cache-dir -r track-b/requirements.txt \
    && pip install --no-cache-dir -e ./track-a \
    && pip install --no-cache-dir -e ./track-b

# Create data directory for SQLite
RUN mkdir -p /app/data

# Copy startup script
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

# Expose ports
# Track A: $PORT (also serves static site at /), Track B: 8200 (internal)
EXPOSE 8200

# Default command: run both tracks via startup script
CMD ["/app/start.sh"]
