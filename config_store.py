#!/usr/bin/env python3
import json
import os
import threading
from copy import deepcopy

CONFIG_FILE = "/opt/meshtak/config.json"

DEFAULT_CONFIG = {
    "meshtastic": {
        "mode": "serial",
        "serial_device": "/dev/ttyACM0",
        "host": "",
        "port": 4403,
    },
    "tak": {
        "enabled": False,
        "host": "",
        "port": 8088,
    },
    "web": {
        "host": "0.0.0.0",
        "port": 8443,
    },
}

_lock = threading.Lock()


def _ensure_parent(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)


def _deep_merge(defaults, incoming):
    result = deepcopy(defaults)

    if not isinstance(incoming, dict):
        return result

    for key, value in incoming.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value

    return result


def _normalize_config(cfg):
    cfg = _deep_merge(DEFAULT_CONFIG, cfg)

    meshtastic = cfg.get("meshtastic", {})
    tak = cfg.get("tak", {})
    web = cfg.get("web", {})

    mode = str(meshtastic.get("mode", "serial")).strip().lower()
    if mode not in ("serial", "ip"):
        mode = "serial"

    serial_device = str(meshtastic.get("serial_device", "/dev/ttyACM0")).strip() or "/dev/ttyACM0"
    host = str(meshtastic.get("host", "")).strip()

    try:
        mesh_port = int(meshtastic.get("port", 4403))
    except Exception:
        mesh_port = 4403

    try:
        tak_enabled = bool(tak.get("enabled", False))
    except Exception:
        tak_enabled = False

    tak_host = str(tak.get("host", "")).strip()

    try:
        tak_port = int(tak.get("port", 8088))
    except Exception:
        tak_port = 8088

    web_host = str(web.get("host", "0.0.0.0")).strip() or "0.0.0.0"

    try:
        web_port = int(web.get("port", 8443))
    except Exception:
        web_port = 8443

    return {
        "meshtastic": {
            "mode": mode,
            "serial_device": serial_device,
            "host": host,
            "port": mesh_port,
        },
        "tak": {
            "enabled": tak_enabled,
            "host": tak_host,
            "port": tak_port,
        },
        "web": {
            "host": web_host,
            "port": web_port,
        },
    }


def load_config():
    with _lock:
        if not os.path.exists(CONFIG_FILE):
            return deepcopy(DEFAULT_CONFIG)

        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return deepcopy(DEFAULT_CONFIG)

        return _normalize_config(data)


def save_config(config):
    normalized = _normalize_config(config)

    with _lock:
        _ensure_parent(CONFIG_FILE)
        tmp = f"{CONFIG_FILE}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(normalized, f, indent=2, sort_keys=False)
        os.replace(tmp, CONFIG_FILE)

    return normalized


def update_config(partial):
    current = load_config()
    merged = _deep_merge(current, partial)
    return save_config(merged)


def get_meshtastic_config():
    return load_config().get("meshtastic", deepcopy(DEFAULT_CONFIG["meshtastic"]))


def get_tak_config():
    return load_config().get("tak", deepcopy(DEFAULT_CONFIG["tak"]))


def get_web_config():
    return load_config().get("web", deepcopy(DEFAULT_CONFIG["web"]))
