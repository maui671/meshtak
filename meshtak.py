#!/usr/bin/env python3

import json
import logging
import os
import socket
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from pubsub import pub
from meshtastic.tcp_interface import TCPInterface
from meshtastic.serial_interface import SerialInterface
from meshtastic.ble_interface import BLEInterface

try:
    from radio_backends import SenseCAPM1Backend
except Exception:
    SenseCAPM1Backend = None

# ================= DEFAULT CONFIG =================

CONFIG_PATH = "/etc/meshtak/config.json"
LOG_FILE_PATH = "/var/log/meshtak.log"

MESHTASTIC_HOST = "10.42.0.238"
MESHTASTIC_PORT = 4403
MESHTASTIC_SERIAL_PORT = "/dev/ttyACM0"
MESHTASTIC_BLE_ADDRESS = ""

TAK_HOST = "127.0.0.1"
TAK_PORT = 8087
TAK_PROTOCOL = "udp"

COT_TYPE = "a-f-G-U-C-I"
STALE_MINUTES = 4

GROUP_NAME = "Cyan"
GROUP_ROLE = "Team Member"

TAK_DEVICE = "Meshtastic-Gateway"
TAK_PLATFORM = "TAK"
TAK_OS = "Linux"
TAK_VERSION = "4.10.3"

SEND_INTERVAL_SECONDS = 5

# ================================================

config: Dict[str, Any] = {}
tak_sock: Optional[socket.socket] = None
tak_addr = (TAK_HOST, TAK_PORT)

node_callsigns: Dict[str, str] = {}
last_sent: Dict[str, float] = {}


def setup_logging() -> None:
    os.makedirs(os.path.dirname(LOG_FILE_PATH), exist_ok=True)
    logging.basicConfig(
        filename=LOG_FILE_PATH,
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )
    logging.info("Logging started")


def load_config() -> Dict[str, Any]:
    if not os.path.exists(CONFIG_PATH):
        logging.warning("No config found at %s; using built-in defaults", CONFIG_PATH)
        return {}

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            return loaded
    except Exception as exc:
        logging.error("Failed to load config %s: %s", CONFIG_PATH, exc)

    return {}


def get_nested(data: Dict[str, Any], path: str, default: Any = None) -> Any:
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def stable_uuid_from_callsign(callsign: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, callsign))


def configure_tak_socket() -> None:
    global tak_sock, tak_addr, TAK_HOST, TAK_PORT, TAK_PROTOCOL

    tak_cfg = config.get("tak", {}) if isinstance(config.get("tak"), dict) else {}

    TAK_HOST = str(tak_cfg.get("host", TAK_HOST))
    TAK_PORT = int(tak_cfg.get("port", TAK_PORT))
    TAK_PROTOCOL = str(tak_cfg.get("protocol", TAK_PROTOCOL)).lower().strip()

    tak_addr = (TAK_HOST, TAK_PORT)

    if TAK_PROTOCOL == "tcp":
        tak_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tak_sock.settimeout(10)
        tak_sock.connect(tak_addr)
        logging.info("Connected TAK TCP %s:%s", TAK_HOST, TAK_PORT)
    else:
        tak_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        logging.info("Configured TAK UDP %s:%s", TAK_HOST, TAK_PORT)


def send_cot(callsign: str, lat: float, lon: float, hae: Any) -> None:
    global tak_sock

    if tak_sock is None:
        configure_tak_socket()

    now = datetime.now(timezone.utc)
    stale = now + timedelta(minutes=STALE_MINUTES)
    uid = stable_uuid_from_callsign(callsign)

    cot = f"""<event version="2.0"
uid="{uid}"
type="{COT_TYPE}"
how="h-e"
access="Undefined"
time="{iso(now)}"
start="{iso(now)}"
stale="{iso(stale)}">
<point lat="{lat:.8f}" lon="{lon:.8f}" hae="{hae}"
       ce="9999999" le="9999999"/>
<detail>
<link relation="p-p" type="{COT_TYPE}" uid="{uid}"/>
<contact endpoint="*:-1:stcp" callsign="{callsign}"/>
<__group name="{GROUP_NAME}" role="{GROUP_ROLE}"/>
<takv device="{TAK_DEVICE}"
      platform="{TAK_PLATFORM}"
      os="{TAK_OS}"
      version="{TAK_VERSION}"/>
</detail>
</event>"""

    payload = cot.encode("utf-8")

    try:
        if TAK_PROTOCOL == "tcp":
            assert tak_sock is not None
            tak_sock.sendall(payload)
        else:
            assert tak_sock is not None
            tak_sock.sendto(payload, tak_addr)

        logging.info("TAK ← %s %.6f,%.6f hae=%s", callsign, lat, lon, hae)

    except Exception as exc:
        logging.error("TAK send failed: %s", exc)
        if TAK_PROTOCOL == "tcp":
            try:
                if tak_sock:
                    tak_sock.close()
            except Exception:
                pass
            tak_sock = None


