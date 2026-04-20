from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import shlex
import subprocess
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.analytics.network_mapper import NetworkMapper
from src.analytics.signal_analyzer import SignalAnalyzer
from src.analytics.traffic_monitor import TrafficMonitor
from src.api.routes import (
    analytics,
    device,
    nodes,
    packets,
    system_metrics,
    telemetry,
    update_check,
)
from src.api.upstream_client import UpstreamClient
from src.api.websocket_manager import WebSocketManager
from src.config import AppConfig, load_config, validate_activation
from src.coordinator import PipelineCoordinator
from src.integrations.meshtak_bridge import MeshTakBridge
from src.log_format import print_banner, print_packet, setup_logging
from src.models.device_identity import DeviceIdentity, _stable_device_id
from src.models.packet import Packet

setup_logging()
logger = logging.getLogger(__name__)

ws_manager = WebSocketManager()
pipeline: PipelineCoordinator | None = None
upstream: UpstreamClient | None = None
bridge: MeshTakBridge | None = None

_runtime_state: dict[str, dict[str, Any]] = {
    "collector": {"status": "initializing"},
    "radio": {"status": "initializing"},
    "radio_config": {
        "status": "idle",
        "requested_at": None,
        "updated_at": None,
        "message": "Radio config export has not been requested yet.",
        "last_error": None,
        "form_values": {},
        "yaml_text": "",
    },
}
_radio_config_lock = threading.Lock()
_radio_config_thread: threading.Thread | None = None
_auth_sessions: dict[str, dict[str, Any]] = {}
AUTH_COOKIE_NAME = "meshtak_session"
AUTH_SESSION_TTL = 24 * 60 * 60
USER_STORE_PATH = "/opt/meshtak/data/users.json"
DEFAULT_ADMIN_USERNAME = "tdcadmin"
DEFAULT_ADMIN_PASSWORD = "TDCnccd_dep10yed!"
SECONDARY_ADMIN_USERNAME = "maui"
SECONDARY_ADMIN_PASSWORD = "maui"


class PurgeMessagesRequest(BaseModel):
    node_id: str | None = None
    channel: str | None = None
    direction: str | None = None
    query: str | None = None


class LoginRequest(BaseModel):
    username: str
    password: str


class UserAccountRequest(BaseModel):
    username: str
    password: str
    role: str = "user"


def _hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.sha256(f"{salt}:{password}".encode("utf-8")).hexdigest()
    return f"{salt}${digest}"


def _verify_password(password: str, stored_hash: str) -> bool:
    try:
        salt, digest = stored_hash.split("$", 1)
    except ValueError:
        return False
    candidate = _hash_password(password, salt).split("$", 1)[1]
    return hmac.compare_digest(candidate, digest)


def _default_admin_records() -> dict[str, dict[str, Any]]:
    now = int(time.time())
    return {
        DEFAULT_ADMIN_USERNAME: {
            "username": DEFAULT_ADMIN_USERNAME,
            "role": "admin",
            "password_hash": _hash_password(os.getenv("MESHTAK_ADMIN_PASSWORD", DEFAULT_ADMIN_PASSWORD)),
            "created_at": now,
        },
        SECONDARY_ADMIN_USERNAME: {
            "username": SECONDARY_ADMIN_USERNAME,
            "role": "admin",
            "password_hash": _hash_password(os.getenv("MESHTAK_MAUI_PASSWORD", SECONDARY_ADMIN_PASSWORD)),
            "created_at": now,
        },
    }


