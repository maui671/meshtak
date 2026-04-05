#!/usr/bin/env python3
import json
import os
import threading
import time
import uuid

NODES_FILE = "/opt/meshtak/nodes.json"
QUEUE_FILE = "/opt/meshtak/message_queue.json"

lock = threading.Lock()


def _ensure_parent(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)


def _load_json(path, default):
    if not os.path.exists(path):
      return default

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data
    except Exception:
        return default


def _save_json(path, data):
    _ensure_parent(path)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=False)
    os.replace(tmp, path)


def _normalize_node_id(node_id):
    return str(node_id).strip()


def get_nodes():
    with lock:
        data = _load_json(NODES_FILE, {})
        if isinstance(data, dict):
            return data
        return {}


def update_node(node_id, new_data):
    node_id = _normalize_node_id(node_id)
    if not node_id:
        return

    with lock:
        data = _load_json(NODES_FILE, {})
        if not isinstance(data, dict):
            data = {}

        node = data.get(node_id, {})
        if not isinstance(node, dict):
            node = {}

        if isinstance(new_data, dict):
            node.update(new_data)

        node["node_id"] = node_id
        node["last_seen"] = time.time()

        data[node_id] = node
        _save_json(NODES_FILE, data)


def delete_node(node_id):
    node_id = _normalize_node_id(node_id)
    if not node_id:
        return

    with lock:
        data = _load_json(NODES_FILE, {})
        if not isinstance(data, dict):
            data = {}

        if node_id in data:
            del data[node_id]
            _save_json(NODES_FILE, data)


def clear_nodes():
    with lock:
        _save_json(NODES_FILE, {})


def enqueue_message(
    text,
    destination="broadcast",
    channel=0,
    want_ack=False,
    sender="webui",
):
    text = str(text or "").strip()
    destination = str(destination or "broadcast").strip() or "broadcast"

    if not text:
        raise ValueError("Message text cannot be empty")

    item = {
        "id": str(uuid.uuid4()),
        "created_at": time.time(),
        "destination": destination,
        "channel": int(channel),
        "want_ack": bool(want_ack),
        "sender": str(sender or "webui").strip() or "webui",
        "text": text,
        "status": "queued",
    }

    with lock:
        queue = _load_json(QUEUE_FILE, [])
        if not isinstance(queue, list):
            queue = []

        queue.append(item)
        _save_json(QUEUE_FILE, queue)

    return item


def list_queued_messages(limit=50):
    with lock:
        queue = _load_json(QUEUE_FILE, [])
        if not isinstance(queue, list):
            queue = []

        queue = sorted(
            queue,
            key=lambda x: float(x.get("created_at", 0)),
            reverse=True,
        )
        return queue[: max(1, int(limit))]


def dequeue_messages(limit=20):
    with lock:
        queue = _load_json(QUEUE_FILE, [])
        if not isinstance(queue, list):
            queue = []

        ready = []
        remaining = []

        for item in queue:
            if (
                isinstance(item, dict)
                and item.get("status") == "queued"
                and len(ready) < int(limit)
            ):
                item["status"] = "dispatched"
                item["dispatched_at"] = time.time()
                ready.append(item)
            else:
                remaining.append(item)

        _save_json(QUEUE_FILE, remaining)
        return ready


def clear_message_queue():
    with lock:
        _save_json(QUEUE_FILE, [])


def prune_old_nodes(max_age_seconds=86400):
    cutoff = time.time() - int(max_age_seconds)

    with lock:
        data = _load_json(NODES_FILE, {})
        if not isinstance(data, dict):
            data = {}

        kept = {}
        for node_id, node in data.items():
            if not isinstance(node, dict):
                continue
            if float(node.get("last_seen", 0)) >= cutoff:
                kept[node_id] = node

        if kept != data:
            _save_json(NODES_FILE, kept)
