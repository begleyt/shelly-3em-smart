import os
from dataclasses import dataclass, field
from typing import Optional


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    val = os.environ.get(name, default)
    if val is None or val == "":
        return None
    return val


def _env_int(name: str, default: int) -> int:
    val = _env(name)
    return int(val) if val is not None else default


def _env_float(name: str, default: float) -> float:
    val = _env(name)
    return float(val) if val is not None else default


def _env_bool(name: str, default: bool) -> bool:
    val = _env(name)
    if val is None:
        return default
    return val.lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    # Shelly device
    shelly_host: str = field(default_factory=lambda: _env("SHELLY_HOST", "192.168.1.50"))
    shelly_ws_path: str = field(default_factory=lambda: _env("SHELLY_WS_PATH", "/rpc"))

    # Channel mapping for US split-phase 240V panel: A=L1, B=L2, C=optional sub-circuit
    channel_a_label: str = field(default_factory=lambda: _env("CHANNEL_A_LABEL", "L1"))
    channel_b_label: str = field(default_factory=lambda: _env("CHANNEL_B_LABEL", "L2"))
    channel_c_label: str = field(default_factory=lambda: _env("CHANNEL_C_LABEL", "C"))

    # Event detection
    step_threshold_w: float = field(default_factory=lambda: _env_float("STEP_THRESHOLD_W", 50.0))
    step_window_s: float = field(default_factory=lambda: _env_float("STEP_WINDOW_S", 3.0))
    settle_window_s: float = field(default_factory=lambda: _env_float("SETTLE_WINDOW_S", 5.0))

    # Clustering
    cluster_interval_s: int = field(default_factory=lambda: _env_int("CLUSTER_INTERVAL_S", 3600))
    cluster_min_samples: int = field(default_factory=lambda: _env_int("CLUSTER_MIN_SAMPLES", 5))
    cluster_eps_w: float = field(default_factory=lambda: _env_float("CLUSTER_EPS_W", 25.0))

    # Storage
    db_path: str = field(default_factory=lambda: _env("DB_PATH", "/data/shelly.db"))
    sample_retention_days: int = field(default_factory=lambda: _env_int("SAMPLE_RETENTION_DAYS", 30))
    sample_downsample_s: int = field(default_factory=lambda: _env_int("SAMPLE_DOWNSAMPLE_S", 10))

    # MQTT (optional — leave empty to disable HA integration)
    mqtt_host: Optional[str] = field(default_factory=lambda: _env("MQTT_HOST"))
    mqtt_port: int = field(default_factory=lambda: _env_int("MQTT_PORT", 1883))
    mqtt_user: Optional[str] = field(default_factory=lambda: _env("MQTT_USER"))
    mqtt_password: Optional[str] = field(default_factory=lambda: _env("MQTT_PASSWORD"))
    mqtt_discovery_prefix: str = field(default_factory=lambda: _env("MQTT_DISCOVERY_PREFIX", "homeassistant"))
    mqtt_node_id: str = field(default_factory=lambda: _env("MQTT_NODE_ID", "shelly3em_smart"))

    # Web
    http_port: int = field(default_factory=lambda: _env_int("HTTP_PORT", 8080))

    # Cost (electricity rate × consumption shows in $ throughout the UI)
    electricity_rate_cents_per_kwh: float = field(default_factory=lambda: _env_float("ELECTRICITY_RATE_CENTS_PER_KWH", 0.0))
    currency_symbol: str = field(default_factory=lambda: _env("CURRENCY_SYMBOL", "$"))

    # Weather correlation. Set weather_entity_id to a HA weather.* or
    # sensor.<outside_temp> entity; the HACS integration will poll it and POST
    # readings to /api/weather_reading. hdd_cdd_base_temp_f is the balance-point
    # temperature for Heating- and Cooling-Degree-Day calcs (US convention is 65°F).
    weather_entity_id: Optional[str] = field(default_factory=lambda: _env("WEATHER_ENTITY_ID"))
    hdd_cdd_base_temp_f: float = field(default_factory=lambda: _env_float("HDD_CDD_BASE_TEMP_F", 65.0))
    temp_unit: str = field(default_factory=lambda: _env("TEMP_UNIT", "F"))   # 'F' | 'C' (display only)
    weather_retention_days: int = field(default_factory=lambda: _env_int("WEATHER_RETENTION_DAYS", 400))

    # Heating-fuel correlation. Useful when the HVAC is gas-fired (or propane /
    # oil): the HACS integration polls a cumulative-volume HA sensor and the
    # add-on normalises everything to therms. gas_rate_dollars_per_therm
    # mirrors electricity_rate_cents_per_kwh for cost overlays.
    gas_rate_dollars_per_therm: float = field(default_factory=lambda: _env_float("GAS_RATE_DOLLARS_PER_THERM", 0.0))
    heating_fuel_kind: str = field(default_factory=lambda: _env("HEATING_FUEL_KIND", "natural_gas"))   # informational only

    @property
    def mqtt_enabled(self) -> bool:
        return self.mqtt_host is not None


settings = Settings()
