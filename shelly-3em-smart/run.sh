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

# --- Auto-wire MQTT from HA's MQTT service when present ---
if bashio::services.available "mqtt"; then
    export MQTT_HOST="$(bashio::services 'mqtt' 'host')"
    export MQTT_PORT="$(bashio::services 'mqtt' 'port')"
    export MQTT_USER="$(bashio::services 'mqtt' 'username')"
    export MQTT_PASSWORD="$(bashio::services 'mqtt' 'password')"
    export MQTT_DISCOVERY_PREFIX="homeassistant"
    export MQTT_NODE_ID="shelly3em_smart"
    bashio::log.info "MQTT service detected: ${MQTT_HOST}:${MQTT_PORT}"
else
    bashio::log.notice "No MQTT service available; HA discovery disabled"
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
