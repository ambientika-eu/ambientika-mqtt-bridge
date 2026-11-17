FROM python:3.11-slim

ARG BUILD_VERSION
ENV BRIDGE_VERSION=${BUILD_VERSION}

LABEL \
  org.opencontainers.image.title="Ambientika MQTT Bridge" \
  org.opencontainers.image.description="MQTT bridge for Ambientika ventilation units" \
  org.opencontainers.image.source="https://github.com/ambientika-eu/ambientika-mqtt-bridge" \
  org.opencontainers.image.vendor="Südwind GmbH" \
  org.opencontainers.image.licenses="MIT"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bridge.py .

# bridge.py looks for its YAML at CONFIG_YAML (docker-compose mounts ./config.yaml
# there). Without the file it falls back to the environment variables.
ENV CONFIG_YAML=/config/config.yaml

CMD ["python", "bridge.py"]
