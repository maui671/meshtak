#!/usr/bin/env python3
import copy
import json
import logging
import os
import ssl
import threading
import time
from typing import Any, Dict, Optional

from flask import Flask, jsonify, render_template, request

from meshtak import MeshTAK, CONFIG_PATH

BASE_DIR = "/opt/meshtak"
STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")
CERT_DIR = os.path.join(BASE_DIR, "certs")
LOG_DIR = os.path.join(BASE_DIR, "logs")

CERT_PATH = os.path.join(CERT_DIR, "meshtak.crt")
KEY_PATH = os.path.join(CERT_DIR, "meshtak.key")
LOG_PATH = os.path.join(LOG_DIR, "webui.log")


def ensure_log_dir() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)


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
        self.app = Flask(
            __name__,
            static_folder=STATIC_DIR,
            template_folder=TEMPLATE_DIR,
        )
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

    def _config_view(self, config: Dict[str, Any]) -> Dict[str, Any]:
        connection = config.get("connection", {})
        tak = config.get("tak", {})
        web = config.get("web", {})

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
                "tls": _boolify(tak.get("tls", False)),
            },
            "web": {
                "host": web.get("host", "0.0.0.0"),
                "port": int(web.get("port", 8443) or 8443),
                "tls_cert": web.get("tls_cert", CERT_PATH),
                "tls_key": web.get("tls_key", KEY_PATH),
            },
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

    def _register_routes(self) -> None:
        app = self.app

        @app.get("/")
        def index():
            return render_template("index.html")

        @app.get("/health")
        def health():
            connected = False
            try:
                connected = self.mesh.is_connected()
            except Exception:
                connected = False

            return jsonify(
                {
                    "ok": True,
                    "service": "meshtak",
                    "connected": connected,
                    "time": int(time.time()),
                }
            )

        @app.get("/api/status")
        def api_status():
            stats = self.mesh.store.stats()
            config = self.mesh.get_config()
            state = {
                "running": self.mesh.running,
                "connected": self.mesh.is_connected(),
                "connection_type": config.get("connection", {}).get("type", "serial"),
                "tak_enabled": _boolify(config.get("tak", {}).get("enabled", False)),
                "tak_queue_count": stats.get("queue_count", 0),
                "stats": stats,
                "updated_at": int(time.time()),
            }
            return jsonify(state)

        @app.get("/api/config")
        def api_get_config():
            config = self.mesh.get_config()
            return jsonify(self._config_view(config))

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
                tak["tls"] = _boolify(tak.get("tls", False))

                web["host"] = str(web.get("host", "0.0.0.0")).strip() or "0.0.0.0"
                web["port"] = int(web.get("port", 8443) or 8443)
                web["tls_cert"] = str(web.get("tls_cert", CERT_PATH)).strip() or CERT_PATH
                web["tls_key"] = str(web.get("tls_key", KEY_PATH)).strip() or KEY_PATH

                self._save_config_to_disk(merged)
                self.mesh.reload_config()

            log.info("Configuration updated via Web UI")
            return jsonify(
                {
                    "ok": True,
                    "message": "Configuration saved",
                    "config": self._config_view(self.mesh.get_config()),
                }
            )

        @app.get("/api/nodes")
        def api_nodes():
            nodes = [self._safe_node_payload(n) for n in self.mesh.store.get_nodes()]
            return jsonify(
                {
                    "ok": True,
                    "nodes": nodes,
                    "count": len(nodes),
                    "updated_at": int(time.time()),
                }
            )

        @app.get("/api/messages")
        def api_messages():
            limit = request.args.get("limit", default=200, type=int)
            limit = max(1, min(limit or 200, 1000))
            messages = [self._safe_message_payload(m) for m in self.mesh.store.get_messages(limit=limit)]
            return jsonify(
                {
                    "ok": True,
                    "messages": messages,
                    "count": len(messages),
                    "updated_at": int(time.time()),
                }
            )

        @app.post("/api/messages/send")
        def api_send_message():
            payload = request.get_json(silent=True) or {}
            text = str(payload.get("text", "")).strip()
            destination = str(payload.get("to", "")).strip() or None

            if not text:
                return jsonify({"ok": False, "error": "Message text is required"}), 400

            self.mesh.queue_tx(text=text, to=destination)

            return jsonify(
                {
                    "ok": True,
                    "message": "Queued for send",
                    "queued": {
                        "text": text,
                        "to": destination,
                    },
                }
            )

        @app.get("/api/map")
        def api_map():
            nodes = []
            for node in self.mesh.store.get_nodes():
                if node.get("lat") is None or node.get("lon") is None:
                    continue
                nodes.append(
                    {
                        "node_id": node.get("node_id", ""),
                        "display_name": node.get("display_name", ""),
                        "lat": node.get("lat"),
                        "lon": node.get("lon"),
                        "alt": node.get("alt"),
                        "last_heard": node.get("last_heard"),
                        "batt": node.get("batt"),
                        "snr": node.get("snr"),
                        "rssi": node.get("rssi"),
                    }
                )

            return jsonify(
                {
                    "ok": True,
                    "nodes": nodes,
                    "count": len(nodes),
                    "updated_at": int(time.time()),
                }
            )

        @app.get("/api/debug")
        def api_debug():
            stats = self.mesh.store.stats()
            queue_items = self.mesh.store.get_queue()
            return jsonify(
                {
                    "ok": True,
                    "stats": stats,
                    "connected": self.mesh.is_connected(),
                    "config": self._config_view(self.mesh.get_config()),
                    "queue_preview": queue_items[-10:],
                    "updated_at": int(time.time()),
                }
            )

    def run(self) -> None:
        config = self.mesh.get_config()
        web = config.get("web", {})
        host = str(web.get("host", "0.0.0.0")).strip() or "0.0.0.0"
        port = int(web.get("port", 8443) or 8443)
        cert_path = str(web.get("tls_cert", CERT_PATH)).strip() or CERT_PATH
        key_path = str(web.get("tls_key", KEY_PATH)).strip() or KEY_PATH

        if not os.path.exists(cert_path):
            raise RuntimeError(f"TLS certificate not found: {cert_path}")
        if not os.path.exists(key_path):
            raise RuntimeError(f"TLS key not found: {key_path}")

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=cert_path, keyfile=key_path)

        log.info("Starting MeshTAK Web UI on https://%s:%s", host, port)
        self.app.run(
            host=host,
            port=port,
            ssl_context=context,
            debug=False,
            threaded=True,
            use_reloader=False,
        )


def main() -> None:
    ui = MeshTAKWebUI()
    ui.run()


if __name__ == "__main__":
    main()
