from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfElectricCurrent, UnitOfElectricPotential, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import ShellyAddonCoordinator


@dataclass(frozen=True, kw_only=True)
class ShellySensorDescription(SensorEntityDescription):
    """Sensor description that knows which channel label to fold into the name."""

    label_key: str | None = None  # one of "a", "b", "c" or None for totals


def _build_descriptions(info: dict) -> list[ShellySensorDescription]:
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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: ShellyAddonCoordinator = hass.data[DOMAIN][entry.entry_id]
    descriptions = _build_descriptions(coordinator.info)
    async_add_entities(ShellySensor(coordinator, entry, d) for d in descriptions)


class ShellySensor(CoordinatorEntity[ShellyAddonCoordinator], SensorEntity):
    """Numeric reading pulled from the add-on's /api/live response."""

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
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Shelly 3EM Smart Monitor",
            manufacturer=MANUFACTURER,
            model=MODEL,
            sw_version=coordinator.info.get("version"),
            configuration_url=f"http://{entry.data['host']}:{entry.data['port']}/",
        )

    @property
    def native_value(self):
        live = self.coordinator.data.get("live") or {}
        val = live.get(self.entity_description.key)
        if val is None:
            return None
        return float(val)
