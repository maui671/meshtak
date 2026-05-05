from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Optional

from fastapi import APIRouter

from src.api.websocket_manager import WebSocketManager
from src.config import AppConfig
from src.models.device_identity import DeviceIdentity
from src.relay.relay_manager import RelayManager
from src.version import __version__

router = APIRouter(prefix="/api/device", tags=["device"])

_identity: DeviceIdentity | None = None
_ws_manager: WebSocketManager | None = None
_relay_manager: RelayManager | None = None
_config: AppConfig | None = None
_mqtt_provider: Optional[Callable[[], Any]] = None
_tak_provider: Optional[Callable[[], Any]] = None
_upstream_provider: Optional[Callable[[], Any]] = None
_start_time: datetime = datetime.now(timezone.utc)


def init_routes(
    identity: DeviceIdentity,
    ws_manager: WebSocketManager,
    relay_manager: RelayManager,
    config: AppConfig | None = None,
    mqtt_provider: Optional[Callable[[], Any]] = None,
    tak_provider: Optional[Callable[[], Any]] = None,
    upstream_provider: Optional[Callable[[], Any]] = None,
) -> None:
    global _identity, _ws_manager, _relay_manager, _config
    global _mqtt_provider, _tak_provider, _upstream_provider, _start_time
    _identity = identity
    _ws_manager = ws_manager
    _relay_manager = relay_manager
    _config = config
    _mqtt_provider = mqtt_provider
    _tak_provider = tak_provider
    _upstream_provider = upstream_provider
    _start_time = datetime.now(timezone.utc)


@router.get("")
async def device_info():
    return _identity.to_dict()


@router.get("/status")
async def device_status():
    uptime = (datetime.now(timezone.utc) - _start_time).total_seconds()
    relay_stats = _relay_manager.get_stats() if _relay_manager else {}
    mqtt = _mqtt_provider() if _mqtt_provider else None
    tak = _tak_provider() if _tak_provider else None
    upstream = _upstream_provider() if _upstream_provider else None
    mqtt_cfg = _config.mqtt if _config else None
    tak_cfg = _config.tak if _config else None
    upstream_cfg = _config.upstream if _config else None

    return {
        "status": "running",
        "uptime_seconds": int(uptime),
        "websocket_clients": _ws_manager.client_count,
        "device_id": _identity.device_id,
        "firmware_version": __version__,
        "relay": relay_stats,
        "mqtt": {
            "enabled": bool(mqtt_cfg and mqtt_cfg.enabled),
            "connected": bool(mqtt and mqtt.connected),
            "broker": mqtt.broker if mqtt else (mqtt_cfg.broker if mqtt_cfg else ""),
            "port": mqtt.port if mqtt else (mqtt_cfg.port if mqtt_cfg else 0),
            "auth_mode": mqtt.auth_mode if mqtt else (mqtt_cfg.auth_mode if mqtt_cfg else ""),
            "publish_count": mqtt.publish_count if mqtt else 0,
            "last_error": mqtt.last_error if mqtt else "",
            "last_published_topic": mqtt.last_published_topic if mqtt else "",
            "last_published_at": mqtt.last_published_at if mqtt else None,
        },
        "tak": {
            "enabled": bool(tak_cfg and tak_cfg.enabled),
            "connected": bool(tak and tak.enabled),
            "target": tak.last_target if tak and tak.last_target else (
                f"{tak_cfg.protocol.lower()}://{tak_cfg.host}:{tak_cfg.port}"
                if tak_cfg and tak_cfg.enabled else ""
            ),
            "publish_count": tak.publish_count if tak else 0,
            "last_error": tak.last_error if tak else "",
            "last_sent_at": tak.last_sent_at if tak else None,
            "role": tak_cfg.role if tak_cfg else "",
            "team": tak_cfg.team if tak_cfg else "",
        },
        "upstream": {
            "enabled": bool(upstream_cfg and upstream_cfg.enabled),
            "connected": bool(upstream and upstream.is_connected),
            "url": upstream_cfg.url if upstream_cfg else "",
        },
    }
