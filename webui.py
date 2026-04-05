#!/usr/bin/env python3
import logging
import os
import ssl
import subprocess
import threading
import time
from collections import deque

from flask import Flask, jsonify, render_template, request

from config_store import get_tak_config, load_config, update_config
from node_store import enqueue_message, get_nodes, list_queued_messages, prune_old_nodes

APP_DIR = "/opt/meshtak"
LOG_FILE = os.environ.get("MESHTAK_LOG_FILE", "/var/log/meshtak.log")
SERVICE_NAME = os.environ.get("MESHTAK_SERVICE_NAME", "meshtak")
CERT_FILE = os.environ.get("MESHTAK_CERT_FILE", f"{APP_DIR}/certs/meshtak.crt")
KEY_FILE = os.environ.get("MESHTAK_KEY_FILE", f"{APP_DIR}/certs/meshtak.key")
NODE_PRUNE_SECONDS = int(os.environ.get("MESHTAK_NODE_PRUNE_SECONDS", "86400"))

app = Flask(
    __name__,
    template_folder=os.path.join(APP_DIR, "templates"),
    static_folder=os.path.join(APP_DIR, "static"),
)

logger = logging.getLogger("meshtak.webui")
logger.setLevel(logging.INFO)

_recent_log = deque(maxlen=200)
_recent_tak = deque(maxlen=100)
_recent_errors = deque(maxlen=100)


def log_event(message, category="INFO"):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {category} {message}"
    _recent_log.append(line)

    category_upper = str(category).upper()
    if category_upper in ("TAK", "COT", "PUSH"):
        _recent_tak.append(line)
    if category_upper in ("ERROR", "WARN", "WARNING", "EXCEPTION"):
        _recent_errors.append(line)

    try:
        logger.info(line)
    except Exception:
        pass


def tail_lines(path, limit=200):
    if not os.path.exists(path):
        return []

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return [line.rstrip("\n") for line in f.readlines()[-limit:]]
    except Exception as exc:
        return [f"ERROR reading log file {path}: {exc}"]


def refresh_recent_buffers_from_log():
    lines = tail_lines(LOG_FILE, 400)

    _recent_log.clear()
    _recent_tak.clear()
    _recent_errors.clear()

    for line in lines:
        _recent_log.append(line)

        upper = line.upper()
        if " TAK " in upper or " COT " in upper or "PUSHED TO TAK" in upper or "TAK <-" in upper or "TAK:" in upper:
            _recent_tak.append(line)

        if (
            " ERROR " in upper
            or " EXCEPTION " in upper
            or " TRACEBACK" in upper
            or " WARNING " in upper
            or " WARN " in upper
            or upper.startswith("ERROR ")
        ):
            _recent_errors.append(line)


