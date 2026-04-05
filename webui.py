#!/usr/bin/env python3
import os
import time
import subprocess
from collections import deque

from flask import Flask, jsonify, render_template

from node_store import get_nodes

app = Flask(
    __name__,
    template_folder="/opt/meshtak/templates",
    static_folder="/opt/meshtak/static",
)

LOG_FILE = "/var/log/meshtak.log"
SERVICE_NAME = "meshtak"
MAX_LOG_LINES = 500

HTTPS_CERT_FILE = "/etc/meshtak/certs/meshtak.crt"
HTTPS_KEY_FILE = "/etc/meshtak/certs/meshtak.key"
HTTPS_HOST = "0.0.0.0"
HTTPS_PORT = 8443


def read_recent_lines(path, max_lines=MAX_LOG_LINES):
    if not os.path.exists(path):
        return []

    dq = deque(maxlen=max_lines)
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            dq.append(line.rstrip())
    return list(dq)


def get_service_status():
    try:
        result = subprocess.run(
            ["systemctl", "is-active", SERVICE_NAME],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    lines = read_recent_lines(LOG_FILE)

    nodes_dict = get_nodes()
    nodes = list(nodes_dict.values())
    nodes = sorted(nodes, key=lambda x: x.get("callsign", x.get("node_id", "")))

    tak_lines = [line for line in lines if "TAK <- " in line][-50:]
    error_lines = [line for line in lines if "ERROR" in line or "WARNING" in line][-50:]

    return jsonify({
        "service": get_service_status(),
        "log_file": LOG_FILE,
        "node_count": len(nodes),
        "nodes": nodes,
        "recent_tak": tak_lines[::-1],
        "recent_errors": error_lines[::-1],
        "recent_log": lines[-100:][::-1],
        "timestamp": time.time(),
        "https_port": HTTPS_PORT,
    })


def main():
    ssl_context = None
    if os.path.isfile(HTTPS_CERT_FILE) and os.path.isfile(HTTPS_KEY_FILE):
        ssl_context = (HTTPS_CERT_FILE, HTTPS_KEY_FILE)

    app.run(
        host=HTTPS_HOST,
        port=HTTPS_PORT,
        debug=False,
        ssl_context=ssl_context,
    )


if __name__ == "__main__":
    main()
