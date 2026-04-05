import json
import os
import threading
import time

STORE_FILE = "/opt/meshtak/nodes.json"
lock = threading.Lock()


def _load():
    if not os.path.exists(STORE_FILE):
        return {}
    try:
        with open(STORE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(data):
    os.makedirs(os.path.dirname(STORE_FILE), exist_ok=True)
    tmp = STORE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, STORE_FILE)


def update_node(node_id, new_data):
    with lock:
        data = _load()
        node = data.get(node_id, {})
        node.update(new_data)
        node["node_id"] = node_id
        node["last_seen"] = time.time()
        data[node_id] = node
        _save(data)


def get_nodes():
    with lock:
        return _load()
