from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import ShellyAddonCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one binary_sensor per labelled device, dynamically as they appear."""
    coordinator: ShellyAddonCoordinator = hass.data[DOMAIN][entry.entry_id]
    known: set[int] = set()

    @callback
    def _add_new_devices() -> None:
        new: list[ShellyDeviceBinarySensor] = []
        for d in coordinator.data.get("devices") or []:
            try:
                device_id = int(d["id"])
            except (KeyError, ValueError, TypeError):
                continue
            if device_id in known:
                continue
            known.add(device_id)
            new.append(ShellyDeviceBinarySensor(coordinator, entry, device_id, d.get("name", f"Device {device_id}")))
        if new:
            async_add_entities(new)
            _LOGGER.info("Registered %d new device binary_sensor(s)", len(new))

    _add_new_devices()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_devices))


class ShellyDeviceBinarySensor(CoordinatorEntity[ShellyAddonCoordinator], BinarySensorEntity):
    """Binary sensor reflecting whether the add-on's inferrer thinks a device is on."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.POWER

    def __init__(
        self,
        coordinator: ShellyAddonCoordinator,
        entry: ConfigEntry,
        device_id: int,
        name: str,
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_name = name
        self._attr_unique_id = f"{entry.entry_id}_device_{device_id}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Shelly 3EM Smart Monitor",
            manufacturer=MANUFACTURER,
            model=MODEL,
            sw_version=coordinator.info.get("version"),
        )

    def _device_row(self) -> dict | None:
        for d in self.coordinator.data.get("devices") or []:
            try:
                if int(d["id"]) == self._device_id:
                    return d
            except (KeyError, ValueError, TypeError):
                pass
        return None

    @property
    def available(self) -> bool:
        return super().available and self._device_row() is not None

    @property
    def is_on(self) -> bool | None:
        row = self._device_row()
        if row is None:
            return None
        return bool(row.get("is_on"))

    @property
    def extra_state_attributes(self) -> dict | None:
        row = self._device_row()
        if row is None:
            return None
        return {
            "last_on": row.get("last_on_ts"),
            "last_off": row.get("last_off_ts"),
            "notes": row.get("notes"),
        }