def normalize_node_id(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if text.startswith("!"):
        return text
    if text.isdigit():
        return f"!{int(text):08x}"
    return text


def callsign_from_user(node_id: str, user: Dict[str, Any]) -> str:
    short_name = str(user.get("shortName") or "").strip()
    long_name = str(user.get("longName") or "").strip()

    # Prefer short name for TAK display if present.
    return short_name or long_name or node_id.lstrip("!")


def extract_lat_lon(pos: Dict[str, Any]) -> Optional[tuple[float, float]]:
    if "latitudeI" in pos and "longitudeI" in pos:
        return float(pos["latitudeI"]) / 1e7, float(pos["longitudeI"]) / 1e7

    if "latitude" in pos and "longitude" in pos:
        return float(pos["latitude"]), float(pos["longitude"])

    return None


def on_receive(packet: Dict[str, Any], interface: Any = None) -> None:
    try:
        if not isinstance(packet, dict):
            return

        decoded = packet.get("decoded")
        if not isinstance(decoded, dict):
            return

        port = decoded.get("portnum")
        node_id = normalize_node_id(packet.get("fromId") or packet.get("from"))
        if not node_id:
            return

        if port == "USER_APP":
            user = decoded.get("user", {})
            if isinstance(user, dict):
                node_callsigns[node_id] = callsign_from_user(node_id, user)
                logging.info("NODE %s callsign=%s", node_id, node_callsigns[node_id])
            return

        if port != "POSITION_APP":
            return

        pos = decoded.get("position")
        if not isinstance(pos, dict):
            return

        coords = extract_lat_lon(pos)
        if coords is None:
            return

        lat, lon = coords
        if abs(lat) < 0.0001 and abs(lon) < 0.0001:
            return

        now_ts = time.time()
        last = last_sent.get(node_id, 0)
        if now_ts - last < SEND_INTERVAL_SECONDS:
            return

        hae = pos.get("altitudeHae", pos.get("altitude", 9999999))
        callsign = node_callsigns.get(node_id, node_id.lstrip("!"))

        send_cot(callsign, lat, lon, hae)
        last_sent[node_id] = now_ts

    except Exception as exc:
        logging.error("Error processing packet: %s", exc)


def connect_heltec_serial(port: str) -> Any:
    logging.info("Connecting to Heltec serial node: %s", port)
    return SerialInterface(devPath=port)


def connect_heltec_tcp(host: str, port: int) -> Any:
    logging.info("Connecting to Heltec TCP node: %s:%s", host, port)
    return TCPInterface(hostname=host, portNumber=port)


def connect_heltec_ble(address: str) -> Any:
    if not address:
        raise RuntimeError("Heltec BLE selected but no BLE address was configured")
    logging.info("Connecting to Heltec BLE node: %s", address)
    return BLEInterface(address=address)


def connect_sensecap_m1(radio_cfg: Dict[str, Any]) -> Any:
    if SenseCAPM1Backend is None:
        raise RuntimeError("SenseCAP M1 backend module is not available")

    m1_cfg = radio_cfg.get("sensecap_m1", {})
    if not isinstance(m1_cfg, dict):
        m1_cfg = {}

    logging.info("Starting SenseCAP M1 WM1303 backend")
    backend = SenseCAPM1Backend(config=m1_cfg, packet_callback=on_receive)
    backend.start()
    return backend


def connect_radio() -> Any:
    radio_cfg = config.get("radio", {}) if isinstance(config.get("radio"), dict) else {}

    # New config format from the backend selector install.sh.
    backend = str(radio_cfg.get("backend", "")).strip().lower()

    # Backwards compatibility with older install.sh format.
    old_active = config.get("meshtastic_active", {}) if isinstance(config.get("meshtastic_active"), dict) else {}
    old_conn = old_active.get("connection", {}) if isinstance(old_active.get("connection"), dict) else {}

    if not backend:
        old_type = str(old_conn.get("type", "tcp")).strip().lower()
        backend = {
            "serial": "heltec_serial",
            "tcp": "heltec_tcp",
            "ip": "heltec_tcp",
            "ble": "heltec_ble",
            "bluetooth": "heltec_ble",
        }.get(old_type, "heltec_tcp")

    heltec = radio_cfg.get("heltec", {}) if isinstance(radio_cfg.get("heltec"), dict) else {}

    if backend == "heltec_serial":
        port = str(
            heltec.get("serial_port")
            or old_conn.get("serial_port")
            or MESHTASTIC_SERIAL_PORT
        )
        return connect_heltec_serial(port)

    if backend == "heltec_tcp":
        host = str(
            heltec.get("host")
            or old_conn.get("host")
            or MESHTASTIC_HOST
        )
        port = int(
            heltec.get("port")
            or old_conn.get("port")
            or MESHTASTIC_PORT
        )
        return connect_heltec_tcp(host, port)

    if backend == "heltec_ble":
        address = str(
            heltec.get("ble_address")
            or old_conn.get("ble_address")
            or MESHTASTIC_BLE_ADDRESS
        ).strip()
        return connect_heltec_ble(address)

    if backend == "sensecap_m1":
        return connect_sensecap_m1(radio_cfg)

    raise RuntimeError(f"Unknown radio backend: {backend}")


def connect_with_retries() -> Any:
    max_retries = 5
    retries = 0

    while retries < max_retries:
        try:
            iface = connect_radio()
            logging.info("Successfully started radio backend")
            return iface
        except Exception as exc:
            retries += 1
            wait_time = 5 * retries
            logging.warning(
                "Error starting radio backend: %s. Retrying in %s seconds...",
                exc,
                wait_time,
            )
            time.sleep(wait_time)

    logging.error("Failed to start radio backend after several attempts")
    return None


def main() -> None:
    global config

    setup_logging()
    logging.info("Starting MeshTAK Gateway...")

    config = load_config()
    configure_tak_socket()

    iface = connect_with_retries()
    if not iface:
        logging.error("Exiting... Could not establish radio backend")
        return

    radio_cfg = config.get("radio", {}) if isinstance(config.get("radio"), dict) else {}
    backend = str(radio_cfg.get("backend", "")).strip().lower()

    if backend != "sensecap_m1":
        pub.subscribe(on_receive, "meshtastic.receive")

    logging.info("MeshTAK gateway running")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logging.info("Stopping MeshTAK gateway")
    finally:
        try:
            if hasattr(iface, "close"):
                iface.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
