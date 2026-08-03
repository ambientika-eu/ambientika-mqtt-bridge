ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:latest
FROM $BUILD_FROM

# Re-declare build-time ARGs inside the build stage so they are available
# to LABEL and other instructions after FROM (Docker scoping rule).
ARG BUILD_ARCH
ARG BUILD_VERSION
ENV BRIDGE_VERSION=${BUILD_VERSION}

LABEL \
    io.hass.name="Ambientika MQTT Bridge" \
        io.hass.description="MQTT bridge for Ambientika ventilation units" \
            io.hass.arch="${BUILD_ARCH}" \
                io.hass.type="addon" \
                    io.hass.version="${BUILD_VERSION}" \
                        maintainer="Ambientika / SUEDWIND"

                        # Install Python plus build tools needed for aiohttp / yarl / multidict on ARM
                        RUN apk add --no-cache \
                            python3 \
                                py3-pip \
                                    git \
                                        gcc \
                                            musl-dev \
                                                python3-dev \
                                                    libffi-dev \
                                                        openssl-dev \
                                                            cargo \
                                                                make

                                                                # Install bridge dependencies (PEP 668: system Python is externally managed)
                                                                COPY requirements.txt /tmp/requirements.txt
                                                                RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt

                                                                # Copy bridge script
                                                                COPY bridge.py /app/bridge.py

                                                                # Copy startup script
                                                                COPY run.sh /run.sh
                                                                RUN chmod +x /run.sh

                                                                CMD [ "/run.sh" ]
                                                                
