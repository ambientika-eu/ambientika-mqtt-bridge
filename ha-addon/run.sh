#!/usr/bin/with-contenv bashio

# Read configuration from HA Add-on options
AMBIENTIKA_USERNAME=$(bashio::config 'ambientika_username')
AMBIENTIKA_PASSWORD=$(bashio::config 'ambientika_password')
MQTT_HOST=$(bashio::config 'mqtt_host')
MQTT_PORT=$(bashio::config 'mqtt_port')
MQTT_USERNAME=$(bashio::config 'mqtt_username')
MQTT_PASSWORD=$(bashio::config 'mqtt_password')
MQTT_TOPIC_PREFIX=$(bashio::config 'mqtt_topic_prefix')
POLL_INTERVAL=$(bashio::config 'poll_interval')
LOG_LEVEL=$(bashio::config 'log_level')
SLAVE_FILTER_SOFT_RESET=$(bashio::config 'slave_filter_soft_reset')
FILTER_ACK_TTL_DAYS=$(bashio::config 'filter_ack_ttl_days')

bashio::log.info "Starting Ambientika MQTT Bridge..."
bashio::log.info "MQTT Broker: ${MQTT_HOST}:${MQTT_PORT}"
bashio::log.info "Topic prefix: ${MQTT_TOPIC_PREFIX}"
bashio::log.info "Poll interval: ${POLL_INTERVAL}s"

# Export as environment variables for bridge.py
export AMBIENTIKA_USERNAME
export AMBIENTIKA_PASSWORD
export MQTT_HOST
export MQTT_PORT
export MQTT_USERNAME
export MQTT_PASSWORD
export MQTT_TOPIC_PREFIX
export POLL_INTERVAL
export LOG_LEVEL
export SLAVE_FILTER_SOFT_RESET
export FILTER_ACK_TTL_DAYS

# Run the bridge
exec python3 /app/bridge.py
