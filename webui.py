#!/usr/bin/env python3
import os
import re
import json
import subprocess
from collections import deque
from flask import Flask, jsonify, render_template
from node_store import get_nodes

app = Flask(__name__, template_folder="/opt/meshtak/templates", static_folder="/opt/meshtak/static")

LOG_FILE = "/var/log/meshtak.log"
SERVICE_NAME = "meshtak"
MAX_LOG_LINES = 500


def read_recent_lines(path, max_lines=MAX_LOG_LINES):
    if not os.path.exists(path):
        return []

    dq = deque(maxlen=max_lines)
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            dq.append(line.rstrip())
    return list(dq)


def parse_nodes(lines):
    nodes = {}
    name_re = re.compile(r"NAME <- (.+?) \[(.+?)\]")
    tak_re = re.compile(r"TAK <- (.+?) \[(.+?)\] ([\-0-9.]+),([\-0-9.]+) hae=([^\s]+) source=([^\s]+) uid=(.+)$")

    for line in lines:
        m = name_re.search(line)
        if m:
            callsign, node_id = m.groups()
            nodes.setdefault(node_id, {})
            nodes[node_id]["callsign"] = callsign
            nodes[node_id]["node_id"] = node_id
            nodes[node_id]["last_name_line"] = line

        m = tak_re.search(line)
        if m:
            callsign, node_id, lat, lon, hae, source, uid = m.groups()
            nodes.setdefault(node_id, {})
            nodes[node_id].update({
                "callsign": callsign,
                "node_id": node_id,
                "lat": lat,
                "lon": lon,
                "hae": hae,
                "source": source,
                "uid": uid,
                "last_tak_line": line
            })

    return sorted(nodes.values(), key=lambda x: x.get("callsign", x.get("node_id", "")))


def get_service_status():
    try:
        result = subprocess.run(
            ["systemctl", "is-active", SERVICE_NAME],
            capture_output=True,
            text=True,
            check=False
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

    tak_lines = [line for line in lines if "TAK <- " in line][-50:]
    error_lines = [line for line in lines if "ERROR" in line or "WARNING" in line][-50:]

    return jsonify({
        "service": get_service_status(),
        "log_file": LOG_FILE,
        "node_count": len(nodes),
        "nodes": nodes,
        "recent_tak": tak_lines[::-1],
        "recent_errors": error_lines[::-1],
        "recent_log": lines[-100:][::-1]
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8420, debug=False)
