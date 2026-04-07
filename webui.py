#!/usr/bin/env python3
import copy
import json
import logging
import os
import ssl
import threading
import time
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, render_template, request
from werkzeug.utils import secure_filename

from meshtak import MeshTAK, CONFIG_PATH

BASE_DIR = "/opt/meshtak"
STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")
CERT_DIR = os.path.join(BASE_DIR, "certs")
LOG_DIR = os.path.join(BASE_DIR, "logs")

CERT_PATH = os.path.join(CERT_DIR, "meshtak.crt")
KEY_PATH = os.path.join(CERT_DIR, "meshtak.key")
LOG_PATH = os.path.join(LOG_DIR, "webui.log")
UPLOAD_ALLOWED = {".pem", ".crt", ".cer", ".key", ".p12", ".pfx"}


def ensure_log_dir() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(CERT_DIR, exist_ok=True)


def build_logger() -> logging.Logger:
    ensure_log_dir()
    logger = logging.getLogger("meshtak.webui")
    logger.setLevel(logging.INFO)

    if logger.handlers:
        return logger

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    file_handler = logging.FileHandler(LOG_PATH)
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    logger.propagate = False
    return logger


log = build_logger()


def _deep_merge(base: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _boolify(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "y"}
    return bool(value)


class MeshTAKWebUI:
    def __init__(self, mesh: Optional[MeshTAK] = None):
        self.mesh = mesh or MeshTAK()
        self.app = Flask(__name__, static_folder=STATIC_DIR, template_folder=TEMPLATE_DIR)
        self._config_lock = threading.RLock()
        self._register_routes()

    def _load_config_from_disk(self) -> Dict[str, Any]:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save_config_to_disk(self, config: Dict[str, Any]) -> None:
        tmp_path = f"{CONFIG_PATH}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, sort_keys=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, CONFIG_PATH)

    def _normalize_channels(self, channels: Any) -> List[Dict[str, Any]]:
        normalized: List[Dict[str, Any]] = []
        seen = set()
        if not isinstance(channels, list):
            return normalized

        for item in channels:
            if not isinstance(item, dict):
                continue
            try:
                index = int(item.get("index", 0))
            except Exception:
                index = 0
            name = str(item.get("name", "")).strip() or f"Channel {index}"
            key = (index, name.lower())
            if key in seen:
                continue
            seen.add(key)
            normalized.append({"name": name, "index": index, "pinned": _boolify(item.get("pinned", True))})
        normalized.sort(key=lambda x: (0 if x.get("pinned") else 1, int(x.get("index", 0)), x.get("name", "").lower()))
        return normalized

    def _config_view(self, config: Dict[str, Any]) -> Dict[str, Any]:
        connection = config.get("connection", {})
        tak = config.get("tak", {})
        web = config.get("web", {})
        channels = self._normalize_channels(config.get("channels", []))

        return {
            "connection": {
                "type": connection.get("type", "serial"),
                "port": connection.get("port", ""),
                "host": connection.get("host", ""),
            },
            "tak": {
                "enabled": _boolify(tak.get("enabled", False)),
                "host": tak.get("host", ""),
                "port": int(tak.get("port", 8088) or 8088),
                "protocol": str(tak.get("protocol", "tcp")).strip().lower() or "tcp",
                "tls": _boolify(tak.get("tls", False)),
                "ca_cert": str(tak.get("ca_cert", "")).strip(),
                "client_cert": str(tak.get("client_cert", "")).strip(),
                "client_key": str(tak.get("client_key", "")).strip(),
            },
            "web": {
                "host": web.get("host", "0.0.0.0"),
                "port": int(web.get("port", 9443) or 9443),
                "tls_cert": web.get("tls_cert", CERT_PATH),
                "tls_key": web.get("tls_key", KEY_PATH),
            },
            "channels": channels,
        }

    def _safe_node_payload(self, node: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "node_id": node.get("node_id", ""),
            "display_name": node.get("display_name", ""),
            "long_name": node.get("long_name", ""),
            "short_name": node.get("short_name", ""),
            "hw_model": node.get("hw_model", ""),
            "role": node.get("role", ""),
            "lat": node.get("lat"),
            "lon": node.get("lon"),
            "alt": node.get("alt"),
            "batt": node.get("batt"),
            "snr": node.get("snr"),
            "rssi": node.get("rssi"),
            "hop_limit": node.get("hop_limit"),
            "via": node.get("via", ""),
            "last_heard": node.get("last_heard"),
            "updated_at": node.get("updated_at"),
        }

    def _safe_message_payload(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": msg.get("id", ""),
            "direction": msg.get("direction", ""),
            "text": msg.get("text", ""),
            "from_id": msg.get("from_id", ""),
            "from_name": msg.get("from_name", ""),
            "to_id": msg.get("to_id", ""),
            "to_name": msg.get("to_name", ""),
            "channel": msg.get("channel", ""),
            "acked": bool(msg.get("acked", False)),
            "timestamp": msg.get("timestamp"),
            "created_at": msg.get("created_at"),
        }

    def _message_targets(self) -> List[Dict[str, Any]]:
        config = self.mesh.get_config()
        channels = self._normalize_channels(config.get("channels", []))
        nodes = self.mesh.store.get_nodes()
        targets: List[Dict[str, Any]] = [
            {
                "kind": "broadcast",
                "label": "Broadcast",
                "to": None,
                "channel_index": 0,
                "channel_name": "Broadcast",
                "pinned": True,
            }
        ]

        for channel in channels:
            targets.append(
                {
                    "kind": "channel",
                    "label": f"{channel['name']} (ch {channel['index']})",
                    "to": None,
                    "channel_index": int(channel.get("index", 0)),
                    "channel_name": channel.get("name", ""),
                    "pinned": True,
                }
            )

        node_targets: List[Dict[str, Any]] = []
        seen = set()
        for node in nodes:
            node_id = str(node.get("node_id") or "").strip()
            if not node_id or node_id in seen:
                continue
            seen.add(node_id)
            label = str(node.get("short_name") or node.get("display_name") or node.get("long_name") or node_id).strip() or node_id
            node_targets.append(
                {
                    "kind": "node",
                    "label": label,
                    "to": node_id,
                    "channel_index": 0,
                    "channel_name": "Direct",
                    "pinned": False,
                }
            )
        node_targets.sort(key=lambda x: x["label"].lower())
        targets.extend(node_targets)
        return targets

    def _register_routes(self) -> None:
        app = self.app

        @app.get("/")
        def index():
            return render_template("index.html")

        @app.get("/health")
        def health():
            return jsonify({
                "ok": True,
                "service": "meshtak",
                "connected": self.mesh.is_connected(),
                "time": int(time.time()),
            })

        @app.get("/api/status")
        def api_status():
            stats = self.mesh.store.stats()
            config = self.mesh.get_config()
            return jsonify({
                "running": self.mesh.running,
                "connected": self.mesh.is_connected(),
                "connection_type": config.get("connection", {}).get("type", "serial"),
                "tak_enabled": _boolify(config.get("tak", {}).get("enabled", False)),
                "tak_protocol": str(config.get("tak", {}).get("protocol", "tcp")).strip().lower() or "tcp",
                "tak_queue_count": stats.get("queue_count", 0),
                "stats": stats,
                "updated_at": int(time.time()),
            })

        @app.get("/api/config")
        def api_get_config():
            return jsonify(self._config_view(self.mesh.get_config()))

        @app.post("/api/config")
        def api_save_config():
            payload = request.get_json(silent=True) or {}
            if not isinstance(payload, dict):
                return jsonify({"ok": False, "error": "Invalid JSON payload"}), 400

            with self._config_lock:
                current = self._load_config_from_disk()
                merged = _deep_merge(current, payload)

                connection = merged.setdefault("connection", {})
                tak = merged.setdefault("tak", {})
                web = merged.setdefault("web", {})
                merged["channels"] = self._normalize_channels(merged.get("channels", []))

                connection["type"] = str(connection.get("type", "serial")).strip().lower()
                if connection["type"] not in {"serial", "tcp"}:
                    return jsonify({"ok": False, "error": "connection.type must be serial or tcp"}), 400

                if connection["type"] == "serial":
                    connection["port"] = str(connection.get("port", "/dev/ttyACM0")).strip() or "/dev/ttyACM0"
                    connection.pop("host", None)
                else:
                    connection["host"] = str(connection.get("host", "")).strip()
                    if not connection["host"]:
                        return jsonify({"ok": False, "error": "TCP host is required"}), 400
                    connection.pop("port", None)

                tak["enabled"] = _boolify(tak.get("enabled", False))
                tak["host"] = str(tak.get("host", "")).strip()
                tak["port"] = int(tak.get("port", 8088) or 8088)
                tak["protocol"] = str(tak.get("protocol", "tcp")).strip().lower() or "tcp"
                tak["tls"] = _boolify(tak.get("tls", False))
                tak["ca_cert"] = str(tak.get("ca_cert", "")).strip()
                tak["client_cert"] = str(tak.get("client_cert", "")).strip()
                tak["client_key"] = str(tak.get("client_key", "")).strip()
                if tak["protocol"] not in {"tcp", "udp"}:
                    return jsonify({"ok": False, "error": "tak.protocol must be tcp or udp"}), 400

                web["host"] = str(web.get("host", "0.0.0.0")).strip() or "0.0.0.0"
                web["port"] = int(web.get("port", 9443) or 9443)
                web["tls_cert"] = str(web.get("tls_cert", CERT_PATH)).strip() or CERT_PATH
                web["tls_key"] = str(web.get("tls_key", KEY_PATH)).strip() or KEY_PATH

                self._save_config_to_disk(merged)
                self.mesh.reload_config()

            log.info("Configuration updated via Web UI")
            return jsonify({"ok": True, "message": "Configuration saved", "config": self._config_view(self.mesh.get_config())})

        @app.post("/api/tak-certs")
        def api_tak_certs():
            saved = {}
            with self._config_lock:
                config = self._load_config_from_disk()
                tak = config.setdefault("tak", {})
                for field, config_key in (("ca_cert", "ca_cert"), ("client_cert", "client_cert"), ("client_key", "client_key")):
                    file = request.files.get(field)
                    if not file or not file.filename:
                        continue
                    filename = secure_filename(file.filename)
                    ext = os.path.splitext(filename)[1].lower()
                    if ext not in UPLOAD_ALLOWED:
                        return jsonify({"ok": False, "error": f"Unsupported file type for {field}"}), 400
                    save_name = f"tak_{field}{ext}"
                    save_path = os.path.join(CERT_DIR, save_name)
                    file.save(save_path)
                    os.chmod(save_path, 0o640)
                    tak[config_key] = save_path
                    saved[config_key] = save_path
                self._save_config_to_disk(config)
                self.mesh.reload_config()
            return jsonify({"ok": True, "saved": saved, "config": self._config_view(self.mesh.get_config())})

        @app.get("/api/message-targets")
        def api_message_targets():
            return jsonify({"ok": True, "targets": self._message_targets(), "updated_at": int(time.time())})

        @app.get("/api/nodes")
        def api_nodes():
            nodes = [self._safe_node_payload(n) for n in self.mesh.store.get_nodes()]
            return jsonify({"ok": True, "nodes": nodes, "count": len(nodes), "updated_at": int(time.time())})

        @app.get("/api/map")
        def api_map():
            nodes = []
            for node in self.mesh.store.get_nodes():
                if node.get("lat") is None or node.get("lon") is None:
                    continue
                nodes.append(self._safe_node_payload(node))
            return jsonify({"ok": True, "nodes": nodes, "count": len(nodes), "updated_at": int(time.time())})

        @app.get("/api/messages")
        def api_messages():
            limit = request.args.get("limit", default=200, type=int)
            limit = max(1, min(limit or 200, 1000))
            messages = [self._safe_message_payload(m) for m in self.mesh.store.get_messages(limit=limit)]
            return jsonify({"ok": True, "messages": messages, "count": len(messages), "updated_at": int(time.time())})

        @app.post("/api/messages/send")
        def api_send_message():
            payload = request.get_json(silent=True) or {}
            text = str(payload.get("text", "")).strip()
            destination = str(payload.get("to", "")).strip() or None
            channel_name = str(payload.get("channel_name", "")).strip() or None
            channel_index = payload.get("channel_index", None)

            if not text:
                return jsonify({"ok": False, "error": "Message text is required"}), 400

            try:
                if channel_index in ("", None):
                    channel_index = None
                else:
                    channel_index = int(channel_index)
                self.mesh.queue_tx(text=text, to=destination, channel_index=channel_index, channel_name=channel_name)
            except Exception as exc:
                log.exception("Failed to queue TX message")
                return jsonify({"ok": False, "error": str(exc)}), 500

            return jsonify({
                "ok": True,
                "message": "Queued for transmit",
                "queued": {"text": text, "to": destination, "channel_index": channel_index, "channel_name": channel_name},
            })

    def run(self) -> None:
        config = self.mesh.get_config()
        web_cfg = config.get("web", {})

        host = str(web_cfg.get("host", "0.0.0.0")).strip() or "0.0.0.0"
        port = int(web_cfg.get("port", 9443) or 9443)
        cert_path = str(web_cfg.get("tls_cert", CERT_PATH)).strip() or CERT_PATH
        key_path = str(web_cfg.get("tls_key", KEY_PATH)).strip() or KEY_PATH

        ssl_context = None
        if os.path.exists(cert_path) and os.path.exists(key_path):
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certfile=cert_path, keyfile=key_path)
            ssl_context = context
            log.info("Starting HTTPS Web UI on %s:%s", host, port)
        else:
            log.warning("TLS cert or key missing. Starting HTTP on %s:%s", host, port)

        self.app.run(host=host, port=port, ssl_context=ssl_context, threaded=True)


if __name__ == "__main__":
    ui = MeshTAKWebUI()
    ui.run()
