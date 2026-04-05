#!/usr/bin/env python3
import json
import os
import tempfile
import time
from typing import Dict, Any

STATE_FILE = "/opt/meshtak/runtime/nodes.json"


def _ensure_parent():
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)


def _load_state() -> Dict[str, Any]:
    _ensure_parent()

    if not os.path.exists(STATE_FILE):
        return {}

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass

    return {}


def _save_state(data: Dict[str, Any]) -> None:
    _ensure_parent()

    fd, tmp_path = tempfile.mkstemp(prefix="nodes.", suffix=".json", dir=os.path.dirname(STATE_FILE))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            json.dump(data, tmp, indent=2, sort_keys=True)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, STATE_FILE)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except Exception:
            pass


def update_node(node_id: str, new_data: Dict[str, Any]) -> None:
    state = _load_state()
    node = state.get(node_id, {})
    node.update(new_data)
    node["node_id"] = node_id
    node["last_seen"] = time.time()
    state[node_id] = node
    _save_state(state)


def get_nodes() -> Dict[str, Any]:
    return _load_state()


def clear_nodes() -> None:
    _save_state({})
