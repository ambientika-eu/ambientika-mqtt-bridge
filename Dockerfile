FROM python:3.11-slim

ARG BUILD_VERSION
ENV BRIDGE_VERSION=${BUILD_VERSION}

WORKDIR /app

# git is required for pip to install ambientika_py from its upstream Git repo
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bridge.py .

ENV CONFIG_PATH=/config/config.yaml

CMD ["python", "bridge.py"]
