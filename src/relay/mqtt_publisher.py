"""MQTT publisher with explicit enablement and optional channel allowlisting.

Gate 1: mqtt.enabled must be true (off by default).
Gate 2: if publish_channels is non-empty, only packets from those channels are
         published. An empty publish_channels list means "publish all decrypted
         channels seen by the gateway".

Supports dual-protocol publishing (Meshtastic + MeshCore) with optional
JSON mirror for Home Assistant and Node-RED consumers.
"""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from src.config import MqttConfig
from src.models.packet import Packet, PacketType, Protocol
from src.relay.channel_resolver import ChannelResolver
from src.relay.mqtt_formatter import (
    MeshCoreMqttFormatter,
    MeshtasticMqttFormatter,
    MqttMessage,
)

logger = logging.getLogger(__name__)

try:
    import paho.mqtt.client as paho_mqtt
    from paho.mqtt.client import CallbackAPIVersion
    PAHO_AVAILABLE = True
except ImportError:
    paho_mqtt = None
    CallbackAPIVersion = None
    PAHO_AVAILABLE = False


class MqttPublisher:
    """Publishes decoded packets to an MQTT broker.

    Enforces a two-gate safety model:
      1) MQTT must be explicitly enabled in config
      2) Only packets from whitelisted channels are published
    """

    def __init__(
        self,
        config: MqttConfig,
        device_name: str,
        channel_keys: Optional[dict[str, str]] = None,
        default_key_b64: str = "AQ==",
        primary_channel_name: str = "LongFast",
        device_position: Optional[tuple[float | None, float | None, float | None]] = None,
        self_identity: Optional[dict[str, str]] = None,
    ):
        self._config = config
        self._gateway_id = _generate_gateway_id(device_name)
        self._client: Optional[paho_mqtt.Client] = None
        self._connected = False
        self._publish_count = 0
        self._last_error = ""
        self._last_published_topic = ""
        self._last_published_at: str | None = None
        self._identity_cache: dict[str, dict[str, str]] = {}
        self._device_position = device_position or (None, None, None)
        self._self_identity = self_identity or {}
        self._primary_channel_name = (primary_channel_name or "LongFast").strip() or "LongFast"

        self._allowed_channels = {
            ch.strip().lower()
            for ch in (config.publish_channels or [])
            if str(ch).strip()
        }
        self._allow_all_channels = len(self._allowed_channels) == 0
        if not self._allow_all_channels:
            self._allowed_channels.add(self._primary_channel_name.lower())

        self._channel_resolver = ChannelResolver(
            channel_keys=channel_keys,
            default_key_b64=default_key_b64,
        )

        self._mt_formatter = MeshtasticMqttFormatter(
            topic_root=config.topic_root,
            region=config.region,
            gateway_id=self._gateway_id,
            api_key=config.api_key,
            location_precision=config.location_precision,
            channel_resolver=self._channel_resolver,
        )
        self._mc_formatter = MeshCoreMqttFormatter(
            topic_root=config.topic_root,
            region=config.region,
            gateway_id=self._gateway_id,
            api_key=config.api_key,
            location_precision=config.location_precision,
        )
        self._ha_discovery: Optional[HomeAssistantDiscovery] = None

    @property
    def gateway_id(self) -> str:
        return self._gateway_id

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def publish_count(self) -> int:
        return self._publish_count

    @property
    def broker(self) -> str:
        return self._config.broker

    @property
    def port(self) -> int:
        return self._config.port

    @property
    def auth_mode(self) -> str:
        return self._config.auth_mode

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def last_published_topic(self) -> str:
        return self._last_published_topic

    @property
    def last_published_at(self) -> str | None:
        return self._last_published_at

    def connect(self) -> bool:
        if not PAHO_AVAILABLE:
            logger.error(
                "paho-mqtt not installed. Run: "
                "sudo /opt/meshpoint/venv/bin/pip install paho-mqtt"
            )
            return False

        try:
            session_id = f"meshpoint-{self._gateway_id[1:]}"
            self._client = paho_mqtt.Client(
                CallbackAPIVersion.VERSION1,
                client_id=session_id,
                protocol=paho_mqtt.MQTTv311,
            )
            self._client.on_connect = self._on_connect
            self._client.on_disconnect = self._on_disconnect
            self._last_error = ""

            creds = self._mqtt_credentials()
            if creds is not None:
                username, password = creds
                self._client.username_pw_set(username, password)

            self._client.connect(
                self._config.broker, self._config.port, keepalive=60
            )
            self._client.loop_start()
            logger.info(
                "MQTT connecting to %s:%d as %s",
                self._config.broker, self._config.port, self._gateway_id,
            )
            return True
        except Exception as exc:
            self._last_error = str(exc)
            logger.exception("MQTT connection failed")
            return False

    def _mqtt_credentials(self) -> tuple[str, str | None] | None:
        mode = (self._config.auth_mode or "username_password").strip().lower()
        username = (self._config.username or "").strip()
        api_key = (self._config.api_key or "").strip()

        if mode in {"none", "anonymous"}:
            return None

        if username:
            return username, self._config.password

        if mode == "api_key":
            if api_key:
                return "mrk_" + hashlib.sha256(api_key.encode()).hexdigest()[:20], api_key
            return None

        logger.warning("MQTT auth_mode=%s but username is empty; continuing without auth", mode)
        return None

    def disconnect(self) -> None:
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()
            self._connected = False
            logger.info("MQTT disconnected")

    def publish(self, packet: Packet) -> bool:
        """Publish a packet if it passes the two-gate safety check."""
        if not self._connected or not self._client:
            return False

        if not self._passes_safety_gates(packet):
            return False

        messages = self._format_packet(packet)
        published = False
        for msg in messages:
            result = self._client.publish(msg.topic, msg.payload, qos=1)
            logger.debug("MQTT pub rc=%d topic=%s size=%d", result.rc, msg.topic, len(msg.payload))
            if result.rc == paho_mqtt.MQTT_ERR_SUCCESS:
                published = True
                self._last_published_topic = msg.topic
                self._last_published_at = datetime.now(timezone.utc).isoformat()
                logger.info(
                    "MQTT published src=%s type=%s proto=%s topic=%s",
                    packet.source_id,
                    packet.packet_type.value,
                    packet.protocol.value,
                    msg.topic,
                )

        if published:
            self._publish_count += 1
            if self._ha_discovery:
                self._ha_discovery.announce_node(packet)
        else:
            self._last_error = "publish returned non-success status"

        return published

    def _passes_safety_gates(self, packet: Packet) -> bool:
        if packet.packet_type == PacketType.ENCRYPTED:
            return False

        if not packet.decrypted and packet.encrypted_payload:
            return False

        if self._allow_all_channels:
            return True

        channel_name = self._resolve_channel_name(packet)
        if channel_name.lower() not in self._allowed_channels:
            # If the network uses a non-stock PSK and we don't have enough key
            # context to name the primary channel cleanly, don't silently drop
            # standard Meshtastic traffic just because it resolved to chNN.
            if (
                packet.protocol == Protocol.MESHTASTIC
                and channel_name.startswith("ch")
                and self._primary_channel_name.lower() in self._allowed_channels
            ):
                logger.info(
                    "MQTT allowing Meshtastic packet %s on unresolved primary channel %s",
                    packet.packet_id,
                    channel_name,
                )
                return True
            logger.debug(
                "MQTT gate 2 blocked: channel '%s' not in allowed list", channel_name
            )
            return False

        return True

    def _resolve_channel_name(self, packet: Packet) -> str:
        return self._channel_resolver.resolve(
            packet.channel_hash, packet.protocol
        )

    def _format_packet(self, packet: Packet) -> list[MqttMessage]:
        messages: list[MqttMessage] = []
        api_key_mode = (self._config.auth_mode or "").strip().lower() == "api_key"
        broker_host = (self._config.broker or "").strip().lower()
        local_json_ingest_mode = (
            bool((self._config.api_key or "").strip())
            or broker_host not in {"mqtt.meshtastic.org", "broker.meshtastic.org"}
        )
        packet = self._enrich_packet(packet)

        if packet.protocol == Protocol.MESHTASTIC:
            if api_key_mode:
                json_msg = self._mt_formatter.format_json(packet)
                if json_msg:
                    messages.append(json_msg)
            else:
                msg = self._mt_formatter.format(packet)
                if msg:
                    messages.append(msg)
                if self._config.publish_json or local_json_ingest_mode:
                    json_msg = self._mt_formatter.format_json(packet)
                    if json_msg:
                        messages.append(json_msg)

        elif packet.protocol == Protocol.MESHCORE:
            msg = self._mc_formatter.format(packet)
            if msg:
                messages.append(msg)

        return messages

    def publish_self_snapshot(self) -> bool:
        if not self._connected or not self._client:
            return False

        lat, lon, alt = self._device_position
        if lat is None or lon is None:
            return False

        topic = f"{self._config.topic_root}/{self._config.region}/2/json/LongFast/{self._gateway_id}"
        if self._config.api_key:
            topic = f"{topic}/key/{self._config.api_key}"

        payload = {
            "id": f"self-{self._gateway_id[1:]}",
            "from": self._gateway_id,
            "node_id": self._gateway_id,
            "to": "ffffffff",
            "type": "position",
            "sender": self._gateway_id,
            "timestamp": int(datetime.now(timezone.utc).timestamp()),
            "latitude": lat,
            "longitude": lon,
            "altitude": alt or 0.0,
            "long_name": self._self_identity.get("long_name") or self._self_identity.get("device_name") or "Meshpoint",
            "short_name": self._self_identity.get("short_name") or "MPNT",
            "role": self._self_identity.get("role") or "gateway",
            "payload": {
                "latitude": lat,
                "longitude": lon,
                "altitude": alt or 0.0,
                "long_name": self._self_identity.get("long_name") or self._self_identity.get("device_name") or "Meshpoint",
                "short_name": self._self_identity.get("short_name") or "MPNT",
                "role": self._self_identity.get("role") or "gateway",
            },
        }
        if self._config.api_key:
            payload["api_key"] = self._config.api_key

        result = self._client.publish(topic, json.dumps(payload).encode(), qos=1)
        if result.rc == paho_mqtt.MQTT_ERR_SUCCESS:
            self._last_published_topic = topic
            self._last_published_at = datetime.now(timezone.utc).isoformat()
            self._publish_count += 1
            return True
        self._last_error = "self snapshot publish returned non-success status"
        return False

    def _enrich_packet(self, packet: Packet) -> Packet:
        payload = dict(packet.decoded_payload or {})
        identity = self._extract_identity(payload)
        if identity:
            cached = self._identity_cache.setdefault(packet.source_id, {})
            cached.update(identity)
        cached_identity = self._identity_cache.get(packet.source_id, {})
        if cached_identity:
            for key, value in cached_identity.items():
                payload.setdefault(key, value)
        if payload == (packet.decoded_payload or {}):
            return packet
        return replace(packet, decoded_payload=payload)

    @staticmethod
    def _extract_identity(payload: dict) -> dict[str, str]:
        out: dict[str, str] = {}
        for key in ("long_name", "short_name", "role", "hw_model"):
            value = payload.get(key)
            if value not in (None, ""):
                out[key] = str(value)
        return out

    def _on_connect(self, client, userdata, flags, rc) -> None:
        if rc == 0:
            self._connected = True
            self._last_error = ""
            logger.info("MQTT connected to %s as %s", self._config.broker, self._gateway_id)
            self.publish_self_snapshot()
            if self._config.homeassistant_discovery and self._client:
                self._ha_discovery = HomeAssistantDiscovery(self._client, self._gateway_id)
        else:
            self._connected = False
            self._last_error = f"broker refused connection (rc={rc})"
            logger.warning("MQTT connection refused (rc=%d)", rc)

    def _on_disconnect(self, client, userdata, rc) -> None:
        self._connected = False
        if rc != 0:
            self._last_error = f"unexpected disconnect (rc={rc})"
            logger.warning("MQTT unexpected disconnect (rc=%d), auto-reconnecting", rc)

