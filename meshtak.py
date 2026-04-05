#!/usr/bin/env python3
import json
import logging
import socket
import time

from pubsub import pub

from config_store import load_config
from node_store import (
    append_message_event,
    dequeue_messages,
    update_node,
)

from meshtastic.serial_interface import SerialInterface
from meshtastic.tcp_interface import TCPInterface


logging.basicConfig(
    filename="/var/log/meshtak.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

iface = None
tak_sock = None


def load_runtime():
    cfg = load_config()

    m = cfg.get("meshtastic", {})
    t = cfg.get("tak", {})

    return {
        "mode": m.get("mode"),
        "serial_device": m.get("serial_device"),
        "host": m.get("host"),
        "port": m.get("port"),
        "tak_enabled": t.get("enabled", False),
        "tak_host": t.get("host"),
        "tak_port": t.get("port", 8088),
    }


def connect_meshtastic(cfg):
    global iface

    if cfg["mode"] == "serial":
        iface = SerialInterface(cfg["serial_device"])
        logging.info(f"Connected via serial {cfg['serial_device']}")
    else:
        iface = TCPInterface(cfg["host"])
        logging.info(f"Connected via TCP {cfg['host']}")


def connect_tak(cfg):
    global tak_sock

    if not cfg["tak_enabled"]:
        return

    tak_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tak_sock.connect((cfg["tak_host"], cfg["tak_port"]))
    logging.info(f"Connected to TAK {cfg['tak_host']}:{cfg['tak_port']}")


def send_to_tak(xml):
    global tak_sock
    if not tak_sock:
        return

    try:
        tak_sock.sendall(xml.encode("utf-8"))
    except Exception as e:
        logging.error(f"TAK send failed: {e}")


def build_cot(node_id, callsign, lat, lon, hae):
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    stale = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 300))

    return f"""<event version="2.0" uid="{node_id}" type="a-f-G-U-C"
time="{now}" start="{now}" stale="{stale}">
<point lat="{lat}" lon="{lon}" hae="{hae}" ce="9999999" le="9999999"/>
<detail>
<contact callsign="{callsign}"/>
</detail>
</event>"""


def handle_packet(packet, interface):
    try:
        decoded = packet.get("decoded", {})
        portnum = decoded.get("portnum")

        node_id = packet.get("fromId", "")
        callsign = packet.get("from", "")

        # ======================
        # POSITION HANDLING
        # ======================
        if portnum == "POSITION_APP":
            pos = decoded.get("position", {})

            lat = pos.get("latitude")
            lon = pos.get("longitude")
            hae = pos.get("altitude", 0)

            if lat and lon:
                update_node(
                    node_id,
                    {
                        "callsign": callsign,
                        "lat": lat,
                        "lon": lon,
                        "hae": hae,
                    },
                )

                cfg = load_runtime()

                if cfg["tak_enabled"]:
                    cot = build_cot(node_id, callsign, lat, lon, hae)
                    send_to_tak(cot)
                    logging.info(
                        f"TAK <- {callsign} [{node_id}] {lat},{lon} hae={hae}"
                    )

        # ======================
        # MESSAGE RX HANDLING
        # ======================
        elif portnum == "TEXT_MESSAGE_APP":
            text = decoded.get("text", "").strip()
            if not text:
                return

            to_id = packet.get("toId", "")
            is_broadcast = str(to_id).lower() in ("", "broadcast")

            append_message_event(
                direction="rx",
                text=text,
                local_node_id=interface.myInfo.my_node_num if interface.myInfo else "",
                local_callsign="ME",
                peer_node_id=node_id,
                peer_callsign=callsign,
                destination=to_id,
                is_broadcast=is_broadcast,
                channel=packet.get("channel", 0),
                raw_portnum=portnum,
            )

            logging.info(
                f"RX <- {callsign} [{node_id}] -> {to_id} : {text}"
            )

    except Exception as e:
        logging.error(f"Packet handling error: {e}")


def process_outbound():
    while True:
        try:
            msgs = dequeue_messages()

            for m in msgs:
                try:
                    iface.sendText(
                        text=m["text"],
                        destinationId=m["destination"],
                        wantAck=m["want_ack"],
                        channelIndex=m["channel"],
                    )

                    append_message_event(
                        direction="tx",
                        text=m["text"],
                        local_node_id=iface.myInfo.my_node_num
                        if iface.myInfo
                        else "",
                        local_callsign="ME",
                        peer_node_id=m["destination"],
                        peer_callsign=m["destination"],
                        destination=m["destination"],
                        is_broadcast=(m["destination"] == "broadcast"),
                        channel=m["channel"],
                    )

                    logging.info(
                        f"TX -> {m['destination']} : {m['text']}"
                    )

                except Exception as e:
                    logging.error(f"TX failed: {e}")

        except Exception as e:
            logging.error(f"Queue processing error: {e}")

        time.sleep(2)


def main():
    cfg = load_runtime()

    connect_meshtastic(cfg)
    connect_tak(cfg)

    pub.subscribe(handle_packet, "meshtastic.receive")

    import threading

    t = threading.Thread(target=process_outbound, daemon=True)
    t.start()

    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
