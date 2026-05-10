from __future__ import annotations

import asyncio
import html
import logging
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.config import DeviceConfig, TakConfig, TransmitConfig
from src.models.node import Node
from src.models.packet import Packet, PacketType
from src.storage.node_repository import NodeRepository

logger = logging.getLogger(__name__)


@dataclass
class TakTarget:
    enabled: bool
    host: str
    port: int
    protocol: str


class TakPublisher:
    """Emit CoT position updates for nodes heard on the mesh."""

    def __init__(
        self,
        config: TakConfig,
        node_repo: NodeRepository,
        device_config: DeviceConfig | None = None,
        transmit_config: TransmitConfig | None = None,
    ):
        self._config = config
        self._node_repo = node_repo
        self._device_config = device_config
        self._transmit_config = transmit_config
        self._last_sent: dict[str, float] = {}
        self._last_self_sent = 0.0
        self._publish_count = 0
        self._last_error = ""
        self._last_target = ""
        self._last_sent_at: str | None = None

    @property
    def enabled(self) -> bool:
        return (
            self._config.enabled
            and bool(self._config.host)
            and int(self._config.port) > 0
        )

    @property
    def publish_count(self) -> int:
        return self._publish_count

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def last_target(self) -> str:
        return self._last_target

    @property
    def last_sent_at(self) -> str | None:
        return self._last_sent_at

    def mark_error(self, error: Exception | str) -> None:
        self._last_error = str(error)

    async def publish_packet(self, packet: Packet) -> None:
        if not self.enabled or packet.packet_type != PacketType.POSITION:
            return

        node = await self._node_repo.get_by_id(packet.source_id)
        if not node or not node.has_position:
            return

        now = asyncio.get_running_loop().time()
        last = self._last_sent.get(node.node_id, 0.0)
        if now - last < 5.0:
            return

        cot = self.build_cot(node, self._config)
        await asyncio.to_thread(
            self.send_cot,
            cot,
            TakTarget(
                enabled=True,
                host=self._config.host,
                port=self._config.port,
                protocol=self._config.protocol,
            ),
        )
        self._publish_count += 1
        self._last_error = ""
        self._last_target = f"{self._config.protocol.lower()}://{self._config.host}:{self._config.port}"
        self._last_sent_at = datetime.now(timezone.utc).isoformat()
        self._last_sent[node.node_id] = now

    async def publish_self_heartbeat(self) -> None:
        if not self.enabled or self._device_config is None:
            return
        if self._device_config.latitude is None or self._device_config.longitude is None:
            return

        now = asyncio.get_running_loop().time()
        interval = max(1.0, float(getattr(self._config, "publish_interval_seconds", 20) or 20))
        if now - self._last_self_sent < interval:
            return

        local_node = Node(
            node_id="self",
            long_name=(
                self._transmit_config.long_name
                if self._transmit_config and self._transmit_config.long_name
                else self._device_config.device_name
            ),
            short_name=(
                self._transmit_config.short_name
                if self._transmit_config and self._transmit_config.short_name
                else None
            ),
            latitude=self._device_config.latitude,
            longitude=self._device_config.longitude,
            altitude=self._device_config.altitude,
        )
        cot = self.build_cot(local_node, self._config)
        await asyncio.to_thread(
            self.send_cot,
            cot,
            TakTarget(
                enabled=True,
                host=self._config.host,
                port=self._config.port,
                protocol=self._config.protocol,
            ),
        )
        self._publish_count += 1
        self._last_error = ""
        self._last_target = f"{self._config.protocol.lower()}://{self._config.host}:{self._config.port}"
        self._last_sent_at = datetime.now(timezone.utc).isoformat()
        self._last_self_sent = now

    @staticmethod
    def node_callsign(node: Node, config: TakConfig) -> str:
        if config.use_meshtastic_names:
            if node.long_name:
                return node.long_name
            if node.short_name:
                return node.short_name
        return f"!{node.node_id}"

    @staticmethod
    def build_cot(node: Node, config: TakConfig) -> str:
        now = datetime.now(timezone.utc)
        stale = now + timedelta(seconds=max(30, int(config.stale_seconds)))
        callsign = html.escape(TakPublisher.node_callsign(node, config), quote=True)
        uid = html.escape(f"meshpoint-{node.node_id}", quote=True)
        cot_type = html.escape(config.cot_type, quote=True)
        team = html.escape(config.team, quote=True)
        role = html.escape(config.role, quote=True)
        color_name = str(getattr(config, "color", "Orange") or "Orange").strip().lower()
        color_argb = {
            "blue": "-16776961",
            "green": "-16711936",
            "yellow": "-256",
            "orange": "-23296",
            "red": "-65536",
            "purple": "-8388480",
            "pink": "-16181",
            "cyan": "-16711681",
            "white": "-1",
        }.get(color_name, "-23296")
        lat = node.latitude or 0.0
        lon = node.longitude or 0.0
        alt = node.altitude or 0.0
        return (
            f'<event version="2.0" uid="{uid}" type="{cot_type}" how="m-g" '
            f'time="{TakPublisher._iso(now)}" start="{TakPublisher._iso(now)}" '
            f'stale="{TakPublisher._iso(stale)}">'
            f'<point lat="{lat:.8f}" lon="{lon:.8f}" hae="{alt:.2f}" '
            f'ce="9999999.0" le="9999999.0"/>'
            f'<detail><contact callsign="{callsign}"/>'
            f'<__group name="{team}" role="{role}"/>'
            f'<color argb="{color_argb}"/></detail></event>'
        )

    @staticmethod
    def _iso(value: datetime) -> str:
        return value.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    @staticmethod
    def send_cot(cot: str, target: TakTarget) -> None:
        if not target.enabled:
            return

        proto = (target.protocol or "udp").strip().lower()
        data = cot.encode("utf-8")
        if proto == "tcp":
            with socket.create_connection((target.host, target.port), timeout=5) as sock:
                sock.sendall(data)
        else:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.sendto(data, (target.host, target.port))
        logger.info("TAK publish sent to %s:%s", target.host, target.port)

    async def send_test(self) -> None:
        dummy = Node(
            node_id="test0001",
            long_name="Meshpoint Test",
            short_name="TEST",
            latitude=0.0,
            longitude=0.0,
            altitude=0.0,
        )
        cot = self.build_cot(dummy, self._config)
        await asyncio.to_thread(
            self.send_cot,
            cot,
            TakTarget(
                enabled=self.enabled,
                host=self._config.host,
                port=self._config.port,
                protocol=self._config.protocol,
            ),
        )
