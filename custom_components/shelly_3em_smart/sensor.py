from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import ShellyAddonCoordinator

_LOGGER = logging.getLogger(__name__)


# ---------- static, panel-level sensors (one device, fixed entities) ----------

@dataclass(frozen=True, kw_only=True)
class ShellySensorDescription(SensorEntityDescription):
    pass


def _build_static_descriptions(info: dict) -> list[ShellySensorDescription]:
    a = info.get("channel_a_label") or "L1"
    b = info.get("channel_b_label") or "L2"
    c = info.get("channel_c_label") or "Spare"

    def power(key: str, label: str) -> ShellySensorDescription:
        return ShellySensorDescription(
            key=key,
            name=f"{label} Power",
            device_class=SensorDeviceClass.POWER,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfPower.WATT,
            suggested_display_precision=0,
        )

    def current(key: str, label: str) -> ShellySensorDescription:
        return ShellySensorDescription(
            key=key,
            name=f"{label} Current",
            device_class=SensorDeviceClass.CURRENT,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
            suggested_display_precision=2,
        )

    def voltage(key: str, label: str) -> ShellySensorDescription:
        return ShellySensorDescription(
            key=key,
            name=f"{label} Voltage",
            device_class=SensorDeviceClass.VOLTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfElectricPotential.VOLT,
            suggested_display_precision=1,
            entity_registry_enabled_default=False,
        )

    return [
        power("total_power", "Total"),
        ShellySensorDescription(
            key="total_current",
            name="Total Current",
            device_class=SensorDeviceClass.CURRENT,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
            suggested_display_precision=2,
        ),
        power("a_power", a),
        power("b_power", b),
        power("c_power", c),
        current("a_current", a),
        current("b_current", b),
        current("c_current", c),
        voltage("a_voltage", a),
        voltage("b_voltage", b),
        voltage("c_voltage", c),
    ]


def _panel_device_info(entry: ConfigEntry, coordinator: ShellyAddonCoordinator) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="Shelly 3EM Smart Monitor",
        manufacturer=MANUFACTURER,
        model=MODEL,
        sw_version=coordinator.info.get("version"),
        configuration_url=f"http://{entry.data['host']}:{entry.data['port']}/",
    )


def _appliance_device_info(entry: ConfigEntry, device_id: int, name: str) -> DeviceInfo:
    """Each detected appliance gets its own HA device so Energy Dashboard can
    track it individually."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_device_{device_id}")},
        name=name,
        manufacturer=MANUFACTURER,
        model="Detected appliance",
        via_device=(DOMAIN, entry.entry_id),
    )


class ShellyLiveSensor(CoordinatorEntity[ShellyAddonCoordinator], SensorEntity):
    """One of the panel-level meter readings from /api/live."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ShellyAddonCoordinator,
        entry: ConfigEntry,
        description: ShellySensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = _panel_device_info(entry, coordinator)

    @property
    def native_value(self):
        live = self.coordinator.data.get("live") or {}
        val = live.get(self.entity_description.key)
        return float(val) if val is not None else None


# ---------- dynamic per-appliance sensors (Power + Energy per detected device) ----------

class _DeviceSensorBase(CoordinatorEntity[ShellyAddonCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ShellyAddonCoordinator,
        entry: ConfigEntry,
        device_id: int,
        device_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_device_info = _appliance_device_info(entry, device_id, device_name)

    def _row(self) -> dict | None:
        for d in self.coordinator.data.get("devices") or []:
            try:
                if int(d["id"]) == self._device_id:
                    return d
            except (KeyError, ValueError, TypeError):
                pass
        return None

    @property
    def available(self) -> bool:
        return super().available and self._row() is not None


class ShellyDevicePowerSensor(_DeviceSensorBase):
    """Live wattage estimate: cluster mean_power when on, 0 when off."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator, entry, device_id, device_name) -> None:
        super().__init__(coordinator, entry, device_id, device_name)
        self._attr_name = "Power"
        self._attr_unique_id = f"{entry.entry_id}_device_{device_id}_power"

    @property
    def native_value(self):
        row = self._row()
        if row is None:
            return None
        val = row.get("current_power_w")
        return float(val) if val is not None else None


class ShellyDeviceEnergySensor(_DeviceSensorBase):
    """Cumulative kWh, monotonically increasing — suitable for the Energy Dashboard."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 3

    def __init__(self, coordinator, entry, device_id, device_name) -> None:
        super().__init__(coordinator, entry, device_id, device_name)
        self._attr_name = "Energy"
        self._attr_unique_id = f"{entry.entry_id}_device_{device_id}_energy"

    @property
    def native_value(self):
        row = self._row()
        if row is None:
            return None
        wh = row.get("current_energy_wh")
        if wh is None:
            return None
        return round(float(wh) / 1000.0, 6)


# ---------- platform setup ----------

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: ShellyAddonCoordinator = hass.data[DOMAIN][entry.entry_id]

    # Static panel sensors
    static = [
        ShellyLiveSensor(coordinator, entry, d)
        for d in _build_static_descriptions(coordinator.info)
    ]
    async_add_entities(static)

    # Dynamic per-device sensors: two per labelled appliance, registered as
    # they appear in /api/devices.
    known: set[int] = set()

    @callback
    def _add_new_devices() -> None:
        new: list[SensorEntity] = []
        for d in coordinator.data.get("devices") or []:
            try:
                device_id = int(d["id"])
            except (KeyError, ValueError, TypeError):
                continue
            if device_id in known:
                continue
            known.add(device_id)
            name = d.get("name") or f"Device {device_id}"
            new.append(ShellyDevicePowerSensor(coordinator, entry, device_id, name))
            new.append(ShellyDeviceEnergySensor(coordinator, entry, device_id, name))
        if new:
            async_add_entities(new)
            _LOGGER.info("Registered %d new per-device sensor(s)", len(new))

    _add_new_devices()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_devices))
