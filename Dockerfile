# ─── Hyper Trading Bot ─── ARM64 / Raspberry Pi 5 ───
FROM python:3.11-slim-bookworm

WORKDIR /app

# System deps for pandas/numpy wheels
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gcc libffi-dev && \
    rm -rf /var/lib/apt/lists/*

# Install Python deps (cached unless requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY config.py main.py dashboard.py ./
COPY bot/ bot/
COPY data/ data/
COPY utils/ utils/

# Dashboard port
EXPOSE 5050

# Default: run the trading bot
CMD ["python", "main.py"]
