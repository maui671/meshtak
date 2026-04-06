#!/usr/bin/env python3
import json
import logging
import os
import queue
import re
import socket
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any, Dict, Optional

from meshtastic.serial_interface import SerialInterface
from meshtastic.tcp_interface import TCPInterface
from pubsub import pub

from node_store import NodeStore

BASE_DIR = "/opt/meshtak"
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(BASE_DIR, "logs")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
LOG_PATH = os.path.join(LOG_DIR, "meshtak.log")


def ensure_log_dir() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)


def setup_logging() -> logging.Logger:
    ensure_log_dir()
    logger = logging.getLogger("meshtak")
    logger.setLevel(logging.INFO)

    if logger.handlers:
        return logger

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    file_handler = logging.FileHandler(LOG_PATH)
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    logger.propagate = False
    return logger


log = setup_logging()


class MeshTAK:
    def __init__(self) -> None:
        self._config_lock = threading.RLock()
        self.config = self.load_config()

        os.makedirs(DATA_DIR, exist_ok=True)

        self.store = NodeStore(
            os.path.join(DATA_DIR, "nodes.json"),
            os.path.join(DATA_DIR, "messages.json"),
            os.path.join(DATA_DIR, "queue.json"),
        )

        self.interface = None
        self.tx_queue: "queue.Queue[Dict[str, Optional[str]]]" = queue.Queue()
        self.running = True
        self.connected = False

        self.radio_lock = threading.RLock()
        self.tx_priority_until = 0.0

        self._tak_thread: Optional[threading.Thread] = None
        self._tak_sync_thread: Optional[threading.Thread] = None
        self._tx_thread: Optional[threading.Thread] = None

        self.start_interfaces()
        self.start_workers()

    def load_config(self) -> Dict[str, Any]:
        if not os.path.exists(CONFIG_PATH):
            raise RuntimeError(f"Missing config file: {CONFIG_PATH}")

        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)

    def get_config(self) -> Dict[str, Any]:
        with self._config_lock:
            return json.loads(json.dumps(self.config))

    def reload_config(self) -> None:
        with self._config_lock:
            self.config = self.load_config()

        tak_cfg = self.config.get("tak", {})
        cot_cfg = self.config.get("cot", {})

        log.info(
            "Configuration reloaded: tak.enabled=%s tak.host=%s tak.port=%s tak.protocol=%s cot.team=%s cot.role=%s cot.type=%s",
            tak_cfg.get("enabled", False),
            tak_cfg.get("host", ""),
            tak_cfg.get("port", 8088),
            tak_cfg.get("protocol", "udp"),
            cot_cfg.get("team", "Orange"),
            cot_cfg.get("role", "RTO"),
            cot_cfg.get("type", "a-f-G-U-C"),
        )

    def _normalize_node_id(self, value: Optional[Any]) -> Optional[str]:
        if value is None:
            return None

        if isinstance(value, int):
            return f"!{value & 0xFFFFFFFF:08x}"

        node_id = str(value).strip()
        if not node_id:
            return None

        # BROADCAST HANDLING (FIX)
        if node_id.lower() in {
            "broadcast",
            "all",
            "*",
            "none",
            "null",
            "undefined",
            "any",
            "everyone",
        }:
            return None

        if node_id.startswith("!"):
            node_id = node_id[1:].strip()

        if not node_id:
            return None

        if node_id.lower().startswith("0x"):
            try:
                return f"!{int(node_id, 16) & 0xFFFFFFFF:08x}"
            except Exception:
                pass

        if re.fullmatch(r"[0-9a-fA-F]{8}", node_id):
            return f"!{node_id.lower()}"

        if re.fullmatch(r"\d+", node_id):
            try:
                return f"!{int(node_id, 10) & 0xFFFFFFFF:08x}"
            except Exception:
                pass

        cleaned = re.sub(r"[^0-9a-zA-Z]+", "", node_id).lower()
        if re.fullmatch(r"[0-9a-f]{8}", cleaned):
            return f"!{cleaned}"

        return f"!{node_id.lower()}"

    def _radio_maintenance_paused(self) -> bool:
        return time.time() < self.tx_priority_until

    def _pause_radio_maintenance(self, seconds: float = 3.0) -> None:
        self.tx_priority_until = max(self.tx_priority_until, time.time() + seconds)

    def _lookup_user_from_interface(self, node_id: Optional[Any]) -> Dict[str, Any]:
        normalized = self._normalize_node_id(node_id)
        if not normalized or not self.interface:
            return {}

        if self._radio_maintenance_paused():
            return {}

        acquired = self.radio_lock.acquire(timeout=0.25)
        if not acquired:
            return {}

        try:
            nodes = getattr(self.interface, "nodes", {}) or {}

            for key, value in nodes.items():
                key_norm = self._normalize_node_id(key)
                if key_norm == normalized and isinstance(value, dict):
                    user = value.get("user") or {}
                    if isinstance(user, dict):
                        return user

            for value in nodes.values():
                if not isinstance(value, dict):
                    continue

                user = value.get("user") or {}
                if not isinstance(user, dict):
                    user = {}

                candidates = [
                    value.get("num"),
                    value.get("id"),
                    value.get("nodeId"),
                    value.get("fromId"),
                    user.get("id"),
                ]

                for candidate in candidates:
                    if self._normalize_node_id(candidate) == normalized:
                        return user
        except Exception as exc:
            log.debug("User lookup failed for %s: %s", normalized, exc)
        finally:
            self.radio_lock.release()

        return {}

    def _extract_user_from_packet(self, packet: Dict[str, Any], from_id: str) -> Dict[str, Any]:
        decoded = packet.get("decoded", {}) or {}
        user = decoded.get("user") or packet.get("user") or {}
        if isinstance(user, dict) and user:
            return user
        return self._lookup_user_from_interface(from_id)

    def _refresh_known_nodes(self) -> None:
        if not self.interface:
            return

        if self._radio_maintenance_paused():
            return

        acquired = self.radio_lock.acquire(timeout=0.25)
        if not acquired:
            return

        try:
            nodes = getattr(self.interface, "nodes", {}) or {}
            now = int(time.time())

            for key, value in nodes.items():
                if not isinstance(value, dict):
                    continue

                node_id = (
                    value.get("id")
                    or value.get("nodeId")
                    or value.get("fromId")
                    or value.get("num")
                    or key
                )
                node_id_norm = self._normalize_node_id(node_id)
                if not node_id_norm:
                    continue

                user = value.get("user") or {}
                if not isinstance(user, dict):
                    user = {}

                position = value.get("position") or {}
                if not isinstance(position, dict):
                    position = {}

                device_metrics = value.get("deviceMetrics", {})
                if not isinstance(device_metrics, dict):
                    device_metrics = {}

                self.store.upsert_node(
                    node_id_norm,
                    long_name=user.get("longName"),
                    short_name=user.get("shortName"),
                    hw_model=user.get("hwModel"),
                    role=user.get("role"),
                    lat=position.get("latitude"),
                    lon=position.get("longitude"),
                    alt=position.get("altitude"),
                    batt=device_metrics.get("batteryLevel"),
                    last_heard=now,
                    raw=value,
                )
        except Exception as exc:
            log.debug("Known node refresh failed: %s", exc)
        finally:
            self.radio_lock.release()

    def get_callsign_for_node(self, node: Dict[str, Any]) -> str:
        short_name = str(node.get("short_name") or "").strip()
        display_name = str(node.get("display_name") or "").strip()
        long_name = str(node.get("long_name") or "").strip()
        node_id = str(node.get("node_id") or "").strip()

        return short_name or display_name or long_name or node_id or "UNKNOWN"

    def get_uid_for_node(self, node: Dict[str, Any]) -> str:
        node_id = self._normalize_node_id(node.get("node_id")) or "!unknown000"
        safe_node_id = node_id.lstrip("!")
        return f"meshtak-node-{safe_node_id}"

    def is_connected(self) -> bool:
        return bool(self.connected and self.interface is not None)

    def start_interfaces(self) -> None:
        connection = self.config.get("connection", {})
        conn_type = str(connection.get("type", "serial")).strip().lower()

        if conn_type == "serial":
            port = str(connection.get("port", "/dev/ttyACM0")).strip() or "/dev/ttyACM0"
            log.info("Connecting via serial: %s", port)
            self.interface = SerialInterface(port)
        elif conn_type == "tcp":
            host = str(connection.get("host", "")).strip()
            if not host:
                raise RuntimeError("TCP host missing in config")
            log.info("Connecting via TCP: %s", host)
            self.interface = TCPInterface(host)
        else:
            raise RuntimeError(f"Invalid connection type: {conn_type}")

        pub.subscribe(self.on_receive, "meshtastic.receive")
        pub.subscribe(self.on_connection, "meshtastic.connection.established")
        self.connected = True
        self._refresh_known_nodes()

    def on_connection(self, interface=None, topic=pub.AUTO_TOPIC) -> None:
        self.connected = True
        log.info("Meshtastic connection established")
        self._refresh_known_nodes()

    def start(self) -> None:
        log.info("MeshTAK backend starting")
        while self.running:
            time.sleep(1)

    def stop(self) -> None:
        self.running = False
        self.connected = False
        try:
            if self.interface:
                self.interface.close()
        except Exception as exc:
            log.warning("Error closing interface: %s", exc)

    def start_workers(self) -> None:
        if self._tak_thread is None or not self._tak_thread.is_alive():
            self._tak_thread = threading.Thread(target=self.tak_worker, daemon=True)
            self._tak_thread.start()

        if self._tak_sync_thread is None or not self._tak_sync_thread.is_alive():
            self._tak_sync_thread = threading.Thread(target=self.tak_sync_worker, daemon=True)
            self._tak_sync_thread.start()

        if self._tx_thread is None or not self._tx_thread.is_alive():
            self._tx_thread = threading.Thread(target=self.tx_worker, daemon=True)
            self._tx_thread.start()

    def _tak_enabled(self) -> bool:
        return bool(self.config.get("tak", {}).get("enabled", False))

    def _tak_protocol(self) -> str:
        return str(self.config.get("tak", {}).get("protocol", "udp")).strip().lower()

    def _cot_config(self) -> Dict[str, Any]:
        cot_cfg = self.config.get("cot", {})
        return {
            "type": str(cot_cfg.get("type", "a-f-G-U-C")).strip() or "a-f-G-U-C",
            "team": str(cot_cfg.get("team", "Orange")).strip() or "Orange",
            "role": str(cot_cfg.get("role", "RTO")).strip() or "RTO",
        }

    def on_receive(self, packet, interface) -> None:
        try:
            decoded = packet.get("decoded", {}) or {}
            portnum = decoded.get("portnum")
            from_id = self._normalize_node_id(packet.get("fromId") or packet.get("from"))
            to_id = self._normalize_node_id(packet.get("toId") or packet.get("to"))
            rx_time = int(time.time())

            if not from_id:
                return

            user = self._extract_user_from_packet(packet, from_id)

            if isinstance(user, dict) and user:
                self.store.upsert_node(
                    from_id,
                    long_name=user.get("longName"),
                    short_name=user.get("shortName"),
                    hw_model=user.get("hwModel"),
                    role=user.get("role"),
                    last_heard=rx_time,
                    raw=packet,
                )

            if portnum == "POSITION_APP":
                pos = decoded.get("position", {}) or {}
                lat = pos.get("latitude")
                lon = pos.get("longitude")
                alt = pos.get("altitude")

                if lat is not None and lon is not None:
                    node = self.store.upsert_node(
                        from_id,
                        long_name=user.get("longName") if isinstance(user, dict) else None,
                        short_name=user.get("shortName") if isinstance(user, dict) else None,
                        hw_model=user.get("hwModel") if isinstance(user, dict) else None,
                        role=user.get("role") if isinstance(user, dict) else None,
                        lat=lat,
                        lon=lon,
                        alt=alt,
                        last_heard=rx_time,
                        raw=packet,
                    )

                    log.info(
                        "POS %s (%s) -> %s,%s alt=%s",
                        from_id,
                        node.get("short_name") or node.get("long_name") or from_id,
                        lat,
                        lon,
                        alt,
                    )

                    if self._tak_enabled():
                        cot = self.build_cot(node)
                        self.store.enqueue_tak(cot, event_type="position", node_id=from_id)

            elif portnum == "TEXT_MESSAGE_APP":
                text = decoded.get("text", "")
                channel = str(packet.get("channel") or decoded.get("channel") or "")

                msg = self.store.add_message(
                    direction="rx",
                    text=text,
                    from_id=from_id,
                    to_id=to_id,
                    channel=channel,
                    rx_timestamp=rx_time,
                    raw=packet,
                )

                self.store.upsert_node(
                    from_id,
                    long_name=user.get("longName") if isinstance(user, dict) else None,
                    short_name=user.get("shortName") if isinstance(user, dict) else None,
                    hw_model=user.get("hwModel") if isinstance(user, dict) else None,
                    role=user.get("role") if isinstance(user, dict) else None,
                    last_heard=rx_time,
                    raw=packet,
                )

                log.info(
                    "RX MSG from=%s to=%s text=%s",
                    msg.get("from_name") or msg.get("from_id"),
                    msg.get("to_name") or msg.get("to_id") or "broadcast",
                    msg.get("text", ""),
                )

            else:
                self.store.upsert_node(
                    from_id,
                    long_name=user.get("longName") if isinstance(user, dict) else None,
                    short_name=user.get("shortName") if isinstance(user, dict) else None,
                    hw_model=user.get("hwModel") if isinstance(user, dict) else None,
                    role=user.get("role") if isinstance(user, dict) else None,
                    last_heard=rx_time,
                    raw=packet,
                )

        except Exception as exc:
            log.exception("Error processing packet: %s", exc)

    def build_cot(self, node: Dict[str, Any]) -> str:
        now = datetime.now(timezone.utc)
        stale = now + timedelta(minutes=2)

        time_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        stale_str = stale.strftime("%Y-%m-%dT%H:%M:%SZ")

        cot_cfg = self._cot_config()

        lat = node.get("lat", 0)
        lon = node.get("lon", 0)
        alt = node.get("alt", 0)
        callsign = escape(self.get_callsign_for_node(node), quote=True)
        uid = escape(self.get_uid_for_node(node), quote=True)

        cot = (
            f'<event version="2.0" uid="{uid}" type="{escape(cot_cfg["type"], quote=True)}" '
            f'time="{time_str}" start="{time_str}" stale="{stale_str}" how="m-g">'
            f'<point lat="{lat}" lon="{lon}" hae="{alt}" ce="9999999.0" le="9999999.0"/>'
            f'<detail>'
            f'<contact callsign="{callsign}"/>'
            f'<__group name="{escape(cot_cfg["team"], quote=True)}" role="{escape(cot_cfg["role"], quote=True)}"/>'
            f'</detail>'
            f'</event>'
        )
        return cot

    def tak_sync_worker(self) -> None:
        last_sent: Dict[str, int] = {}

        while self.running:
            try:
                if not self._tak_enabled():
                    time.sleep(5)
                    continue

                if self._radio_maintenance_paused():
                    time.sleep(0.5)
                    continue

                self._refresh_known_nodes()

                now = int(time.time())
                for node in self.store.get_nodes():
                    node_id = self._normalize_node_id(node.get("node_id"))
                    lat = node.get("lat")
                    lon = node.get("lon")
                    if not node_id or lat is None or lon is None:
                        continue

                    if now - int(last_sent.get(node_id, 0)) < 30:
                        continue

                    cot = self.build_cot(node)
                    self.store.enqueue_tak(cot, event_type="position", node_id=node_id)
                    last_sent[node_id] = now

                time.sleep(5)
            except Exception as exc:
                log.exception("TAK sync worker error: %s", exc)
                time.sleep(5)

    def tak_worker(self) -> None:
        while self.running:
            if not self._tak_enabled():
                time.sleep(5)
                continue

            item = self.store.pop_queue()
            if not item:
                time.sleep(1)
                continue

            tak_cfg = self.config.get("tak", {})
            protocol = self._tak_protocol()
            host = str(tak_cfg.get("host", "")).strip()
            port = int(tak_cfg.get("port", 8088))
            use_tls = bool(tak_cfg.get("tls", False))
            payload = item["cot"].encode("utf-8")

            if not host:
                log.error("TAK enabled but host is empty")
                self.store.requeue_failed(item, "TAK enabled but host is empty")
                time.sleep(2)
                continue

            try:
                if protocol == "udp":
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    sock.sendto(payload, (host, port))
                    sock.close()
                    log.info("TAK UDP SENT node=%s host=%s port=%s", item.get("node_id", ""), host, port)
                else:
                    sock = socket.create_connection((host, port), timeout=5)
                    if use_tls:
                        context = ssl.create_default_context()
                        sock = context.wrap_socket(sock, server_hostname=host)
                    sock.sendall(payload)
                    sock.close()
                    log.info(
                        "TAK TCP SENT node=%s host=%s port=%s tls=%s",
                        item.get("node_id", ""),
                        host,
                        port,
                        use_tls,
                    )
            except Exception as exc:
                log.error(
                    "TAK send failed protocol=%s host=%s port=%s error=%s",
                    protocol,
                    host,
                    port,
                    exc,
                )
                self.store.requeue_failed(item, str(exc))
                time.sleep(2)

    def queue_tx(self, text: str, to: Optional[str] = None) -> None:
        try:
            self.send_message(text=text, to=to)
            log.info("TX sent immediately to=%s text=%s", to or "broadcast", text)
            return
        except Exception as exc:
            log.warning("Immediate TX failed, queueing retry to=%s error=%s", to or "broadcast", exc)

        self.tx_queue.put({"text": text, "to": to})
        log.info("TX queued for retry to=%s text=%s", to or "broadcast", text)

    def tx_worker(self) -> None:
        while self.running:
            try:
                msg = self.tx_queue.get(timeout=1)
            except queue.Empty:
                continue

            time.sleep(0.5)

            try:
                self.send_message(msg.get("text", ""), msg.get("to"))
            except Exception as exc:
                failed_to = self._normalize_node_id(msg.get("to"))
                log.error("TX worker send failed to=%s error=%s", failed_to or "broadcast", exc)

                self.store.add_message(
                    direction="tx",
                    text=msg.get("text", ""),
                    to_id=failed_to,
                    from_id="self",
                    from_name="MeshTAK",
                    to_name=failed_to or "Broadcast",
                    rx_timestamp=int(time.time()),
                    raw={"status": "failed", "error": str(exc)},
                )

    def send_message(self, text: str, to: Optional[str] = None) -> None:
        if not self.interface:
            raise RuntimeError("Meshtastic interface is not connected")

        text = str(text or "").strip()
        if not text:
            raise ValueError("Message text is required")

        destination = self._normalize_node_id(to)

        with self.radio_lock:
            self._pause_radio_maintenance(4.0)

            if destination:
                log.info("TX PRIORITY direct to=%s text=%s", destination, text)
                sent_packet = self.interface.sendText(text, destinationId=destination)
            else:
                log.info("TX PRIORITY broadcast text=%s", text)
                sent_packet = self.interface.sendText(text)

            self.store.add_message(
                direction="tx",
                text=text,
                to_id=destination,
                from_id="self",
                from_name="MeshTAK",
                to_name=destination or "Broadcast",
                rx_timestamp=int(time.time()),
                raw={"status": "sent", "packet": sent_packet},
            )

            log.info("TX sent to radio to=%s text=%s", destination or "broadcast", text)


if __name__ == "__main__":
    app = MeshTAK()
    try:
        app.start()
    except KeyboardInterrupt:
        app.stop()
