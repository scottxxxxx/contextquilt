# Context Quilt - Dockerfile
# Multi-stage build for optimized production image

# ============================================
# Stage 1: Builder
# ============================================
FROM python:3.11-slim as builder

WORKDIR /build

# Install build dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

# ============================================
# Stage 2: Runtime
# ============================================
FROM python:3.11-slim

LABEL maintainer="Context Quilt Team"
LABEL description="Context Quilt - LLM Gateway with Unified Memory"
LABEL version="1.0.0"

# Create non-root user for security
RUN useradd -m -u 1000 contextquilt && \
    mkdir -p /app /app/logs && \
    chown -R contextquilt:contextquilt /app

WORKDIR /app

# Install runtime dependencies
# Graphviz from Debian apt is 2.42.4 which has a known SVG viewBox bug
# (regression from 2.38.0, fixed in 13.0.0). Install the official
# graphviz release deb to get correct SVG output.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    libltdl7 \
    libgts-0.7-5 \
    libexpat1 \
    libgd3 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libcairo2 \
    fontconfig \
    fonts-dejavu-core \
    && curl -fsSL \
       "https://gitlab.com/api/v4/projects/4207231/packages/generic/graphviz-releases/14.1.4/ubuntu_22.04_graphviz-14.1.4-cmake.deb" \
       -o /tmp/graphviz.deb \
    && apt-get install -y /tmp/graphviz.deb \
    && rm /tmp/graphviz.deb \
    && rm -rf /var/lib/apt/lists/*

# Copy Python packages from builder
COPY --from=builder /root/.local /home/contextquilt/.local

# Copy application code
COPY --chown=contextquilt:contextquilt src/ ./src/
COPY --chown=contextquilt:contextquilt README.md .

# Switch to non-root user
USER contextquilt

# Add local bin to PATH
ENV PATH=/home/contextquilt/.local/bin:$PATH
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Run the application
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
