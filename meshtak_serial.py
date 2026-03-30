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

# Node broadcasts every 10 sec on your design; this prevents one gateway
# from re-sending the same node too aggressively if packets are noisy.
SEND_INTERVAL_SECONDS = 5

LOG_FILE_PATH = "/var/log/meshtak.log"

# Polling supplements pubsub so the gateway can refresh names and recent node state.
POLL_SECONDS = 10

# If position packet lacks altitude, fall back to this.
DEFAULT_HAE = 9999999
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


def make_aka(long_name: str, short_name: str, node_id: str) -> str:
    """
    Best-effort AKA-style label from Meshtastic node naming.

    Examples:
      long=MAUI 5671 short=5671 -> M671
      long=MAUI5671 short=5671  -> M671
      short=5828                -> M828
      otherwise falls back to short/long/node_id
    """
    if short_name:
        short_name = short_name.strip()
        digits = "".join(ch for ch in short_name if ch.isdigit())
        if len(digits) >= 3:
            return f"M{digits[-3:]}"
        if short_name:
            return short_name

    if long_name:
        compact = long_name.replace(" ", "").strip()
        m = re.match(r"(?i)^MAUI(\d+)$", compact)
        if m:
            digits = m.group(1)
            if len(digits) >= 3:
                return f"M{digits[-3:]}"
            return f"M{digits}"
        if compact:
            return compact

    return node_id.lstrip("!")


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
    return DEFAULT_HAE


def refresh_node_cache():
    global node_cache
    try:
        if iface and getattr(iface, "nodes", None):
            node_cache = dict(iface.nodes)
            for node_id, node in node_cache.items():
                user = node.get("user", {})
                callsign = make_aka(
                    user.get("longName", ""),
                    user.get("shortName", ""),
                    node_id,
                )
                node_callsigns[node_id] = callsign
    except Exception as e:
        logging.warning(f"Failed to refresh node cache: {e}")


def get_callsign(node_id: str) -> str:
    if node_id in node_callsigns:
        return node_callsigns[node_id]

    node = node_cache.get(node_id, {})
    user = node.get("user", {})
    callsign = make_aka(
        user.get("longName", ""),
        user.get("shortName", ""),
        node_id,
    )
    node_callsigns[node_id] = callsign
    return callsign


def send_cot(node_id: str, callsign: str, lat: float, lon: float, hae, source: str, pos_time=None):
    now = datetime.now(timezone.utc)
    stale = now + timedelta(minutes=STALE_MINUTES)
    uid = stable_uuid_from_node_id(node_id)

    remarks = f"node_id={node_id} source={source}"
    if pos_time is not None:
        remarks += f" pos_time={pos_time}"

    cot = f"""<event version="2.0" uid="{uid}" type="{COT_TYPE}" how="m-g"
time="{iso(now)}" start="{iso(now)}" stale="{iso(stale)}">
  <point lat="{lat}" lon="{lon}" hae="{hae}" ce="9999999" le="9999999"/>
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


def maybe_send_position(node_id: str, pos: dict, source: str):
    if not pos:
        return

    lat, lon = get_lat_lon(pos)
    if lat is None or lon is None:
        return

    if abs(lat) < 0.0001 and abs(lon) < 0.0001:
        return

    now_ts = time.time()
    last = last_sent.get(node_id, 0)
    if now_ts - last < SEND_INTERVAL_SECONDS:
        return

    callsign = get_callsign(node_id)
    hae = get_hae(pos)
    pos_time = pos.get("time")

    send_cot(node_id, callsign, lat, lon, hae, source, pos_time=pos_time)
    last_sent[node_id] = now_ts


def on_receive(packet, interface):
    try:
        decoded = packet.get("decoded")
        if not decoded:
            return

        node_id = packet.get("fromId")
        if not node_id:
            return

        port = decoded.get("portnum")

        if port == "USER_APP":
            user = decoded.get("user", {})
            callsign = make_aka(
                user.get("longName", ""),
                user.get("shortName", ""),
                node_id,
            )
            node_callsigns[node_id] = callsign
            logging.info(f"USER_APP <- {callsign} [{node_id}]")
            return

        if port != "POSITION_APP":
            return

        refresh_node_cache()

        pos = decoded.get("position")
        if not pos:
            return

        maybe_send_position(node_id, pos, source="packet")

    except Exception as e:
        logging.error(f"Error processing packet: {e}")


def poll_known_nodes():
    refresh_node_cache()
    for node_id, node in node_cache.items():
        pos = node.get("position", {})
        if not pos:
            continue
        maybe_send_position(node_id, pos, source="poll")


def connect_to_meshtastic():
    max_retries = 5
    retries = 0

    while retries < max_retries:
        try:
            logging.info(f"Connecting to Meshtastic serial device {MESHTASTIC_DEVICE} ...")
            connected = SerialInterface(devPath=MESHTASTIC_DEVICE)
            logging.info("Successfully connected to Meshtastic over serial!")
            return connected
        except Exception as e:
            retries += 1
            wait_time = 5 * retries
            logging.warning(
                f"Error connecting to Meshtastic serial device: {e}. "
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


setup_logging()
logging.info("Starting MeshTAK Gateway...")

iface = connect_to_meshtastic()
if not iface:
    logging.error("Exiting... Could not establish connection.")
    raise SystemExit(1)

refresh_node_cache()
pub.subscribe(on_receive, "meshtastic.receive")
logging.info("Mesh -> TAK gateway running (all heard nodes mode)")

last_poll = 0.0

while True:
    try:
        now = time.time()
        if now - last_poll >= POLL_SECONDS:
            poll_known_nodes()
            last_poll = now
        time.sleep(0.25)
    except KeyboardInterrupt:
        break
    except Exception as e:
        logging.error(f"Main loop error: {e}")
        time.sleep(1)
