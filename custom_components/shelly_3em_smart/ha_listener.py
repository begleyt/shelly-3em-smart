"""Listen for state changes on user-selected entities and forward them to the
add-on so it can correlate with detected step events."""
from __future__ import annotations

import logging
from typing import Callable

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event

from .coordinator import ShellyAddonClient

_LOGGER = logging.getLogger(__name__)


def setup_ha_event_listener(
    hass: HomeAssistant,
    client: ShellyAddonClient,
    entity_ids: list[str],
) -> Callable[[], None]:
    """Attach a state-change listener. Returns an unsubscribe callable."""
    if not entity_ids:
        _LOGGER.info("HA event correlation: no entities selected")
        return lambda: None

    _LOGGER.info("HA event correlation: tracking %d entities", len(entity_ids))

    @callback
    def _on_state_change(event: Event) -> None:
        data = event.data
        entity_id = data.get("entity_id")
        old = data.get("old_state")
        new = data.get("new_state")
        if entity_id is None:
            return

        old_state = old.state if old is not None else None
        new_state = new.state if new is not None else None
        if old_state == new_state:
            # Attribute-only change; ignore.
            return

        friendly = None
        if new is not None:
            friendly = new.attributes.get("friendly_name")

        ts = event.time_fired.timestamp() if hasattr(event, "time_fired") else None

        hass.async_create_task(
            client.post_ha_event(
                entity_id=entity_id,
                old_state=old_state,
                new_state=new_state,
                friendly_name=friendly,
                ts=ts,
            )
        )

    return async_track_state_change_event(hass, entity_ids, _on_state_change)