def _ensure_user_store() -> dict[str, Any]:
    os.makedirs(os.path.dirname(USER_STORE_PATH), exist_ok=True)
    if not os.path.exists(USER_STORE_PATH):
        users = _default_admin_records()
        _save_user_store({"users": users})

    with open(USER_STORE_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or not isinstance(data.get("users"), dict):
        raise HTTPException(status_code=500, detail="Invalid MeshTAK user store")
    users = data.get("users", {})
    legacy_admin = users.get("admin")
    if (
        DEFAULT_ADMIN_USERNAME not in users
        and isinstance(legacy_admin, dict)
        and legacy_admin.get("role") == "admin"
        and _verify_password("admin", str(legacy_admin.get("password_hash", "")))
    ):
        users.pop("admin", None)
        users[DEFAULT_ADMIN_USERNAME] = _default_admin_records()[DEFAULT_ADMIN_USERNAME]
    default_admins = _default_admin_records()
    updated = False
    for username, record in default_admins.items():
        if username not in users:
            users[username] = record
            updated = True
    if legacy_admin is not None or updated:
        _save_user_store(data)
    return data


def _save_user_store(data: dict[str, Any]) -> None:
    with open(USER_STORE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def _current_auth_user(request: Request) -> dict[str, Any] | None:
    token = request.cookies.get(AUTH_COOKIE_NAME)
    if not token:
        return None
    session = _auth_sessions.get(token)
    if not session:
        return None
    if int(session.get("expires_at", 0)) < int(time.time()):
        _auth_sessions.pop(token, None)
        return None
    return {
        "username": session.get("username", ""),
        "role": session.get("role", "user"),
    }


def _require_admin(request: Request) -> dict[str, Any]:
    user = _current_auth_user(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin login required")
    return user


def _public_user_record(username: str, user: dict[str, Any]) -> dict[str, Any]:
    return {
        "username": str(user.get("username") or username),
        "role": str(user.get("role") or "user"),
        "created_at": user.get("created_at"),
    }


def _normalize_port(value: Any, default: int) -> int:
    try:
        port = int(value)
        return port if port > 0 else default
    except Exception:
        return default


def _bridge_required() -> MeshTakBridge:
    if bridge is None:
        raise HTTPException(status_code=503, detail="MeshTAK bridge not initialized")
    return bridge


def _build_settings_payload() -> dict[str, Any]:
    cfg = _bridge_required().get_config()

    tak = cfg.get("tak", {})
    collector = cfg.get("collector", {})
    radio = cfg.get("meshtastic_active", {})
    radio_conn = radio.get("connection", cfg.get("connection", {}))

    radio_status = "connected" if _bridge_required().is_connected() else "disconnected"

    return {
        "tak": {
            "enabled": bool(tak.get("enabled", False)),
            "host": tak.get("host", ""),
            "port": _normalize_port(tak.get("port", 8088), 8088),
            "protocol": tak.get("protocol", "udp"),
            "tls": bool(tak.get("tls", False)),
        },
        "collector": {
            "enabled": bool(collector.get("enabled", True)),
            "status": _runtime_state["collector"].get("status", "unknown"),
            "type": collector.get("type", "wm1303"),
            "config_path": collector.get("config_path", ""),
            "spi_device": collector.get("spi_device", collector.get("device", "/dev/spidev0.0")),
        },
        "radio": {
            "enabled": bool(radio.get("enabled", False)) or bool(cfg.get("connection", {}).get("enabled", False)),
            "status": radio_status,
            "type": radio_conn.get("type", "serial"),
            "serial_port": radio_conn.get("serial_port") or radio_conn.get("port", ""),
            "host": radio_conn.get("host", ""),
            "port": _normalize_port(radio_conn.get("port", 4403), 4403),
        },
    }


def _write_bridge_config(b: MeshTakBridge, cfg: dict[str, Any]) -> None:
    from src.integrations.meshtak_bridge import CONFIG_PATH

    with b._config_lock:
        b.config = cfg
        Path(CONFIG_PATH).parent.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
            f.write("\n")


def _runtime_yaml_config_path() -> Path:
    return Path(os.environ.get("CONCENTRATOR_CONFIG", "/opt/meshtak/config/local.yaml"))


def _radio_template_path() -> Path:
    candidates = [
        Path(__file__).resolve().parents[2] / "M671_config.yaml",
        Path("/opt/meshtak/M671_config.yaml"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _read_runtime_yaml_config() -> dict[str, Any]:
    import yaml

    path = _runtime_yaml_config_path()
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data if isinstance(data, dict) else {}


def _read_radio_template_config() -> tuple[dict[str, Any], str]:
    import yaml

    path = _radio_template_path()
    if not path.exists():
        return {}, ""
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    return (data if isinstance(data, dict) else {}), text


def _write_runtime_yaml_config(data: dict[str, Any]) -> None:
    import yaml

    path = _runtime_yaml_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False)


def _normalize_psk_b64(value: Any) -> str:
    psk = str(value or "").strip()
    if not psk:
        raise HTTPException(status_code=400, detail="Channel PSK is required")
    try:
        decoded = base64.b64decode(psk, validate=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Channel PSK must be valid base64") from exc
    if len(decoded) not in {1, 16, 32}:
        raise HTTPException(status_code=400, detail="Channel PSK must decode to 1, 16, or 32 bytes")
    return base64.b64encode(decoded).decode("ascii")


def _generate_psk_b64() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode("ascii")


def _safe_channel_name(value: Any) -> str:
    name = str(value or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Channel name is required")
    if len(name) > 32:
        raise HTTPException(status_code=400, detail="Channel name must be 32 characters or less")
    return name


def _normalize_channel_index(value: Any, default: int = 1) -> int:
    try:
        index = int(value)
    except Exception:
        index = default
    if index < 0 or index > 7:
        raise HTTPException(status_code=400, detail="Channel index must be between 0 and 7")
    return index


def _save_channel_key(name: str, psk_b64: str) -> dict[str, str]:
    cfg = _read_runtime_yaml_config()
    meshtastic = cfg.setdefault("meshtastic", {})
    if not isinstance(meshtastic, dict):
        meshtastic = {}
        cfg["meshtastic"] = meshtastic
    keys = meshtastic.setdefault("channel_keys", {})
    if not isinstance(keys, dict):
        keys = {}
        meshtastic["channel_keys"] = keys
    keys[name] = psk_b64
    _write_runtime_yaml_config(cfg)

    if pipeline is not None and hasattr(pipeline, "_crypto"):
        crypto = getattr(pipeline, "_crypto", None)
        if crypto is not None and hasattr(crypto, "add_channel_key"):
            crypto.add_channel_key(name, psk_b64)

    return {str(k): str(v) for k, v in keys.items()}


def _upsert_bridge_channel(name: str, index: int, psk_b64: str | None = None, pinned: bool = False) -> list[dict[str, Any]]:
    b = _bridge_required()
    cfg = b.get_config()
    channels = cfg.setdefault("channels", [])
    if not isinstance(channels, list):
        channels = []
        cfg["channels"] = channels

    existing = next((ch for ch in channels if int(ch.get("index", -1)) == index), None)
    if existing is None:
        existing = {}
        channels.append(existing)
    existing.update({"name": name, "index": index, "pinned": bool(pinned)})
    if psk_b64:
        existing["psk"] = psk_b64
    _write_bridge_config(b, cfg)
    return channels


def _to_jsonable(value: Any, depth: int = 0) -> Any:
    if depth > 5:
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_jsonable(v, depth + 1) for v in value]
    for method_name in ("to_dict", "as_dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            try:
                return _to_jsonable(method(), depth + 1)
            except Exception:
                pass
    data = getattr(value, "__dict__", None)
    if isinstance(data, dict):
        return {
            str(k): _to_jsonable(v, depth + 1)
            for k, v in data.items()
            if not str(k).startswith("_")
        }
    return str(value)


def _radio_connection() -> dict[str, Any]:
    cfg = _bridge_required().get_config()
    active = cfg.get("meshtastic_active", {})
    conn = active.get("connection") if isinstance(active, dict) else None
    if not isinstance(conn, dict):
        conn = cfg.get("connection", {})
    return conn if isinstance(conn, dict) else {}


def _meshtastic_cli_base_args() -> list[str]:
    cli = "/opt/meshtak/venv/bin/meshtastic"
    if not Path(cli).exists():
        cli = "meshtastic"
    conn = _radio_connection()
    args = [cli]
    connection_type = str(conn.get("type", "serial")).strip().lower()
    if connection_type in {"tcp", "ip", "wifi"} and conn.get("host"):
        args.extend(["--host", str(conn.get("host"))])
    else:
        port = conn.get("serial_port") or conn.get("port") or "/dev/ttyACM0"
        args.extend(["--port", str(port)])
    return args


def _run_meshtastic_cli(extra_args: list[str], timeout: int = 60) -> dict[str, Any]:
    command = _meshtastic_cli_base_args() + extra_args
    try:
        completed = subprocess.run(command, text=True, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail="meshtastic CLI was not found in the MeshTAK venv or PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail="meshtastic CLI timed out while applying radio configuration") from exc

    return {
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "command": " ".join(shlex.quote(part) for part in command),
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


def _close_bridge_radio_interface(b: MeshTakBridge) -> None:
    if b.interface:
        try:
            b.interface.close()
        except Exception:
            pass
    b.interface = None
    b.connected = False
    _runtime_state["radio"]["status"] = "disconnected"


def _reconnect_bridge_radio_interface(b: MeshTakBridge) -> None:
    b.reload_config()
    if not b._active_enabled():
        return
    b.start_interfaces()
    b._refresh_known_nodes()
    _runtime_state["radio"]["status"] = "connected"


def _run_meshtastic_cli_with_bridge_release(extra_args: list[str], timeout: int = 60) -> dict[str, Any]:
    b = bridge
    conn = _radio_connection()
    is_serial = str(conn.get("type", "serial")).strip().lower() == "serial"
    released_live_radio = False
    reconnect_error = None

    if b is not None and is_serial and b.interface is not None:
        _runtime_state["radio"]["status"] = "reconfiguring"
        _close_bridge_radio_interface(b)
        released_live_radio = True
        time.sleep(0.5)

    try:
        result = _run_meshtastic_cli(extra_args, timeout=timeout)
    finally:
        if released_live_radio and b is not None:
            try:
                time.sleep(0.5)
                _reconnect_bridge_radio_interface(b)
            except Exception as exc:
                reconnect_error = str(exc)

    if reconnect_error:
        result["reconnect_error"] = reconnect_error
        _runtime_state["radio"]["status"] = "error"
    return result


def _radio_config_sync_view() -> dict[str, Any]:
    state = _runtime_state.get("radio_config", {})
    status = str(state.get("status") or "idle")
    return {
        "status": status,
        "pending": status in {"requested", "syncing"},
        "authoritative": status == "ready" and bool(state.get("form_values")),
        "requested_at": state.get("requested_at"),
        "updated_at": state.get("updated_at"),
        "message": state.get("message") or "",
        "last_error": state.get("last_error"),
    }


def _set_radio_config_state(**updates: Any) -> None:
    with _radio_config_lock:
        state = _runtime_state.setdefault("radio_config", {})
        state.update(updates)


def _radio_config_export_worker() -> None:
    try:
        _set_radio_config_state(
            status="syncing",
            message="Radio config export requested. Waiting for the connected radio to return its current configuration. Dashboard values may be incomplete until this finishes.",
            last_error=None,
        )
        result = _run_meshtastic_cli_with_bridge_release(["--export-config"], timeout=120)
        if not result.get("ok"):
            error_message = result.get("stderr") or result.get("stdout") or "Radio export command failed"
            _set_radio_config_state(
                status="error",
                message=f"Radio config export failed: {error_message}",
                last_error=error_message,
            )
            return

        import yaml

        exported_text = str(result.get("stdout") or "").strip()
        exported_payload = yaml.safe_load(exported_text) if exported_text else {}
        if not isinstance(exported_payload, dict):
            exported_payload = {}
        form_values = _flatten_radio_form_values(exported_payload)
        if not form_values:
            _set_radio_config_state(
                status="error",
                message="Radio config export completed but returned no editable values yet. Try refresh again once the radio finishes reporting.",
                last_error="empty export",
            )
            return

        _set_radio_config_state(
            status="ready",
            updated_at=int(time.time()),
            message="Live radio config export received. Fields now reflect the connected radio.",
            last_error=None,
            form_values=form_values,
            yaml_text=exported_text,
        )
    except Exception as exc:
        _set_radio_config_state(
            status="error",
            message=f"Radio config export failed: {exc}",
            last_error=str(exc),
        )
    finally:
        global _radio_config_thread
        with _radio_config_lock:
            _radio_config_thread = None


def _request_radio_config_export(force: bool = False) -> None:
    global _radio_config_thread

    with _radio_config_lock:
        state = _runtime_state.setdefault("radio_config", {})
        status = str(state.get("status") or "idle")
        if _radio_config_thread is not None and _radio_config_thread.is_alive():
            return
        if not force and status == "ready" and state.get("form_values"):
            return
        state["status"] = "requested"
        state["requested_at"] = int(time.time())
        state["message"] = "Live radio config export requested. Waiting on the connected radio. Dashboard values may be incomplete until the export returns."
        state["last_error"] = None
        if force:
            state["updated_at"] = None
            state["form_values"] = {}
            state["yaml_text"] = ""
        _radio_config_thread = threading.Thread(target=_radio_config_export_worker, daemon=True)
        _radio_config_thread.start()


def _mesh_channel_export(name: str, index: int, psk_b64: str) -> dict[str, Any]:
    payload = {"name": name, "index": index, "psk": psk_b64, "format": "meshtak-channel-v1"}
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii").rstrip("=")
    return {
        "name": name,
        "index": index,
        "psk": psk_b64,
        "meshtak_url": f"meshtak://channel/{encoded}",
        "json": payload,
        "meshtastic_cli": f"meshtastic --ch-index {index} --ch-set name {shlex.quote(name)} --ch-set psk {shlex.quote(psk_b64)}",
    }


def _parse_mesh_channel_import(payload: dict[str, Any]) -> dict[str, Any]:
    original = dict(payload)
    raw = str(payload.get("url") or payload.get("import_text") or "").strip()
    if raw.startswith("meshtak://channel/"):
        token = raw.rsplit("/", 1)[-1]
        padding = "=" * (-len(token) % 4)
        try:
            decoded = base64.urlsafe_b64decode((token + padding).encode("ascii"))
            data = json.loads(decoded.decode("utf-8"))
            if isinstance(data, dict):
                payload = {**payload, **data}
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Unable to parse MeshTAK channel import URL") from exc
    elif raw.startswith("{"):
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                payload = {**payload, **data}
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Unable to parse channel import JSON") from exc

    for key in ("name", "index", "psk"):
        if original.get(key) not in (None, ""):
            payload[key] = original[key]
    return payload


def _nested_value(data: Any, *path_groups: tuple[str, ...]) -> Any:
    for path in path_groups:
        current = data
        matched = True
        for key in path:
            if not isinstance(current, dict) or key not in current:
                matched = False
                break
            current = current[key]
        if matched and current not in (None, ""):
            return current
    return None


def _extract_radio_editable_fields(radio_snapshot: dict[str, Any]) -> dict[str, Any]:
    my_info = radio_snapshot.get("myInfo") if isinstance(radio_snapshot, dict) else {}
    local_node = radio_snapshot.get("localNode") if isinstance(radio_snapshot, dict) else {}
    local_config = {}
    if isinstance(local_node, dict):
        local_config = local_node.get("localConfig") or local_node.get("local_config") or {}
    user = _nested_value(
        my_info,
        ("my_node_info", "user"),
        ("user",),
    ) or {}

    return {
        "long_name": _nested_value(user, ("longName",), ("long_name",)),
        "short_name": _nested_value(user, ("shortName",), ("short_name",)),
        "region": _nested_value(
            local_config,
            ("lora", "region"),
        ),
        "tx_power": _nested_value(
            local_config,
            ("lora", "txPower"),
            ("lora", "tx_power"),
        ),
        "modem_preset": _nested_value(
            local_config,
            ("lora", "modemPreset"),
            ("lora", "modem_preset"),
        ),
        "position_broadcast_secs": _nested_value(
            local_config,
            ("position", "positionBroadcastSecs"),
            ("position", "position_broadcast_secs"),
        ),
    }


def _extract_live_radio_form_values(radio_snapshot: dict[str, Any]) -> dict[str, Any]:
    my_info = radio_snapshot.get("myInfo") if isinstance(radio_snapshot, dict) else {}
    local_node = radio_snapshot.get("localNode") if isinstance(radio_snapshot, dict) else {}
    if not isinstance(local_node, dict):
        local_node = {}

    user = _nested_value(
        my_info,
        ("my_node_info", "user"),
        ("user",),
    ) or local_node.get("user") or {}
    local_config = local_node.get("localConfig") or local_node.get("local_config") or {}
    module_config = local_node.get("moduleConfig") or local_node.get("module_config") or {}

    live_payload = {
        "owner": _nested_value(user, ("longName",), ("long_name",)),
        "owner_short": _nested_value(user, ("shortName",), ("short_name",)),
        "config": {
            "bluetooth": {
                "enabled": _nested_value(local_config, ("bluetooth", "enabled")),
                "fixedPin": _nested_value(local_config, ("bluetooth", "fixedPin"), ("bluetooth", "fixed_pin")),
                "mode": _nested_value(local_config, ("bluetooth", "mode")),
            },
            "device": {
                "nodeInfoBroadcastSecs": _nested_value(local_config, ("device", "nodeInfoBroadcastSecs"), ("device", "node_info_broadcast_secs")),
                "tzdef": _nested_value(local_config, ("device", "tzdef")),
            },
            "display": {
                "screenOnSecs": _nested_value(local_config, ("display", "screenOnSecs"), ("display", "screen_on_secs")),
            },
            "lora": {
                "region": _nested_value(local_config, ("lora", "region")),
                "bandwidth": _nested_value(local_config, ("lora", "bandwidth")),
                "codingRate": _nested_value(local_config, ("lora", "codingRate"), ("lora", "coding_rate")),
                "configOkToMqtt": _nested_value(local_config, ("lora", "configOkToMqtt"), ("lora", "config_ok_to_mqtt")),
                "hopLimit": _nested_value(local_config, ("lora", "hopLimit"), ("lora", "hop_limit")),
                "spreadFactor": _nested_value(local_config, ("lora", "spreadFactor"), ("lora", "spread_factor")),
                "sx126xRxBoostedGain": _nested_value(local_config, ("lora", "sx126xRxBoostedGain"), ("lora", "sx126x_rx_boosted_gain")),
                "txEnabled": _nested_value(local_config, ("lora", "txEnabled"), ("lora", "tx_enabled")),
                "txPower": _nested_value(local_config, ("lora", "txPower"), ("lora", "tx_power")),
                "usePreset": _nested_value(local_config, ("lora", "usePreset"), ("lora", "use_preset")),
            },
            "network": {
                "wifiSsid": _nested_value(local_config, ("network", "wifiSsid"), ("network", "wifi_ssid")),
            },
            "position": {
                "broadcastSmartMinimumDistance": _nested_value(local_config, ("position", "broadcastSmartMinimumDistance"), ("position", "broadcast_smart_minimum_distance")),
                "broadcastSmartMinimumIntervalSecs": _nested_value(local_config, ("position", "broadcastSmartMinimumIntervalSecs"), ("position", "broadcast_smart_minimum_interval_secs")),
                "gpsEnGpio": _nested_value(local_config, ("position", "gpsEnGpio"), ("position", "gps_en_gpio")),
                "gpsMode": _nested_value(local_config, ("position", "gpsMode"), ("position", "gps_mode")),
                "gpsUpdateInterval": _nested_value(local_config, ("position", "gpsUpdateInterval"), ("position", "gps_update_interval")),
                "positionBroadcastSecs": _nested_value(local_config, ("position", "positionBroadcastSecs"), ("position", "position_broadcast_secs")),
                "positionBroadcastSmartEnabled": _nested_value(local_config, ("position", "positionBroadcastSmartEnabled"), ("position", "position_broadcast_smart_enabled")),
                "positionFlags": _nested_value(local_config, ("position", "positionFlags"), ("position", "position_flags")),
            },
            "power": {
                "lsSecs": _nested_value(local_config, ("power", "lsSecs"), ("power", "ls_secs")),
                "minWakeSecs": _nested_value(local_config, ("power", "minWakeSecs"), ("power", "min_wake_secs")),
                "sdsSecs": _nested_value(local_config, ("power", "sdsSecs"), ("power", "sds_secs")),
                "waitBluetoothSecs": _nested_value(local_config, ("power", "waitBluetoothSecs"), ("power", "wait_bluetooth_secs")),
            },
            "security": {
                "privateKey": _nested_value(local_config, ("security", "privateKey"), ("security", "private_key")),
                "publicKey": _nested_value(local_config, ("security", "publicKey"), ("security", "public_key")),
                "serialEnabled": _nested_value(local_config, ("security", "serialEnabled"), ("security", "serial_enabled")),
            },
        },
        "module_config": {
            "ambientLighting": {
                "red": _nested_value(module_config, ("ambientLighting", "red"), ("ambient_lighting", "red")),
                "green": _nested_value(module_config, ("ambientLighting", "green"), ("ambient_lighting", "green")),
                "blue": _nested_value(module_config, ("ambientLighting", "blue"), ("ambient_lighting", "blue")),
                "current": _nested_value(module_config, ("ambientLighting", "current"), ("ambient_lighting", "current")),
            },
            "cannedMessage": {
                "allowInputSource": _nested_value(module_config, ("cannedMessage", "allowInputSource"), ("canned_message", "allow_input_source")),
                "enabled": _nested_value(module_config, ("cannedMessage", "enabled"), ("canned_message", "enabled")),
            },
            "detectionSensor": {
                "detectionTriggerType": _nested_value(module_config, ("detectionSensor", "detectionTriggerType"), ("detection_sensor", "detection_trigger_type")),
                "minimumBroadcastSecs": _nested_value(module_config, ("detectionSensor", "minimumBroadcastSecs"), ("detection_sensor", "minimum_broadcast_secs")),
            },
            "mqtt": {
                "address": _nested_value(module_config, ("mqtt", "address")),
                "username": _nested_value(module_config, ("mqtt", "username")),
                "password": _nested_value(module_config, ("mqtt", "password")),
                "root": _nested_value(module_config, ("mqtt", "root")),
                "encryptionEnabled": _nested_value(module_config, ("mqtt", "encryptionEnabled"), ("mqtt", "encryption_enabled")),
                "mapReportingEnabled": _nested_value(module_config, ("mqtt", "mapReportingEnabled"), ("mqtt", "map_reporting_enabled")),
                "proxyToClientEnabled": _nested_value(module_config, ("mqtt", "proxyToClientEnabled"), ("mqtt", "proxy_to_client_enabled")),
                "mapReportSettings": {
                    "positionPrecision": _nested_value(module_config, ("mqtt", "mapReportSettings", "positionPrecision"), ("mqtt", "map_report_settings", "position_precision")),
                    "publishIntervalSecs": _nested_value(module_config, ("mqtt", "mapReportSettings", "publishIntervalSecs"), ("mqtt", "map_report_settings", "publish_interval_secs")),
                    "shouldReportLocation": _nested_value(module_config, ("mqtt", "mapReportSettings", "shouldReportLocation"), ("mqtt", "map_report_settings", "should_report_location")),
                },
            },
            "serial": {
                "baud": _nested_value(module_config, ("serial", "baud")),
                "enabled": _nested_value(module_config, ("serial", "enabled")),
            },
            "telemetry": {
                "deviceUpdateInterval": _nested_value(module_config, ("telemetry", "deviceUpdateInterval"), ("telemetry", "device_update_interval")),
            },
        },
    }
    return _flatten_radio_form_values(live_payload)


def _extract_live_radio_channels(radio_snapshot: Any) -> list[dict[str, Any]]:
    if radio_snapshot is None:
        return []

    extracted: list[dict[str, Any]] = []
    seen_indexes: set[int] = set()

    def field_value(value: Any, *names: str) -> Any:
        for name in names:
            if isinstance(value, dict) and name in value:
                found = value.get(name)
                if found not in (None, ""):
                    return found
            if hasattr(value, name):
                try:
                    found = getattr(value, name)
                except Exception:
                    continue
                if found not in (None, ""):
                    return found
        return None

    def iter_entries(value: Any) -> list[tuple[Any, Any]]:
        if value is None:
            return []
        if isinstance(value, dict):
            return list(value.items())
        if isinstance(value, list):
            return list(enumerate(value))
        if hasattr(value, "items"):
            try:
                return list(value.items())
            except Exception:
                return []
        return []

    def append_channel(channel: Any, fallback_index: int = 0) -> None:
        if channel is None:
            return
        channel_index = field_value(channel, "index", "channelNum", "channel_num")
        try:
            channel_index = int(channel_index if channel_index is not None else fallback_index)
        except Exception:
            channel_index = fallback_index
        if channel_index in seen_indexes:
            return
        role = str(field_value(channel, "role") or "").strip()
        settings = field_value(channel, "settings", "channelSettings", "channel_settings")
        name = field_value(settings, "name") or field_value(channel, "name")
        if not name:
            name = "Broadcast" if channel_index == 0 else f"Channel {channel_index}"
        extracted.append(
            {
                "name": str(name),
                "index": channel_index,
                "pinned": channel_index == 0 or role.upper() == "PRIMARY",
                "source": "radio",
            }
        )
        seen_indexes.add(channel_index)

    local_node = field_value(radio_snapshot, "localNode", "local_node")
    local_channels = field_value(local_node, "channels")
    for key, channel in iter_entries(local_channels):
        try:
            fallback_index = int(key)
        except Exception:
            fallback_index = len(extracted)
        append_channel(channel, fallback_index)

    raw_channels = field_value(radio_snapshot, "channels")
    if isinstance(raw_channels, list):
        for idx, channel in enumerate(raw_channels):
            append_channel(channel, idx)
    else:
        for key, channel in iter_entries(raw_channels):
            try:
                fallback_index = int(key)
            except Exception:
                fallback_index = len(extracted)
            append_channel(channel, fallback_index)

    return extracted


def _merge_channel_views(*channel_sets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    by_index: dict[int, dict[str, Any]] = {}
    by_name: dict[str, dict[str, Any]] = {}

    for channel_list in channel_sets:
        if not isinstance(channel_list, list):
            continue
        for channel in channel_list:
            if not isinstance(channel, dict):
                continue
            name = str(channel.get("name") or "").strip()
            try:
                index = int(channel.get("index", -1))
            except Exception:
                index = -1
            existing = by_index.get(index) if index >= 0 else None
            if existing is None and name:
                existing = by_name.get(name)
            if existing is None:
                existing = {}
                merged.append(existing)
            existing.update({k: v for k, v in channel.items() if v not in (None, "")})
            if index >= 0:
                by_index[index] = existing
            if name:
                by_name[name] = existing

    return _sorted_channels(merged)


def _sorted_channels(channels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        channels,
        key=lambda channel: (
            0 if bool(channel.get("pinned")) else 1,
            int(channel.get("index", 999)) if str(channel.get("index", "")).strip() else 999,
            str(channel.get("name") or ""),
        ),
    )


def _flatten_radio_form_values(config_payload: dict[str, Any]) -> dict[str, Any]:
    form_values: dict[str, Any] = {}

    def add(path: str, value: Any) -> None:
        if value is not None:
            form_values[path] = value

    if not isinstance(config_payload, dict):
        return form_values

    def walk(node: Any, prefix: str = "") -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                next_prefix = f"{prefix}.{key}" if prefix else str(key)
                walk(value, next_prefix)
        else:
            add(prefix, node)

    walk(config_payload)
    return form_values


def _merge_form_values(*sources: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key, value in source.items():
            if value not in (None, ""):
                merged[key] = value
    return merged


def _build_radio_configure_payload(payload: dict[str, Any]) -> dict[str, Any]:
    direct_payload = payload.get("config_payload")
    if isinstance(direct_payload, dict) and direct_payload:
        return direct_payload

    config_payload: dict[str, Any] = {}

    long_name = str(payload.get("long_name") or payload.get("owner") or "").strip()
    short_name = str(payload.get("short_name") or "").strip()
    if long_name:
        config_payload["owner"] = long_name
    if short_name:
        config_payload["owner_short"] = short_name[:4]

    config_section: dict[str, Any] = {}
    lora_section: dict[str, Any] = {}
    position_section: dict[str, Any] = {}

    region = payload.get("region")
    if region not in (None, ""):
        lora_section["region"] = str(region)

    tx_power = payload.get("tx_power")
    if tx_power not in (None, ""):
        lora_section["tx_power"] = int(tx_power)

    modem_preset = payload.get("modem_preset")
    if modem_preset not in (None, ""):
        lora_section["modem_preset"] = str(modem_preset)

    position_broadcast_secs = payload.get("position_broadcast_secs")
    if position_broadcast_secs not in (None, ""):
        position_section["position_broadcast_secs"] = int(position_broadcast_secs)

    if lora_section:
        config_section["lora"] = lora_section
    if position_section:
        config_section["position"] = position_section
    if config_section:
        config_payload["config"] = config_section

    return config_payload


def _run_meshtastic_configure(config_payload: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
    import yaml

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".yaml", delete=False) as handle:
        yaml.safe_dump(config_payload, handle, sort_keys=False)
        temp_path = handle.name

    try:
        return _run_meshtastic_cli_with_bridge_release(["--configure", temp_path], timeout=timeout)
    finally:
        try:
            Path(temp_path).unlink(missing_ok=True)
        except Exception:
            pass


def create_app(config: AppConfig | None = None) -> FastAPI:
    if config is None:
        config = load_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        global pipeline, upstream, bridge

        validate_activation(config)

        identity = DeviceIdentity(
            device_id=_stable_device_id(config.device.device_id),
            device_name=config.device.device_name,
            latitude=config.device.latitude,
            longitude=config.device.longitude,
            altitude=config.device.altitude,
            hardware_description=config.device.hardware_description,
            firmware_version=config.device.firmware_version,
        )

        bridge = MeshTakBridge()
        bridge.start()
        _runtime_state["radio"]["status"] = "connected" if bridge.is_connected() else "disconnected"

        pipeline = _build_pipeline(config)
        pipeline.on_packet(_on_packet_received)
        pipeline.on_packet(lambda pkt: print_packet(pkt))
        await pipeline.start()
        _runtime_state["collector"]["status"] = "running"

        upstream = UpstreamClient(config.upstream, identity)
        pipeline.on_packet(upstream.send_packet)
        await upstream.start()

        _init_routes(pipeline, config, identity)

        print_banner(config)
        logger.info("MeshTAK started -- listening for packets")
        yield

        try:
            await upstream.stop()
        except Exception:
            logger.exception("Error stopping upstream")

        try:
            await pipeline.stop()
        except Exception:
            logger.exception("Error stopping pipeline")

        _runtime_state["collector"]["status"] = "stopped"

        try:
            if bridge is not None:
                bridge.stop()
        except Exception:
            logger.exception("Error stopping MeshTAK bridge")

        _runtime_state["radio"]["status"] = "disconnected"
        logger.info("MeshTAK stopped")

    app = FastAPI(
        title="MeshTAK Collector",
        version="1.2.0",
        lifespan=lifespan,
    )

    app.include_router(nodes.router)
    app.include_router(packets.router)
    app.include_router(analytics.router)
    app.include_router(device.router)
    app.include_router(system_metrics.router)
    app.include_router(telemetry.router)
    app.include_router(update_check.router)

    @app.get("/api/settings")
    def get_settings():
        return _build_settings_payload()

    @app.get("/api/auth/status")
    def auth_status(request: Request):
        user = _current_auth_user(request)
        if not user:
            return {"authenticated": False, "role": "", "username": "", "show_update_notices": False}
        return {
            "authenticated": True,
            "role": user.get("role", "user"),
            "username": user.get("username", ""),
            "show_update_notices": user.get("role") == "admin",
        }

    @app.post("/api/auth/login")
    def auth_login(payload: LoginRequest, response: Response):
        users = _ensure_user_store().get("users", {})
        username = payload.username.strip()
        user = users.get(username)
        if not user or not _verify_password(payload.password, str(user.get("password_hash", ""))):
            raise HTTPException(status_code=401, detail="Invalid username or password")

        token = secrets.token_urlsafe(32)
        _auth_sessions[token] = {
            "username": username,
            "role": user.get("role", "user"),
            "expires_at": int(time.time()) + AUTH_SESSION_TTL,
        }
        response.set_cookie(
            AUTH_COOKIE_NAME,
            token,
            httponly=True,
            samesite="lax",
            max_age=AUTH_SESSION_TTL,
        )
        return {
            "ok": True,
            "authenticated": True,
            "username": username,
            "role": user.get("role", "user"),
            "show_update_notices": user.get("role") == "admin",
        }

    @app.post("/api/auth/logout")
    def auth_logout(request: Request, response: Response):
        token = request.cookies.get(AUTH_COOKIE_NAME)
        if token:
            _auth_sessions.pop(token, None)
        response.delete_cookie(AUTH_COOKIE_NAME)
        return {"ok": True, "authenticated": False, "role": "", "username": "", "show_update_notices": False}

    @app.get("/api/auth/users")
    def list_users(request: Request):
        _require_admin(request)
        users = _ensure_user_store().get("users", {})
        return {
            "users": [
                _public_user_record(username, user)
                for username, user in sorted(users.items(), key=lambda item: item[0].lower())
            ]
        }

    @app.post("/api/auth/users")
    def create_user(payload: UserAccountRequest, request: Request):
        _require_admin(request)
        username = payload.username.strip()
        password = payload.password
        role = payload.role.strip().lower()

        if not username:
            raise HTTPException(status_code=400, detail="Username is required")
        if not password:
            raise HTTPException(status_code=400, detail="Password is required")
        if role not in {"admin", "user"}:
            raise HTTPException(status_code=400, detail="Role must be admin or user")

        data = _ensure_user_store()
        users = data.setdefault("users", {})
        if username in users:
            raise HTTPException(status_code=409, detail="User already exists")

        users[username] = {
            "username": username,
            "role": role,
            "password_hash": _hash_password(password),
            "created_at": int(time.time()),
        }
        _save_user_store(data)
        return {"ok": True, "user": _public_user_record(username, users[username])}

    @app.delete("/api/auth/users/{username}")
    def delete_user(username: str, request: Request):
        current_user = _require_admin(request)
        data = _ensure_user_store()
        users = data.setdefault("users", {})

        if username not in users:
            raise HTTPException(status_code=404, detail="User not found")
        if username == current_user.get("username"):
            raise HTTPException(status_code=400, detail="You cannot delete your active admin account")

        user = users.get(username, {})
        if user.get("role") == "admin":
            admin_count = sum(1 for account in users.values() if account.get("role") == "admin")
            if admin_count <= 1:
                raise HTTPException(status_code=400, detail="Cannot delete the last admin account")

        users.pop(username, None)
        for token, session in list(_auth_sessions.items()):
            if session.get("username") == username:
                _auth_sessions.pop(token, None)
        _save_user_store(data)
        return {"ok": True, "deleted": username}

    @app.post("/api/settings/tak")
    def save_tak(payload: dict[str, Any] = Body(...)):
        b = _bridge_required()
        cfg = b.get_config()

        tak = cfg.setdefault("tak", {})
        tak["enabled"] = bool(payload.get("enabled", False))
        tak["host"] = str(payload.get("host", "")).strip()
        tak["port"] = _normalize_port(payload.get("port", 8088), 8088)

        protocol = str(payload.get("protocol", "udp")).strip().lower()
        tak["protocol"] = protocol if protocol in {"tcp", "udp"} else "udp"
        tak["tls"] = bool(payload.get("tls", False))

        with b._config_lock:
            b.config = cfg
            b.store.nodes_store._ensure_parent_dir()
            from src.integrations.meshtak_bridge import CONFIG_PATH
            import json
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
                f.write("\n")

        return {"ok": True, "tak": tak}

    @app.post("/api/settings/collector")
    def save_collector(payload: dict[str, Any] = Body(...)):
        b = _bridge_required()
        cfg = b.get_config()

        collector = cfg.setdefault("collector", {})
        spi_device = str(payload.get("spi_device", collector.get("spi_device", "/dev/spidev0.0"))).strip()
        allowed = {"/dev/spidev0.0", "/dev/spidev0.1", "/dev/spidev1.0", "/dev/spidev1.1"}
        collector["spi_device"] = spi_device if spi_device in allowed else "/dev/spidev0.0"

        from src.integrations.meshtak_bridge import CONFIG_PATH
        import json
        with b._config_lock:
            b.config = cfg
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
                f.write("\n")

        return {"ok": True, "collector": collector}

    @app.post("/api/settings/tak/test")
    def test_tak():
        cfg = _bridge_required().get_config()
        tak = cfg.get("tak", {})

        if not tak.get("enabled", False):
            raise HTTPException(status_code=400, detail="TAK disabled")
        if not tak.get("host"):
            raise HTTPException(status_code=400, detail="TAK host missing")

        return {
            "ok": True,
            "message": f"{tak.get('protocol', 'udp').upper()} {tak.get('host')}:{tak.get('port', 8088)}",
        }

    @app.post("/api/control/collector/start")
    def collector_start():
        _runtime_state["collector"]["status"] = "running"
        return {"ok": True, "status": "running"}

    @app.post("/api/control/collector/stop")
    def collector_stop():
        _runtime_state["collector"]["status"] = "stopped"
        return {"ok": True, "status": "stopped"}

    @app.post("/api/control/collector/reconnect")
    def collector_reconnect():
        _runtime_state["collector"]["status"] = "running"
        return {"ok": True, "status": "running"}

    @app.post("/api/control/radio/connect")
    def radio_connect(payload: dict[str, Any] = Body(...)):
        b = _bridge_required()
        cfg = b.get_config()

        active = cfg.setdefault("meshtastic_active", {})
        conn = active.setdefault("connection", cfg.setdefault("connection", {}))

        active["enabled"] = bool(payload.get("enabled", True))

        conn_type = str(payload.get("type", "serial")).strip().lower()
        conn["type"] = conn_type if conn_type in {"serial", "tcp"} else "serial"
        conn["serial_port"] = str(payload.get("serial_port", "")).strip()
        conn["host"] = str(payload.get("host", "")).strip()
        conn["port"] = _normalize_port(payload.get("port", 4403), 4403)

        from src.integrations.meshtak_bridge import CONFIG_PATH
        import json
        with b._config_lock:
            b.config = cfg
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
                f.write("\n")

        try:
            if b.interface:
                try:
                    b.interface.close()
                except Exception:
                    pass
                b.interface = None
                b.connected = False

            b.reload_config()
            b.start_interfaces()
            b._refresh_known_nodes()
            _runtime_state["radio"]["status"] = "connected"
            return {"ok": True, "status": "connected"}
        except Exception as exc:
            logger.exception("Radio connect failed")
            _runtime_state["radio"]["status"] = "error"
            raise HTTPException(status_code=500, detail=str(exc))

    @app.post("/api/control/radio/disconnect")
    def radio_disconnect():
        b = _bridge_required()
        try:
            if b.interface:
                try:
                    b.interface.close()
                except Exception:
                    pass
            b.interface = None
            b.connected = False
            _runtime_state["radio"]["status"] = "disconnected"
            return {"ok": True, "status": "disconnected"}
        except Exception as exc:
            logger.exception("Radio disconnect failed")
            raise HTTPException(status_code=500, detail=str(exc))

    @app.post("/api/control/radio/reconnect")
    def radio_reconnect():
        b = _bridge_required()
        try:
            if b.interface:
                try:
                    b.interface.close()
                except Exception:
                    pass
                b.interface = None
                b.connected = False

            b.reload_config()
            if not b._active_enabled():
                raise HTTPException(status_code=400, detail="Active radio disabled in config")

            b.start_interfaces()
            b._refresh_known_nodes()
            _runtime_state["radio"]["status"] = "connected"
            return {"ok": True, "status": "connected"}
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("Radio reconnect failed")
            _runtime_state["radio"]["status"] = "error"
            raise HTTPException(status_code=500, detail=str(exc))

    @app.get("/api/radio/status")
    def radio_status():
        b = _bridge_required()
        return {
            "connected": b.is_connected(),
            "status": _runtime_state["radio"].get("status", "unknown"),
        }

    @app.get("/api/radio/nodes")
    def radio_nodes():
        b = _bridge_required()
        nodes = b.store.get_nodes()
        return {"nodes": nodes}

    @app.get("/api/radio/channels")
    def radio_channels():
        b = _bridge_required()
        cfg = b.get_config()
        channels = list(cfg.get("channels", []) or [])
        live_channels: list[dict[str, Any]] = []
        interface = b.interface
        if interface is not None:
            try:
                live_channels = _extract_live_radio_channels(
                    {
                        "channels": getattr(interface, "channels", None),
                        "localNode": getattr(interface, "localNode", None),
                    }
                )
            except Exception:
                try:
                    live_channels = _extract_live_radio_channels(
                        {
                            "channels": _to_jsonable(getattr(interface, "channels", None)),
                            "localNode": _to_jsonable(getattr(interface, "localNode", None)),
                        }
                    )
                except Exception:
                    live_channels = []
        runtime_cfg = _read_runtime_yaml_config()
        stored_keys = ((runtime_cfg.get("meshtastic") or {}).get("channel_keys") or {})
        known_names = {str(channel.get("name") or "").strip() for channel in channels}
        if isinstance(stored_keys, dict):
            next_index = max(
                [int(channel.get("index", 0)) for channel in channels if isinstance(channel, dict)] or [0]
            )
            for name in stored_keys.keys():
                safe_name = str(name or "").strip()
                if not safe_name or safe_name in known_names:
                    continue
                next_index += 1
                channels.append({"name": safe_name, "index": next_index, "pinned": False, "stored_only": True})
                known_names.add(safe_name)
        channels = _merge_channel_views(live_channels, channels)
        if not channels:
            channels = [{"name": "Broadcast", "index": 0, "pinned": True}]
        return {"channels": channels}

    @app.get("/api/radio/config")
    def radio_config(request: Request, force: bool = False):
        _require_admin(request)
        b = _bridge_required()
        conn = _radio_connection()
        cfg = b.get_config()
        runtime_cfg = _read_runtime_yaml_config()
        template_cfg, template_text = _read_radio_template_config()
        stored_keys = ((runtime_cfg.get("meshtastic") or {}).get("channel_keys") or {})
        interface = b.interface
        radio_snapshot: dict[str, Any] = {}
        if interface is not None:
            for attr in ("myInfo", "localNode", "nodes", "channels"):
                try:
                    radio_snapshot[attr] = _to_jsonable(getattr(interface, attr, None))
                except Exception:
                    radio_snapshot[attr] = None
        editable = _extract_radio_editable_fields(radio_snapshot)
        config_sync = _radio_config_sync_view()
        if b.is_connected() and (force or not config_sync.get("authoritative")):
            _request_radio_config_export(force=force)
            config_sync = _radio_config_sync_view()
        form_values = {}
        radio_config_state = _runtime_state.get("radio_config", {})
        if config_sync.get("authoritative"):
            form_values = dict(radio_config_state.get("form_values") or {})
        merged_channels = _merge_channel_views(
            _extract_live_radio_channels(radio_snapshot),
            cfg.get("channels", []) or [],
        )

        return {
            "connected": b.is_connected(),
            "status": _runtime_state["radio"].get("status", "unknown"),
            "connection": conn,
            "channels": merged_channels or [{"name": "Broadcast", "index": 0, "pinned": True}],
            "stored_channel_keys": {
                str(name): {"name": str(name), "psk": str(psk), "masked": f"{str(psk)[:4]}...{str(psk)[-4:]}"}
                for name, psk in stored_keys.items()
            } if isinstance(stored_keys, dict) else {},
            "radio": radio_snapshot,
            "editable": editable,
            "template_config": template_cfg,
            "template_text": template_text,
            "form_values": form_values,
            "config_sync": config_sync,
            "exported_yaml": radio_config_state.get("yaml_text") or "",
        }

    @app.post("/api/radio/config/apply")
    def radio_config_apply(request: Request, payload: dict[str, Any] = Body(...)):
        _require_admin(request)
        config_payload = _build_radio_configure_payload(payload)
        if not config_payload:
            raise HTTPException(status_code=400, detail="No radio configuration changes were provided")

        if bool(payload.get("dry_run", False)):
            return {
                "ok": True,
                "dry_run": True,
                "command": " ".join(shlex.quote(part) for part in (_meshtastic_cli_base_args() + ["--configure", "<generated-config>.yaml"])),
                "config_payload": config_payload,
            }

        result = _run_meshtastic_configure(config_payload, timeout=120)
        if not result["ok"]:
            raise HTTPException(status_code=500, detail=result.get("stderr") or result.get("stdout") or "Radio CLI command failed")

        return {"ok": True, "command": result, "config_payload": config_payload}

    @app.post("/api/radio/config/cli")
    def radio_config_cli(request: Request, payload: dict[str, Any] = Body(...)):
        _require_admin(request)
        raw_args = str(payload.get("args") or "").strip()
        if not raw_args:
            raise HTTPException(status_code=400, detail="Meshtastic CLI arguments are required")
        try:
            args = shlex.split(raw_args)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Unable to parse CLI arguments: {exc}") from exc

        blocked = {"meshtastic", "sudo", "python", "python3", "bash", "sh"}
        if args and Path(args[0]).name in blocked:
            raise HTTPException(status_code=400, detail="Enter only Meshtastic arguments, not the command name")

        if bool(payload.get("dry_run", False)):
            command = _meshtastic_cli_base_args() + args
            return {
                "ok": True,
                "dry_run": True,
                "command": " ".join(shlex.quote(part) for part in command),
            }

        result = _run_meshtastic_cli_with_bridge_release(args, timeout=120)
        if not result["ok"]:
            raise HTTPException(status_code=500, detail=result.get("stderr") or result.get("stdout") or "Radio CLI command failed")
        return {"ok": True, "command": result}

    @app.post("/api/radio/channels/save-key")
    def radio_channel_save_key(request: Request, payload: dict[str, Any] = Body(...)):
        _require_admin(request)
        name = _safe_channel_name(payload.get("name"))
        index = _normalize_channel_index(payload.get("index"), 1)
        psk_b64 = _normalize_psk_b64(payload.get("psk"))
        _save_channel_key(name, psk_b64)
        channels = _upsert_bridge_channel(name, index, psk_b64, bool(payload.get("pinned", False)))
        return {"ok": True, "channels": channels, "export": _mesh_channel_export(name, index, psk_b64)}

    @app.post("/api/radio/channels/create-private")
    def radio_channel_create_private(request: Request, payload: dict[str, Any] = Body(...)):
        _require_admin(request)
        name = _safe_channel_name(payload.get("name"))
        index = _normalize_channel_index(payload.get("index"), 1)
        psk_b64 = _normalize_psk_b64(payload.get("psk") or _generate_psk_b64())
        _save_channel_key(name, psk_b64)
        channels = _upsert_bridge_channel(name, index, psk_b64, bool(payload.get("pinned", False)))

        command_result = None
        if bool(payload.get("apply_to_radio", False)):
            command_result = _run_meshtastic_cli_with_bridge_release(["--ch-index", str(index), "--ch-set", "name", name, "--ch-set", "psk", f"base64:{psk_b64}"], timeout=90)
            if not command_result["ok"]:
                raise HTTPException(status_code=500, detail=command_result.get("stderr") or command_result.get("stdout") or "Radio channel command failed")

        return {
            "ok": True,
            "channels": channels,
            "command": command_result,
            "export": _mesh_channel_export(name, index, psk_b64),
        }

    @app.post("/api/radio/channels/import")
    def radio_channel_import(request: Request, payload: dict[str, Any] = Body(...)):
        _require_admin(request)
        payload = _parse_mesh_channel_import(payload)
        name = _safe_channel_name(payload.get("name"))
        index = _normalize_channel_index(payload.get("index"), 1)
        psk_b64 = _normalize_psk_b64(payload.get("psk"))
        _save_channel_key(name, psk_b64)
        channels = _upsert_bridge_channel(name, index, psk_b64, bool(payload.get("pinned", False)))

        command_result = None
        if bool(payload.get("apply_to_radio", False)):
            command_result = _run_meshtastic_cli_with_bridge_release(["--ch-index", str(index), "--ch-set", "name", name, "--ch-set", "psk", f"base64:{psk_b64}"], timeout=90)
            if not command_result["ok"]:
                raise HTTPException(status_code=500, detail=command_result.get("stderr") or command_result.get("stdout") or "Radio channel command failed")

        return {
            "ok": True,
            "channels": channels,
            "command": command_result,
            "export": _mesh_channel_export(name, index, psk_b64),
        }

    @app.get("/api/messages")
    def get_messages(
        limit: int = 500,
        node_id: str | None = None,
        channel: str | None = None,
        direction: str | None = None,
        query: str | None = None,
    ):
        b = _bridge_required()
        return {
            "messages": b.store.get_messages(
                limit=limit,
                node_id=node_id,
                channel=channel,
                direction=direction,
                query=query,
            )
        }

    @app.post("/api/messages/purge")
    def purge_messages(payload: PurgeMessagesRequest):
        b = _bridge_required()
        deleted = b.store.clear_messages(
            node_id=payload.node_id,
            channel=payload.channel,
            direction=payload.direction,
            query=payload.query,
        )
        remaining = len(b.store.get_messages(limit=5000))
        return {"ok": True, "deleted": deleted, "remaining": remaining}

    @app.post("/api/messages/send")
    def send_message(payload: dict[str, Any] = Body(...)):
        b = _bridge_required()

        text = str(payload.get("text", "")).strip()
        to = payload.get("to")
        channel_index = payload.get("channel_index")
        channel_name = payload.get("channel_name")

        if not text:
            raise HTTPException(status_code=400, detail="Message text is required")

        try:
            b.queue_tx(
                text=text,
                to=to,
                channel_index=channel_index,
                channel_name=channel_name,
            )
            return {"ok": True}
        except Exception as exc:
            logger.exception("Send message failed")
            raise HTTPException(status_code=500, detail=str(exc))

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        await ws_manager.connect(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            await ws_manager.disconnect(websocket)

    static_dir = Path(config.dashboard.static_dir)
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True))

    return app


def _build_pipeline(config: AppConfig) -> PipelineCoordinator:
    coordinator = PipelineCoordinator(config)

    capture_sources = list(config.capture.sources or [])

    for source_name in capture_sources:
        if source_name == "serial":
            _add_serial_source(coordinator, config)
        elif source_name == "concentrator":
            _add_concentrator_source(coordinator, config)
        elif source_name == "meshcore_usb":
            _add_meshcore_usb_source(coordinator, config)

    if (
        "meshcore_usb" not in capture_sources
        and config.capture.meshcore_usb.auto_detect
    ):
        _add_meshcore_usb_source(coordinator, config)

    return coordinator


def _add_serial_source(coordinator: PipelineCoordinator, config: AppConfig):
    try:
        from src.capture.serial_source import SerialCaptureSource

        coordinator.capture_coordinator.add_source(
            SerialCaptureSource(
                port=config.capture.serial_port,
                baud=config.capture.serial_baud,
            )
        )
    except ImportError:
        logger.warning("Serial capture unavailable")


def _add_concentrator_source(coordinator: PipelineCoordinator, config: AppConfig):
    try:
        from src.capture.concentrator_source import ConcentratorCaptureSource

        coordinator.capture_coordinator.add_source(
            ConcentratorCaptureSource(
                spi_path=config.capture.concentrator_spi_device,
                syncword=config.radio.sync_word,
                radio_config=config.radio,
            )
        )
    except Exception:
        logger.exception("Concentrator source unavailable")


def _add_meshcore_usb_source(coordinator: PipelineCoordinator, config: AppConfig):
    try:
        from src.capture.meshcore_usb_source import MeshcoreUsbCaptureSource

        usb_cfg = config.capture.meshcore_usb
        coordinator.capture_coordinator.add_source(
            MeshcoreUsbCaptureSource(
                serial_port=usb_cfg.serial_port,
                baud_rate=usb_cfg.baud_rate,
                auto_detect=usb_cfg.auto_detect,
            )
        )
    except ImportError:
        logger.warning(
            "MeshCore USB unavailable -- meshcore package not installed"
        )


def _init_routes(
    coord: PipelineCoordinator,
    config: AppConfig,
    identity: DeviceIdentity,
) -> None:
    network_mapper = NetworkMapper(coord.node_repo)
    signal_analyzer = SignalAnalyzer(coord.packet_repo)
    traffic_monitor = TrafficMonitor(coord.packet_repo)

    nodes.init_routes(coord.node_repo, network_mapper, bridge)
    packets.init_routes(coord.packet_repo)
    analytics.init_routes(signal_analyzer, traffic_monitor, coord.packet_repo)
    device.init_routes(identity, ws_manager, coord.relay_manager)
    telemetry.init_routes(coord.telemetry_repo)


def _on_packet_received(packet: Packet) -> None:
    try:
        if bridge is not None:
            bridge.ingest_passive_packet(packet)
    except Exception:
        logger.exception("Failed to ingest passive packet into MeshTAK bridge")

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(ws_manager.broadcast("packet", packet.to_dict()))
    except RuntimeError:
        pass
