# Ambientika Matter Bridge - Dockerfile
# Build: docker build -t ambientika-matter-bridge .
# Run:   docker compose up -d

FROM python:3.11-slim

LABEL org.opencontainers.image.title="Ambientika Matter Bridge"
LABEL org.opencontainers.image.description="Bridges Ambientika ventilation units to Matter/Apple Home/Google Home"
LABEL org.opencontainers.image.source="https://github.com/ambientika-eu/ambientika-mqtt-bridge"
LABEL org.opencontainers.image.licenses="MIT"

# System dependencies for python-matter-server
RUN apt-get update && apt-get install -y --no-install-recommends \
        bluez \
        libbluetooth-dev \
        libglib2.0-dev \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bridge code
COPY ambientika_matter_bridge.py .

# Non-root user for security
RUN adduser --disabled-password --gecos "" bridgeuser
USER bridgeuser

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import paho.mqtt.client; import aiohttp; print('ok')" || exit 1

CMD ["python", "-u", "ambientika_matter_bridge.py"]
