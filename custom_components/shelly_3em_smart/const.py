from datetime import timedelta

DOMAIN = "shelly_3em_smart"

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 8080

CONF_HOST = "host"
CONF_PORT = "port"
CONF_TRACKED_ENTITIES = "tracked_entities"
CONF_ENERGY_ENTITIES = "energy_entities"
CONF_WEATHER_ENTITY = "weather_entity"
CONF_OUTDOOR_TEMP_ENTITY = "outdoor_temp_entity"
CONF_GAS_ENTITY = "gas_entity"
CONF_CLIMATE_ENTITIES = "climate_entities"

SCAN_INTERVAL = timedelta(seconds=2)

MANUFACTURER = "DIY"
MODEL = "shelly-3em-smart"

# Entity domains we listen to for state changes worth correlating against
# step events. Adding climate is intentionally omitted for v1 — climate state
# is the mode (heat_cool), not the running state; users wanting that should
# create a template binary_sensor mirroring hvac_action.
TRACKABLE_DOMAINS = ["switch", "light", "fan", "binary_sensor", "input_boolean"]

# Polling interval for energy sensors (HA cumulative kWh entities).
ENERGY_POLL_INTERVAL_S = 30

# Polling interval for the outside-temperature entity. Once a minute is more
# than enough for HDD/CDD calcs (which only need a per-day average).
WEATHER_POLL_INTERVAL_S = 60

# Gas meter readings change slowly; once every 5 minutes is plenty.
GAS_POLL_INTERVAL_S = 300

# Climate (setpoint) snapshots; once a minute catches manual changes quickly.
CLIMATE_POLL_INTERVAL_S = 60
