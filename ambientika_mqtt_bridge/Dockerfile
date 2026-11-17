ARG BUILD_FROM
FROM $BUILD_FROM

# Re-declare build arg so it is available after FROM.
ARG BUILD_VERSION
ENV BRIDGE_VERSION=${BUILD_VERSION}

LABEL \
  io.hass.name="Ambientika MQTT Bridge" \
  io.hass.description="MQTT bridge for Ambientika ventilation units" \
  io.hass.arch="${BUILD_ARCH}" \
  io.hass.type="addon" \
  io.hass.version="${BUILD_VERSION}" \
  maintainer="Südwind GmbH"

# Install the bridge dependencies with the Python of the base image.
# Compilers are only needed while pip builds wheels (e.g. on armv7/i386) and
# are removed in the same layer, so the image keeps only the runtime.
COPY requirements.txt /tmp/requirements.txt
RUN apk add --no-cache --virtual .build-deps \
      gcc \
      musl-dev \
      libffi-dev \
      make \
    && pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt \
    && apk del --no-network .build-deps \
    && rm -rf /tmp/requirements.txt /root/.cache

# Copy bridge script
COPY bridge.py /app/bridge.py

# Copy startup script
COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD [ "/run.sh" ]
