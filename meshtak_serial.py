#!/usr/bin/env python3
import logging
import os
import re
import socket
import time
import uuid
from datetime import datetime, timedelta, timezone

from pubsub import pub
from meshtastic.serial_interface import SerialInterface

from node_store import update_node

# ================= CONFIG =================
MESHTASTIC_DEVICE = "/dev/ttyS0"

TAK_HOST = "127.0.0.1"
TAK_PORT = 8088

COT_TYPE = "a-f-G-U-C-I"
STALE_MINUTES = 4

GROUP_NAME = "Cyan"
GROUP_ROLE = "Team Member"

TAK_DEVICE = "Meshtastic-Gateway"
TAK_PLATFORM = "TAK"
TAK_OS = "Linux"
TAK_VERSION = "4.10.3"

SEND_INTERVAL_SECONDS = 2

LOG_FILE_PATH = "/var/log/meshtak.log"
# ==========================================

tak_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
tak_addr = (TAK_HOST, TAK_PORT)

iface = None
node_callsigns = {}
node_cache = {}
last_sent = {}


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def stable_uuid_from_node_id(node_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"meshtastic:{node_id}"))


def sanitize_callsign(value: str) -> str:
    value = (value or "").strip()
    return value[:64] if value else ""


def make_callsign(long_name: str, short_name: str, node_id: str) -> str:
    short_name = sanitize_callsign(short_name)
    long_name = sanitize_callsign(long_name)

    if short_name:
        return short_name
    if long_name:
        compact = re.sub(r"\s+", " ", long_name).strip()
        if compact:
            return compact
    return node_id.lstrip("!")


def refresh_node_cache():
    global node_cache

    try:
        if iface and getattr(iface, "nodes", None):
            node_cache = dict(iface.nodes)

            for node_id, node in node_cache.items():
                user = node.get("user", {})
                callsign = make_callsign(
                    user.get("longName", ""),
                    user.get("shortName", ""),
                    node_id,
                )
                node_callsigns[node_id] = callsign
                update_node(node_id, {
                    "node_id": node_id,
                    "callsign": callsign
                })
    except Exception as exc:
        logging.warning(f"Failed to refresh node cache: {exc}")


def get_callsign(node_id: str) -> str:
    callsign = node_callsigns.get(node_id)
    if callsign:
        return callsign

    node = node_cache.get(node_id, {})
    user = node.get("user", {})
    callsign = make_callsign(
        user.get("longName", ""),
        user.get("shortName", ""),
        node_id,
    )
    node_callsigns[node_id] = callsign
    return callsign


def get_lat_lon(pos: dict):
    if "latitudeI" in pos and "longitudeI" in pos:
        return pos["latitudeI"] / 1e7, pos["longitudeI"] / 1e7

    if "latitude" in pos and "longitude" in pos:
        return float(pos["latitude"]), float(pos["longitude"])

    return None, None


def get_hae(pos: dict):
    for key in ("altitudeHae", "altitude", "altitudeGeoidalSeparation"):
        value = pos.get(key)
        if value is not None:
            return value
    return 9999999


def valid_position(lat, lon):
    if lat is None or lon is None:
        return False
    if abs(lat) < 0.0001 and abs(lon) < 0.0001:
        return False
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return False
    return True


def should_send(node_id: str, pos: dict):
    now_ts = time.time()
    last = last_sent.get(node_id, 0)

    if now_ts - last < SEND_INTERVAL_SECONDS:
        return False

    return True


def send_cot(node_id: str, callsign: str, lat: float, lon: float, hae, source: str, pos_time=None):
    now = datetime.now(timezone.utc)
    stale = now + timedelta(minutes=STALE_MINUTES)
    uid = stable_uuid_from_node_id(node_id)

    remarks = f"node_id={node_id} source={source}"
    if pos_time is not None:
        remarks += f" pos_time={pos_time}"

    cot = f"""<event version="2.0" uid="{uid}" type="{COT_TYPE}" how="m-g"
time="{iso(now)}" start="{iso(now)}" stale="{iso(stale)}">
  <point lat="{lat:.7f}" lon="{lon:.7f}" hae="{hae}" ce="9999999" le="9999999"/>
  <detail>
    <contact callsign="{callsign}"/>
    <__group name="{GROUP_NAME}" role="{GROUP_ROLE}"/>
    <takv device="{TAK_DEVICE}" platform="{TAK_PLATFORM}" os="{TAK_OS}" version="{TAK_VERSION}"/>
    <remarks>{remarks}</remarks>
  </detail>
</event>"""

    tak_sock.sendto(cot.encode("utf-8"), tak_addr)
    logging.info(
        f"TAK <- {callsign} [{node_id}] {lat:.6f},{lon:.6f} hae={hae} source={source} uid={uid}"
    )


def handle_position(node_id: str, pos: dict, source: str):
    lat, lon = get_lat_lon(pos)
    if not valid_position(lat, lon):
        return

    callsign = get_callsign(node_id)
    hae = get_hae(pos)

    update_node(node_id, {
        "node_id": node_id,
        "callsign": callsign,
        "lat": lat,
        "lon": lon,
        "hae": hae,
        "source": source
    })

    if not should_send(node_id, pos):
        return

    pos_time = pos.get("time")

    send_cot(node_id, callsign, lat, lon, hae, source, pos_time=pos_time)
    last_sent[node_id] = time.time()


def on_receive(packet, interface):
    try:
        decoded = packet.get("decoded")
        if not decoded:
            return

        node_id = packet.get("fromId")
        if not node_id:
            return

        port = decoded.get("portnum")

        if port in ("USER_APP", "NODEINFO_APP"):
            user = decoded.get("user", {})
            callsign = make_callsign(
                user.get("longName", ""),
                user.get("shortName", ""),
                node_id,
            )
            node_callsigns[node_id] = callsign
            update_node(node_id, {
                "node_id": node_id,
                "callsign": callsign
            })
            logging.info(f"NAME <- {callsign} [{node_id}] port={port}")
            return

        if port != "POSITION_APP":
            return

        pos = decoded.get("position")
        if not pos:
            return

        refresh_node_cache()
        handle_position(node_id, pos, source="packet")

    except Exception as exc:
        logging.error(f"Error processing packet: {exc}")


def connect_to_meshtastic():
    max_retries = 5
    retries = 0

    while retries < max_retries:
        try:
            logging.info(f"Connecting to Meshtastic serial device {MESHTASTIC_DEVICE} ...")
            connected = SerialInterface(devPath=MESHTASTIC_DEVICE)
            logging.info("Successfully connected to Meshtastic over serial!")
            return connected
        except Exception as exc:
            retries += 1
            wait_time = 5 * retries
            logging.warning(
                f"Error connecting to Meshtastic serial device: {exc}. "
                f"Retrying in {wait_time} seconds..."
            )
            time.sleep(wait_time)

    logging.error("Failed to connect to Meshtastic after several attempts.")
    return None


def setup_logging():
    os.makedirs(os.path.dirname(LOG_FILE_PATH), exist_ok=True)
    logging.basicConfig(
        filename=LOG_FILE_PATH,
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s"
    )
    logging.info("Logging started")


def main():
    global iface

    setup_logging()
    logging.info("Starting MeshTAK Gateway (serial mode)...")

    iface = connect_to_meshtastic()
    if not iface:
        logging.error("Exiting... Could not establish connection.")
        raise SystemExit(1)

    refresh_node_cache()
    pub.subscribe(on_receive, "meshtastic.receive")
    logging.info("Mesh -> TAK gateway running (event-driven)")

    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
