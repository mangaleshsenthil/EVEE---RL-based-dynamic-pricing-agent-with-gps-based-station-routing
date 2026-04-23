# builder (installs heavy ML dependencies)
FROM python:3.11-slim AS builder

WORKDIR /install

# System libs needed to compile psycopg2 & torch wheels
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        libgomp1 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Copy and install Python dependencies
COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --prefix=/install/packages --no-cache-dir -r requirements.txt


# runtime
FROM python:3.11-slim

WORKDIR /app

# Runtime system libraries only
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /install/packages /usr/local

# Copy application files
COPY ev_app.py          .
COPY ppo_ev_pricing.zip .
COPY sac_ev_pricing.zip .
COPY td3_ev_pricing.zip .

# Copy Streamlit config (NOT secrets — those come from env/volume)
COPY .streamlit/config.toml .streamlit/config.toml

# Create a non-root user for security
RUN useradd -m -u 1000 eveeuser && chown -R eveeuser:eveeuser /app
USER eveeuser

# Streamlit default port
EXPOSE 8501

# Health check — pings Streamlit's health endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8501/_stcore/health || exit 1

# Entrypoint
ENTRYPOINT ["streamlit", "run", "ev_app.py", \
            "--server.port=8501", \
            "--server.address=0.0.0.0", \
            "--server.headless=true", \
            "--browser.gatherUsageStats=false"]
