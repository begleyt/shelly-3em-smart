import json
import logging
import socket
from typing import Optional

from .config import settings

log = logging.getLogger(__name__)

try:
    import paho.mqtt.client as mqtt
    HAVE_MQTT = True
except ImportError:
    HAVE_MQTT = False


def _make_client():
    """Build a paho-mqtt Client that works on both paho-mqtt 1.x and 2.x.

    paho-mqtt 2.x requires `callback_api_version`. We use VERSION1 so our
    legacy on_connect/on_disconnect signatures remain valid on both APIs.
    """
    kwargs = dict(client_id=settings.mqtt_node_id, clean_session=True)
    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, **kwargs)
    except AttributeError:
        return mqtt.Client(**kwargs)


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

        log.info("MQTT starting: broker=%s:%s user=%s prefix=%s",
                 settings.mqtt_host, settings.mqtt_port,
                 settings.mqtt_user or "(none)",
                 settings.mqtt_discovery_prefix)

        try:
            self._client = _make_client()
        except Exception:
            log.exception("MQTT: failed to construct paho client")
            return

        if settings.mqtt_user:
            self._client.username_pw_set(settings.mqtt_user, settings.mqtt_password or "")
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect

        # Try a synchronous connect first so socket/auth errors surface in the
        # log immediately. If it works, hand off to the background loop which
        # will keep the connection alive and handle reconnects.
        try:
            self._client.connect(settings.mqtt_host, settings.mqtt_port, keepalive=60)
        except socket.gaierror as e:
            log.error("MQTT: cannot resolve broker hostname '%s': %s", settings.mqtt_host, e)
            self._client = None
            return
        except (ConnectionRefusedError, OSError) as e:
            log.error("MQTT: cannot reach broker %s:%s — %s",
                      settings.mqtt_host, settings.mqtt_port, e)
            self._client = None
            return
        except Exception:
            log.exception("MQTT: unexpected error connecting to broker")
            self._client = None
            return

        self._client.loop_start()
        log.info("MQTT: connect() returned ok; background loop started")

    def stop(self) -> None:
        if self._client is None:
            return
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:
            pass

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            log.info("MQTT connected (rc=0)")
        else:
            log.error("MQTT connect rejected (rc=%s): %s", rc, _rc_meaning(rc))
        self._connected = (rc == 0)
        if self._connected:
            self._publish_sensor_discovery()

    def _on_disconnect(self, client, userdata, rc):
        if rc != 0:
            log.warning("MQTT disconnected unexpectedly (rc=%s)", rc)
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
        log.info("MQTT: published discovery config for %d sensors under %s/sensor/%s/*",
                 len(sensors), prefix, node)
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
        log.info("MQTT: published discovery config for device %d (%s)", device_id, name)
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


def _rc_meaning(rc: int) -> str:
    return {
        1: "incorrect protocol version",
        2: "invalid client identifier",
        3: "server unavailable",
        4: "bad username or password",
        5: "not authorized",
    }.get(rc, f"code {rc}")


publisher = MqttPublisher()
