#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
set -e

# --- Map add-on options.json -> env vars our Python app already reads ---
export SHELLY_HOST="$(bashio::config 'shelly_host')"
export CHANNEL_A_LABEL="$(bashio::config 'channel_a_label')"
export CHANNEL_B_LABEL="$(bashio::config 'channel_b_label')"
export CHANNEL_C_LABEL="$(bashio::config 'channel_c_label')"
export STEP_THRESHOLD_W="$(bashio::config 'step_threshold_w')"
export STEP_WINDOW_S="$(bashio::config 'step_window_s')"
export SETTLE_WINDOW_S="$(bashio::config 'settle_window_s')"
export CLUSTER_INTERVAL_S="$(bashio::config 'cluster_interval_s')"
export CLUSTER_MIN_SAMPLES="$(bashio::config 'cluster_min_samples')"
export CLUSTER_EPS_W="$(bashio::config 'cluster_eps_w')"
export SAMPLE_RETENTION_DAYS="$(bashio::config 'sample_retention_days')"
export SAMPLE_DOWNSAMPLE_S="$(bashio::config 'sample_downsample_s')"

export DB_PATH="/data/shelly.db"
export HTTP_PORT="8080"

# --- MQTT wiring ---
# Three sources, in priority order when mqtt_enabled = true:
#   1. Manual host filled in by the user
#   2. HA's MQTT service (Mosquitto broker add-on)
#   3. Off
if bashio::config.true 'mqtt_enabled'; then
    manual_host="$(bashio::config 'mqtt_host')"
    if bashio::var.has_value "${manual_host}"; then
        export MQTT_HOST="${manual_host}"
        export MQTT_PORT="$(bashio::config 'mqtt_port')"
        export MQTT_USER="$(bashio::config 'mqtt_username')"
        export MQTT_PASSWORD="$(bashio::config 'mqtt_password')"
        bashio::log.info "MQTT: using manually-configured broker ${MQTT_HOST}:${MQTT_PORT}"
    elif bashio::services.available "mqtt"; then
        export MQTT_HOST="$(bashio::services 'mqtt' 'host')"
        export MQTT_PORT="$(bashio::services 'mqtt' 'port')"
        export MQTT_USER="$(bashio::services 'mqtt' 'username')"
        export MQTT_PASSWORD="$(bashio::services 'mqtt' 'password')"
        bashio::log.info "MQTT: using HA-managed broker ${MQTT_HOST}:${MQTT_PORT}"
    else
        bashio::log.warning "MQTT enabled but no broker is configured and no HA MQTT service is available; discovery disabled"
    fi
    export MQTT_DISCOVERY_PREFIX="$(bashio::config 'mqtt_discovery_prefix')"
    export MQTT_NODE_ID="shelly3em_smart"
else
    bashio::log.info "MQTT integration disabled"
fi

if bashio::var.is_empty "${SHELLY_HOST}"; then
    bashio::log.fatal "shelly_host is not set in the add-on options"
    exit 1
fi

bashio::log.info "Starting Shelly 3EM Smart Monitor (Shelly: ${SHELLY_HOST})"
cd /srv
exec python3 -m uvicorn app.main:app \
    --host 0.0.0.0 \
    --port "${HTTP_PORT}" \
    --log-level "$(bashio::config 'log_level')"