class HomeAssistantDiscovery:
    """Publishes HA auto-discovery configs for mesh node sensors."""

    DISCOVERY_PREFIX = "homeassistant"

    def __init__(self, client: paho_mqtt.Client, gateway_id: str):
        self._client = client
        self._gateway_id = gateway_id
        self._announced_nodes: set[str] = set()

    def announce_node(self, packet: Packet) -> None:
        if not packet.decoded_payload:
            return

        node_id = packet.source_id
        if node_id in self._announced_nodes:
            return

        payload = packet.decoded_payload
        sensors = []

        if payload.get("battery_level") is not None:
            sensors.append(self._battery_config(node_id))
        if payload.get("temperature") is not None:
            sensors.append(self._temperature_config(node_id))
        if payload.get("latitude") is not None:
            sensors.append(self._gps_tracker_config(node_id))

        for topic, config_payload in sensors:
            self._client.publish(topic, config_payload, qos=1, retain=True)

        if sensors:
            self._announced_nodes.add(node_id)

    def _device_block(self, node_id: str) -> dict:
        return {
            "identifiers": [f"meshpoint_{node_id}"],
            "name": f"Mesh Node {node_id[-4:]}",
            "manufacturer": "Meshtastic",
            "via_device": self._gateway_id,
        }

    def _battery_config(self, node_id: str) -> tuple[str, str]:
        import json
        topic = f"{self.DISCOVERY_PREFIX}/sensor/meshpoint_{node_id}/battery/config"
        config = {
            "name": "Battery",
            "unique_id": f"meshpoint_{node_id}_battery",
            "device": self._device_block(node_id),
            "state_topic": f"meshpoint/{node_id}/telemetry",
            "value_template": "{{ value_json.battery_level }}",
            "unit_of_measurement": "%",
            "device_class": "battery",
        }
        return topic, json.dumps(config)

    def _temperature_config(self, node_id: str) -> tuple[str, str]:
        import json
        topic = f"{self.DISCOVERY_PREFIX}/sensor/meshpoint_{node_id}/temperature/config"
        config = {
            "name": "Temperature",
            "unique_id": f"meshpoint_{node_id}_temperature",
            "device": self._device_block(node_id),
            "state_topic": f"meshpoint/{node_id}/telemetry",
            "value_template": "{{ value_json.temperature }}",
            "unit_of_measurement": "°C",
            "device_class": "temperature",
        }
        return topic, json.dumps(config)

    def _gps_tracker_config(self, node_id: str) -> tuple[str, str]:
        import json
        topic = f"{self.DISCOVERY_PREFIX}/device_tracker/meshpoint_{node_id}/config"
        config = {
            "name": "Location",
            "unique_id": f"meshpoint_{node_id}_tracker",
            "device": self._device_block(node_id),
            "json_attributes_topic": f"meshpoint/{node_id}/position",
            "source_type": "gps",
        }
        return topic, json.dumps(config)


def _generate_gateway_id(device_name: str) -> str:
    """Deterministic Meshtastic-format gateway ID from device name.

    Format: !XXXXXXXX (8 hex chars) derived from device name hash.
    Broker requires standard node ID format for ServiceEnvelope distribution.
    """
    import hashlib
    digest = hashlib.md5(device_name.lower().encode(), usedforsecurity=False).hexdigest()[:8]  # nosec B324
    return f"!{digest}"
