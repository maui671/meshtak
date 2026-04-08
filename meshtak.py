#!/usr/bin/env python3

import socket
import uuid
import time
from datetime import datetime, timedelta, timezone
from pubsub import pub
from meshtastic.tcp_interface import TCPInterface
import logging
import os

# ================= CONFIG =================

MESHTASTIC_HOST = "10.42.0.238"
MESHTASTIC_PORT = 4403

TAK_HOST = "127.0.0.1"
TAK_PORT = 8087

COT_TYPE = "a-f-G-U-C-I"
STALE_MINUTES = 4

GROUP_NAME = "Cyan"
GROUP_ROLE = "Team Member"

TAK_DEVICE = "Meshtastic-Gateway"
TAK_PLATFORM = "TAK"
TAK_OS = "Linux"
TAK_VERSION = "4.10.3"

SEND_INTERVAL_SECONDS = 5

LOG_FILE_PATH = "/var/log/meshtak.log"

# ==========================================

tak_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
tak_addr = (TAK_HOST, TAK_PORT)

# Cache
node_callsigns = {}
last_sent = {}

def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def stable_uuid_from_callsign(callsign):
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, callsign))


def send_cot(callsign, lat, lon, hae):
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

    tak_sock.sendto(cot.encode("utf-8"), tak_addr)
    logging.info(f"TAK ← {callsign} {lat:.6f},{lon:.6f} hae={hae}")


def on_receive(packet, interface):
    try:
        decoded = packet.get("decoded")
        if not decoded:
            return

        port = decoded.get("portnum")
        node_id = packet.get("fromId")

        # ================= USER APP =================
        if port == "USER_APP":
            user = decoded.get("user", {})
            callsign = (
                user.get("longName")
                or user.get("shortName")
                or node_id.lstrip("!")
            )
            node_callsigns[node_id] = callsign
            return

        # ================= POSITION APP =================
        if port != "POSITION_APP":
            return

        pos = decoded.get("position")
        if not pos:
            return

        # Rate limiting
        now_ts = time.time()
        last = last_sent.get(node_id, 0)
        if now_ts - last < SEND_INTERVAL_SECONDS:
            return

        # Lat / Lon
        if "latitudeI" in pos and "longitudeI" in pos:
            lat = pos["latitudeI"] / 1e7
            lon = pos["longitudeI"] / 1e7
        elif "latitude" in pos and "longitude" in pos:
            lat = float(pos["latitude"])
            lon = float(pos["longitude"])
        else:
            return

        if abs(lat) < 0.0001 and abs(lon) < 0.0001:
            return

        hae = pos.get("altitudeHae", 9999999)

        callsign = node_callsigns.get(node_id, node_id.lstrip("!"))

        send_cot(callsign, lat, lon, hae)
        last_sent[node_id] = now_ts

    except Exception as e:
        logging.error(f"Error processing packet: {e}")


def connect_to_meshtastic():
    max_retries = 5
    retries = 0
    while retries < max_retries:
        try:
            logging.info("Connecting to Meshtastic TCP node...")
            iface = TCPInterface(MESHTASTIC_HOST, MESHTASTIC_PORT)
            logging.info("Successfully connected to Meshtastic!")
            return iface
        except Exception as e:
            retries += 1
            wait_time = 5 * retries  # Exponential backoff
            logging.warning(f"Error connecting to Meshtastic: {e}. Retrying in {wait_time} seconds...")
            time.sleep(wait_time)
    logging.error("Failed to connect to Meshtastic after several attempts.")
    return None


def setup_logging():
    # Create the directory if it doesn't exist
    os.makedirs(os.path.dirname(LOG_FILE_PATH), exist_ok=True)

    # Set up logging to file
    logging.basicConfig(
        filename=LOG_FILE_PATH,
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s"
    )

    logging.info("Logging started")


# Initialize logging
setup_logging()

logging.info("Starting Mesh-Tak Gateway...")

# Main connection loop
iface = connect_to_meshtastic()
if iface:
    pub.subscribe(on_receive, "meshtastic.receive")
    logging.info("Mesh → TAK gateway running")
    while True:
        time.sleep(1)
else:
    logging.error("Exiting... Could not establish connection.")