def get_service_status():
    try:
        proc = subprocess.run(
            ["systemctl", "is-active", SERVICE_NAME],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        status = (proc.stdout or proc.stderr or "").strip().lower()
        if not status:
            return "unknown"
        return status
    except Exception:
        return "unknown"


def get_web_config():
    cfg = load_config()
    return cfg.get("web", {})


def format_nodes_for_ui():
    nodes = get_nodes()
    if not isinstance(nodes, dict):
        return []

    formatted = []
    for node_id, node in nodes.items():
        if not isinstance(node, dict):
            continue

        item = dict(node)
        item["node_id"] = str(item.get("node_id") or node_id)

        try:
            if item.get("lat") is not None:
                item["lat"] = float(item["lat"])
        except Exception:
            pass

        try:
            if item.get("lon") is not None:
                item["lon"] = float(item["lon"])
        except Exception:
            pass

        try:
            if item.get("hae") is not None and item.get("hae") != "":
                item["hae"] = float(item["hae"])
        except Exception:
            pass

        try:
            if item.get("last_seen") is not None:
                item["last_seen"] = float(item["last_seen"])
        except Exception:
            pass

        formatted.append(item)

    formatted.sort(
        key=lambda n: (
            0 if n.get("last_seen") else 1,
            -(float(n.get("last_seen", 0)) if n.get("last_seen") else 0),
            str(n.get("callsign") or ""),
            str(n.get("node_id") or ""),
        )
    )
    return formatted


def background_maintenance():
    while True:
        try:
            prune_old_nodes(NODE_PRUNE_SECONDS)
        except Exception as exc:
            log_event(f"Node prune failed: {exc}", "ERROR")

        try:
            refresh_recent_buffers_from_log()
        except Exception as exc:
            log_event(f"Log refresh failed: {exc}", "ERROR")

        time.sleep(15)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/healthz")
def healthz():
    web_cfg = get_web_config()
    return jsonify(
        {
            "ok": True,
            "service": get_service_status(),
            "timestamp": time.time(),
            "web": {
                "host": web_cfg.get("host", "0.0.0.0"),
                "port": int(web_cfg.get("port", 8443)),
            },
        }
    )


@app.route("/api/status", methods=["GET"])
def api_status():
    nodes = format_nodes_for_ui()
    web_cfg = get_web_config()
    tak_cfg = get_tak_config()

    return jsonify(
        {
            "service": get_service_status(),
            "timestamp": time.time(),
            "https_port": int(web_cfg.get("port", 8443)),
            "log_file": LOG_FILE,
            "node_count": len(nodes),
            "nodes": nodes,
            "tak": {
                "enabled": bool(tak_cfg.get("enabled", False)),
                "host": str(tak_cfg.get("host", "")).strip(),
                "port": int(tak_cfg.get("port", 8088)),
            },
            "recent_tak": list(_recent_tak)[-25:],
            "recent_errors": list(_recent_errors)[-25:],
            "recent_log": list(_recent_log)[-100:],
            "queued_messages": list_queued_messages(limit=25),
        }
    )


@app.route("/api/nodes", methods=["GET"])
def api_nodes():
    return jsonify(
        {
            "timestamp": time.time(),
            "nodes": format_nodes_for_ui(),
        }
    )


@app.route("/api/messages", methods=["GET"])
def api_messages():
    limit_raw = request.args.get("limit", "50")
    try:
        limit = max(1, min(200, int(limit_raw)))
    except Exception:
        limit = 50

    return jsonify(
        {
            "timestamp": time.time(),
            "messages": list_queued_messages(limit=limit),
        }
    )


@app.route("/api/send-message", methods=["POST"])
def api_send_message():
    payload = request.get_json(silent=True) or {}

    text = str(payload.get("text") or "").strip()
    destination = str(payload.get("destination") or "broadcast").strip() or "broadcast"
    sender = str(payload.get("sender") or "webui").strip() or "webui"

    try:
        channel = int(payload.get("channel", 0))
    except Exception:
        channel = 0

    want_ack = bool(payload.get("want_ack", False))

    if not text:
        return jsonify({"ok": False, "error": "Message text is required"}), 400

    try:
        item = enqueue_message(
            text=text,
            destination=destination,
            channel=channel,
            want_ack=want_ack,
            sender=sender,
        )
        log_event(
            f"Queued outbound Meshtastic message destination={destination} channel={channel} text={text}",
            "INFO",
        )
        return jsonify({"ok": True, "queued": item})
    except Exception as exc:
        log_event(f"Failed to queue outbound message: {exc}", "ERROR")
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/config", methods=["GET"])
def api_get_config():
    cfg = load_config()
    return jsonify(
        {
            "ok": True,
            "config": cfg,
        }
    )


@app.route("/api/config/tak", methods=["GET"])
def api_get_tak_config():
    tak_cfg = get_tak_config()
    return jsonify(
        {
            "ok": True,
            "tak": {
                "enabled": bool(tak_cfg.get("enabled", False)),
                "host": str(tak_cfg.get("host", "")).strip(),
                "port": int(tak_cfg.get("port", 8088)),
            },
        }
    )


@app.route("/api/config/tak", methods=["POST"])
def api_update_tak_config():
    payload = request.get_json(silent=True) or {}

    enabled = bool(payload.get("enabled", False))
    host = str(payload.get("host", "")).strip()

    try:
        port = int(payload.get("port", 8088))
    except Exception:
        return jsonify({"ok": False, "error": "TAK port must be numeric"}), 400

    if port < 1 or port > 65535:
        return jsonify({"ok": False, "error": "TAK port must be between 1 and 65535"}), 400

    if enabled and not host:
        return jsonify({"ok": False, "error": "TAK host is required when TAK forwarding is enabled"}), 400

    try:
        cfg = update_config(
            {
                "tak": {
                    "enabled": enabled,
                    "host": host,
                    "port": port,
                }
            }
        )
        tak_cfg = cfg.get("tak", {})
        log_event(
            f"Updated TAK config enabled={tak_cfg.get('enabled')} host={tak_cfg.get('host')} port={tak_cfg.get('port')}",
            "INFO",
        )
        return jsonify(
            {
                "ok": True,
                "tak": {
                    "enabled": bool(tak_cfg.get("enabled", False)),
                    "host": str(tak_cfg.get("host", "")).strip(),
                    "port": int(tak_cfg.get("port", 8088)),
                },
            }
        )
    except Exception as exc:
        log_event(f"Failed to update TAK config: {exc}", "ERROR")
        return jsonify({"ok": False, "error": str(exc)}), 500


def build_ssl_context():
    if not (os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE)):
        raise FileNotFoundError(
            f"Missing TLS certificate or key: cert={CERT_FILE} key={KEY_FILE}"
        )

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=CERT_FILE, keyfile=KEY_FILE)
    return context


def main():
    os.makedirs(APP_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

    refresh_recent_buffers_from_log()

    t = threading.Thread(target=background_maintenance, daemon=True)
    t.start()

    web_cfg = get_web_config()
    web_host = str(web_cfg.get("host", "0.0.0.0")).strip() or "0.0.0.0"
    web_port = int(web_cfg.get("port", 8443))

    log_event(
        f"WEB: Starting HTTPS server on {web_host}:{web_port}",
        "INFO",
    )

    ssl_context = build_ssl_context()
    app.run(
        host=web_host,
        port=web_port,
        ssl_context=ssl_context,
        debug=False,
        use_reloader=False,
        threaded=True,
    )


if __name__ == "__main__":
    main()
