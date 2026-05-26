import asyncio
import json
import logging
import time
from typing import Optional

from .config import settings

log = logging.getLogger(__name__)

try:
    import paho.mqtt.client as mqtt
    HAVE_MQTT = True
except ImportError:
    HAVE_MQTT = False


class MqttPublisher:
    def __init__(self) -> None:
        self._client: Optional["mqtt.Client"] = None
        self._connected = False
        self._discovered_devices: set = set()
        self._discovered_sensors = False

    def start(self) -> None:
        if not settings.mqtt_enabled:
            log.info("MQTT disabled (no MQTT_HOST set)")
            return
        if not HAVE_MQTT:
            log.warning("paho-mqtt not installed; MQTT integration disabled")
            return
        self._client = mqtt.Client(client_id=settings.mqtt_node_id, clean_session=True)
        if settings.mqtt_user:
            self._client.username_pw_set(settings.mqtt_user, settings.mqtt_password or "")
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        try:
            self._client.connect_async(settings.mqtt_host, settings.mqtt_port, keepalive=60)
            self._client.loop_start()
        except Exception:
            log.exception("MQTT connect failed")

    def stop(self) -> None:
        if self._client is None:
            return
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:
            pass

    def _on_connect(self, client, userdata, flags, rc):
        log.info("MQTT connected rc=%s", rc)
        self._connected = (rc == 0)
        if self._connected:
            self._publish_sensor_discovery()

    def _on_disconnect(self, client, userdata, rc):
        log.warning("MQTT disconnected rc=%s", rc)
        self._connected = False

    @property
    def base_topic(self) -> str:
        return f"{settings.mqtt_node_id}"

    def _device_block(self) -> dict:
        return {
            "identifiers": [settings.mqtt_node_id],
            "name": "Shelly 3EM Smart Monitor",
            "manufacturer": "DIY",
            "model": "shelly-3em-smart",
        }

    def _publish_sensor_discovery(self) -> None:
        if self._discovered_sensors or not self._client:
            return
        prefix = settings.mqtt_discovery_prefix
        node = settings.mqtt_node_id

        sensors = [
            ("total_power", "Total Power", "W", "power", "measurement"),
            ("total_current", "Total Current", "A", "current", "measurement"),
            ("a_power", f"{settings.channel_a_label} Power", "W", "power", "measurement"),
            ("b_power", f"{settings.channel_b_label} Power", "W", "power", "measurement"),
            ("c_power", f"{settings.channel_c_label} Power", "W", "power", "measurement"),
            ("a_current", f"{settings.channel_a_label} Current", "A", "current", "measurement"),
            ("b_current", f"{settings.channel_b_label} Current", "A", "current", "measurement"),
            ("c_current", f"{settings.channel_c_label} Current", "A", "current", "measurement"),
        ]
        for key, name, unit, dev_class, state_class in sensors:
            topic = f"{prefix}/sensor/{node}/{key}/config"
            payload = {
                "name": name,
                "unique_id": f"{node}_{key}",
                "state_topic": f"{self.base_topic}/state",
                "value_template": f"{{{{ value_json.{key} }}}}",
                "unit_of_measurement": unit,
                "device_class": dev_class,
                "state_class": state_class,
                "device": self._device_block(),
            }
            self._client.publish(topic, json.dumps(payload), retain=True)
        self._discovered_sensors = True

    def publish_device_discovery(self, device_id: int, name: str) -> None:
        if not self._connected or not self._client:
            return
        if device_id in self._discovered_devices:
            return
        prefix = settings.mqtt_discovery_prefix
        node = settings.mqtt_node_id
        slug = f"device_{device_id}"
        topic = f"{prefix}/binary_sensor/{node}/{slug}/config"
        payload = {
            "name": name,
            "unique_id": f"{node}_{slug}",
            "state_topic": f"{self.base_topic}/device/{device_id}/state",
            "payload_on": "on",
            "payload_off": "off",
            "device_class": "power",
            "device": self._device_block(),
        }
        self._client.publish(topic, json.dumps(payload), retain=True)
        self._discovered_devices.add(device_id)

    def publish_state(self, sample: dict) -> None:
        if not self._connected or not self._client:
            return
        payload = {k: sample.get(k) for k in (
            "total_power", "total_current",
            "a_power", "b_power", "c_power",
            "a_current", "b_current", "c_current",
        )}
        self._client.publish(f"{self.base_topic}/state", json.dumps(payload), retain=False)

    def publish_device_state(self, device_id: int, state: str) -> None:
        if not self._connected or not self._client:
            return
        self._client.publish(f"{self.base_topic}/device/{device_id}/state", state, retain=True)


publisher = MqttPublisher()
