
from __future__ import annotations

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

from src.integrations.node_store import NodeStore

BASE_DIR = "/opt/meshtak"
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(BASE_DIR, "logs")
CONFIG_PATH = "/etc/meshtak/config.json"
LOG_PATH = os.path.join(LOG_DIR, "meshtak-bridge.log")


def ensure_log_dir() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(DATA_DIR, exist_ok=True)


def setup_logging() -> logging.Logger:
    ensure_log_dir()
    logger = logging.getLogger("meshtak.bridge")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.FileHandler(LOG_PATH)
    fh.setFormatter(formatter)
    sh = logging.StreamHandler()
    sh.setFormatter(formatter)
    logger.addHandler(fh)
    logger.addHandler(sh)
    logger.propagate = False
    return logger


log = setup_logging()


class MeshTakBridge:
    def __init__(self) -> None:
        self._config_lock = threading.RLock()
        self.config = self.load_config()
        self.store = NodeStore(
            os.path.join(DATA_DIR, "nodes.json"),
            os.path.join(DATA_DIR, "messages.json"),
            os.path.join(DATA_DIR, "queue.json"),
            hidden_nodes_path=os.path.join(DATA_DIR, "hidden_nodes.json"),
        )
        self.interface = None
        self.tx_queue: "queue.Queue[Dict[str, Optional[str]]]" = queue.Queue()
        self.running = True
        self.connected = False
        self.radio_lock = threading.RLock()
        self.tx_priority_until = 0.0
        self._tak_thread = None
        self._tak_sync_thread = None
        self._tx_thread = None

    def load_config(self) -> Dict[str, Any]:
        if not os.path.exists(CONFIG_PATH):
            return {
                "connection": {"type": "serial", "port": "/dev/ttyACM0", "host": "", "enabled": False},
                "tak": {"enabled": False, "host": "", "port": 8088, "protocol": "udp", "tls": False},
                "web": {"host": "0.0.0.0", "port": 9443, "tls_cert": "/opt/meshtak/certs/meshtak.crt", "tls_key": "/opt/meshtak/certs/meshtak.key"},
                "channels": [{"name": "Broadcast", "index": 0, "pinned": True}],
                "cot": {"type": "a-f-G-U-C", "team": "Orange", "role": "RTO"},
                "identity_policy": {"prefer_meshtastic_name_if_same_node_id": True, "allow_passive_only_tak_publish": True},
            }
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)

    def reload_config(self) -> None:
        with self._config_lock:
            self.config = self.load_config()

    def get_config(self) -> Dict[str, Any]:
        with self._config_lock:
            return json.loads(json.dumps(self.config))

    def _active_enabled(self) -> bool:
        active = self.config.get("meshtastic_active", {})
        if "enabled" in active:
            return bool(active.get("enabled"))
        conn = self.config.get("connection", {})
        return bool(conn.get("enabled", True))

    def _tak_enabled(self) -> bool:
        return bool(self.config.get("tak", {}).get("enabled", False))

    def _tak_protocol(self) -> str:
        return str(self.config.get("tak", {}).get("protocol", "udp")).strip().lower()

    @staticmethod
    def _normalize_node_id(value: Optional[Any]) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, int):
            return f"!{value & 0xFFFFFFFF:08x}"
        node_id = str(value).strip()
        if not node_id:
            return None
        if node_id.lower() in {"broadcast", "all", "*", "none", "null", "undefined", "any", "everyone"}:
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

    def _refresh_known_nodes(self) -> None:
        if not self.interface:
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
                node_id = value.get("id") or value.get("nodeId") or value.get("fromId") or value.get("num") or key
                node_id_norm = self._normalize_node_id(node_id)
                if not node_id_norm:
                    continue
                user = value.get("user") or {}
                if not isinstance(user, dict):
                    user = {}
                position = value.get("position") or {}
                if not isinstance(position, dict):
                    position = {}
                device_metrics = value.get("deviceMetrics") or {}
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
                    via="heltec",
                    raw=value,
                )
        except Exception as exc:
            log.debug("Known node refresh failed: %s", exc)
        finally:
            self.radio_lock.release()

    def start(self) -> None:
        self.start_workers()
        if self._active_enabled():
            try:
                self.start_interfaces()
            except Exception as exc:
                log.warning("Active Meshtastic interface unavailable at startup: %s", exc)

    def stop(self) -> None:
        self.running = False
        self.connected = False
        try:
            if self.interface:
                self.interface.close()
        except Exception as exc:
            log.warning("Error closing interface: %s", exc)

    def start_interfaces(self) -> None:
        conn = self.config.get("meshtastic_active", {}).get("connection", self.config.get("connection", {}))
        conn_type = str(conn.get("type", "serial")).strip().lower()
        if conn_type == "serial":
            port = str(conn.get("serial_port") or conn.get("port") or "/dev/ttyACM0").strip() or "/dev/ttyACM0"
            log.info("Connecting active Meshtastic radio via serial: %s", port)
            self.interface = SerialInterface(port)
        elif conn_type == "tcp":
            host = str(conn.get("host", "")).strip()
            if not host:
                raise RuntimeError("TCP host missing in config")
            log.info("Connecting active Meshtastic radio via TCP: %s", host)
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

    def get_callsign_for_node(self, node: Dict[str, Any]) -> str:
        return str(node.get("short_name") or node.get("display_name") or node.get("long_name") or node.get("node_id") or "UNKNOWN").strip()

    def get_uid_for_node(self, node: Dict[str, Any]) -> str:
        node_id = self._normalize_node_id(node.get("node_id")) or "!unknown000"
        return f"meshtak-node-{node_id.lstrip('!')}"

    def is_connected(self) -> bool:
        return bool(self.connected and self.interface is not None)

    def _extract_user_from_interface(self, node_id: Optional[Any]) -> Dict[str, Any]:
        normalized = self._normalize_node_id(node_id)
        if not normalized or not self.interface:
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
                candidates = [value.get("num"), value.get("id"), value.get("nodeId"), value.get("fromId"), user.get("id")]
                for candidate in candidates:
                    if self._normalize_node_id(candidate) == normalized:
                        return user
        finally:
            self.radio_lock.release()
        return {}

    def on_receive(self, packet, interface) -> None:
        try:
            decoded = packet.get("decoded", {}) or {}
            portnum = decoded.get("portnum")
            from_id = self._normalize_node_id(packet.get("fromId") or packet.get("from"))
            to_id = self._normalize_node_id(packet.get("toId") or packet.get("to"))
            rx_time = int(time.time())
            if not from_id:
                return
            user = decoded.get("user") or packet.get("user") or self._extract_user_from_interface(from_id) or {}
            if isinstance(user, dict) and user:
                self.store.upsert_node(from_id, long_name=user.get("longName"), short_name=user.get("shortName"), hw_model=user.get("hwModel"), role=user.get("role"), last_heard=rx_time, via="heltec", raw=packet)
            if portnum == "POSITION_APP":
                pos = decoded.get("position", {}) or {}
                lat = pos.get("latitude")
                lon = pos.get("longitude")
                alt = pos.get("altitude")
                if lat is not None and lon is not None:
                    node = self.store.upsert_node(from_id, long_name=user.get("longName") if isinstance(user, dict) else None, short_name=user.get("shortName") if isinstance(user, dict) else None, hw_model=user.get("hwModel") if isinstance(user, dict) else None, role=user.get("role") if isinstance(user, dict) else None, lat=lat, lon=lon, alt=alt, last_heard=rx_time, via="heltec", raw=packet)
                    if self._tak_enabled():
                        self.store.enqueue_tak(self.build_cot(node), event_type="position", node_id=from_id)
            elif portnum == "TEXT_MESSAGE_APP":
                text = decoded.get("text", "")
                channel = str(packet.get("channel") or decoded.get("channel") or "")
                self.store.add_message(
                    direction="rx",
                    text=text,
                    from_id=from_id,
                    to_id=to_id,
                    channel=channel,
                    rx_timestamp=rx_time,
                    hop_start=packet.get("hopStart") or packet.get("hop_start"),
                    hop_limit=packet.get("hopLimit") or packet.get("hop_limit"),
                    raw=packet,
                )
                self.store.upsert_node(from_id, long_name=user.get("longName") if isinstance(user, dict) else None, short_name=user.get("shortName") if isinstance(user, dict) else None, hw_model=user.get("hwModel") if isinstance(user, dict) else None, role=user.get("role") if isinstance(user, dict) else None, last_heard=rx_time, via="heltec", raw=packet)
            else:
                self.store.upsert_node(from_id, long_name=user.get("longName") if isinstance(user, dict) else None, short_name=user.get("shortName") if isinstance(user, dict) else None, hw_model=user.get("hwModel") if isinstance(user, dict) else None, role=user.get("role") if isinstance(user, dict) else None, last_heard=rx_time, via="heltec", raw=packet)
        except Exception as exc:
            log.exception("Error processing active radio packet: %s", exc)

    def _extract_position(self, payload: dict) -> tuple[Optional[float], Optional[float], Optional[float]]:
        if not isinstance(payload, dict):
            return None, None, None
        candidates = [payload, payload.get("position") if isinstance(payload.get("position"), dict) else None, payload.get("decoded") if isinstance(payload.get("decoded"), dict) else None]
        for c in candidates:
            if not isinstance(c, dict):
                continue
            lat = c.get("latitude") or c.get("lat")
            lon = c.get("longitude") or c.get("lon")
            alt = c.get("altitude") or c.get("alt")
            if lat is not None and lon is not None:
                try:
                    return float(lat), float(lon), float(alt) if alt is not None else None
                except Exception:
                    pass
        return None, None, None

    def ingest_passive_packet(self, packet: Any) -> None:
        try:
            if hasattr(packet, 'to_dict'):
                pkt = packet.to_dict()
            else:
                pkt = dict(packet or {})
            source_id = self._normalize_node_id(pkt.get('source_id') or pkt.get('fromId') or pkt.get('from'))
            if not source_id:
                return
            payload = pkt.get('decoded_payload') or pkt.get('decoded') or {}
            signal = pkt.get('signal') or {}
            lat, lon, alt = self._extract_position(payload)
            packet_type = str(pkt.get('packet_type') or payload.get('portnum') or '').lower()
            raw_name = source_id
            if isinstance(payload, dict):
                raw_name = str(payload.get('short_name') or payload.get('shortName') or payload.get('long_name') or payload.get('longName') or source_id)
            node = self.store.upsert_node(
                source_id,
                long_name=payload.get('long_name') if isinstance(payload, dict) else None,
                short_name=payload.get('short_name') if isinstance(payload, dict) else None,
                lat=lat,
                lon=lon,
                alt=alt,
                snr=signal.get('snr'),
                rssi=signal.get('rssi'),
                last_heard=int(time.time()),
                via='wm1303',
                raw=pkt,
            )
            if not node.get('display_name'):
                self.store.upsert_node(source_id, short_name=raw_name, via='wm1303', last_heard=int(time.time()), raw=pkt)
                node = next((n for n in self.store.get_nodes() if n.get('node_id') == source_id), node)
            if self._tak_enabled() and lat is not None and lon is not None:
                self.store.enqueue_tak(self.build_cot(node), event_type='position', node_id=source_id)
        except Exception as exc:
            log.exception('Passive packet ingest failed: %s', exc)

    def build_cot(self, node: Dict[str, Any]) -> str:
        now = datetime.now(timezone.utc)
        stale = now + timedelta(minutes=2)
        time_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        stale_str = stale.strftime("%Y-%m-%dT%H:%M:%SZ")
        cot_cfg = self.config.get('cot', {})
        lat = node.get('lat', 0)
        lon = node.get('lon', 0)
        alt = node.get('alt', 0)
        callsign = escape(self.get_callsign_for_node(node), quote=True)
        uid = escape(self.get_uid_for_node(node), quote=True)
        return (
            f'<event version="2.0" uid="{uid}" type="{escape(str(cot_cfg.get("type", "a-f-G-U-C")), quote=True)}" '
            f'time="{time_str}" start="{time_str}" stale="{stale_str}" how="m-g">'
            f'<point lat="{lat}" lon="{lon}" hae="{alt}" ce="9999999.0" le="9999999.0"/>'
            f'<detail><contact callsign="{callsign}"/>'
            f'<__group name="{escape(str(cot_cfg.get("team", "Orange")), quote=True)}" role="{escape(str(cot_cfg.get("role", "RTO")), quote=True)}"/>'
            f'</detail></event>'
        )

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

    def tak_sync_worker(self) -> None:
        last_sent: Dict[str, int] = {}
        while self.running:
            try:
                if not self._tak_enabled():
                    time.sleep(5)
                    continue
                if self.connected:
                    self._refresh_known_nodes()
                now = int(time.time())
                for node in self.store.get_nodes():
                    node_id = self._normalize_node_id(node.get('node_id'))
                    lat = node.get('lat'); lon = node.get('lon')
                    if not node_id or lat is None or lon is None:
                        continue
                    if now - int(last_sent.get(node_id, 0)) < 30:
                        continue
                    self.store.enqueue_tak(self.build_cot(node), event_type='position', node_id=node_id)
                    last_sent[node_id] = now
                time.sleep(5)
            except Exception as exc:
                log.exception('TAK sync worker error: %s', exc)
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
            tak_cfg = self.config.get('tak', {})
            protocol = self._tak_protocol()
            host = str(tak_cfg.get('host', '')).strip()
            port = int(tak_cfg.get('port', 8088))
            use_tls = bool(tak_cfg.get('tls', False))
            payload = item['cot'].encode('utf-8')
            if not host:
                self.store.requeue_failed(item, 'TAK enabled but host is empty')
                time.sleep(2)
                continue
            try:
                if protocol == 'udp':
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    sock.sendto(payload, (host, port))
                    sock.close()
                else:
                    sock = socket.create_connection((host, port), timeout=5)
                    if use_tls:
                        cafile = str(tak_cfg.get('ca_cert', '')).strip() or None
                        certfile = str(tak_cfg.get('client_cert', '')).strip() or None
                        keyfile = str(tak_cfg.get('client_key', '')).strip() or None
                        context = ssl.create_default_context(cafile=cafile) if cafile else ssl.create_default_context()
                        if certfile and keyfile:
                            context.load_cert_chain(certfile=certfile, keyfile=keyfile)
                        sock = context.wrap_socket(sock, server_hostname=host)
                    sock.sendall(payload)
                    sock.close()
            except Exception as exc:
                log.error('TAK send failed protocol=%s host=%s port=%s error=%s', protocol, host, port, exc)
                self.store.requeue_failed(item, str(exc))
                time.sleep(2)

    def queue_tx(self, text: str, to: Optional[str] = None, channel_index: Optional[int] = None, channel_name: Optional[str] = None) -> None:
        try:
            self.send_message(text=text, to=to, channel_index=channel_index, channel_name=channel_name)
            return
        except Exception as exc:
            log.warning('Immediate TX failed, queueing retry: %s', exc)
        self.tx_queue.put({'text': text, 'to': to, 'channel_index': channel_index, 'channel_name': channel_name})

    def tx_worker(self) -> None:
        while self.running:
            try:
                msg = self.tx_queue.get(timeout=1)
            except queue.Empty:
                continue
            time.sleep(0.5)
            try:
                self.send_message(msg.get('text',''), msg.get('to'), msg.get('channel_index'), msg.get('channel_name'))
            except Exception as exc:
                failed_to = self._normalize_node_id(msg.get('to'))
                self.store.add_message(direction='tx', text=msg.get('text',''), to_id=failed_to, from_id='self', from_name='MeshTAK', to_name=failed_to or 'Broadcast', rx_timestamp=int(time.time()), raw={'status':'failed','error':str(exc)})

    def send_message(self, text: str, to: Optional[str] = None, channel_index: Optional[int] = None, channel_name: Optional[str] = None) -> None:
        if not self.interface:
            raise RuntimeError('Meshtastic interface is not connected')
        text = str(text or '').strip()
        if not text:
            raise ValueError('Message text is required')
        destination = self._normalize_node_id(to)
        if channel_index is not None:
            try:
                channel_index = int(channel_index)
            except Exception:
                channel_index = None
        with self.radio_lock:
            kwargs = {}
            if channel_index is not None:
                kwargs['channelIndex'] = channel_index
            if destination:
                sent_packet = self.interface.sendText(text, destinationId=destination, **kwargs)
            else:
                sent_packet = self.interface.sendText(text, **kwargs)
            self.store.add_message(direction='tx', text=text, to_id=destination, from_id='self', from_name='MeshTAK', to_name=destination or (channel_name or 'Broadcast'), channel=channel_name or (f'ch{channel_index}' if channel_index is not None else ''), rx_timestamp=int(time.time()), hop_start=(sent_packet or {}).get('hopStart') if isinstance(sent_packet, dict) else None, hop_limit=(sent_packet or {}).get('hopLimit') if isinstance(sent_packet, dict) else None, raw={'status':'sent','packet':sent_packet,'channel_index':channel_index,'channel_name':channel_name})
