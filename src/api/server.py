from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
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
    "collector": {"status": "unknown"},
    "radio": {"status": "unknown"},
}


class PurgeMessagesRequest(BaseModel):
    node_id: str | None = None
    channel: str | None = None
    direction: str | None = None
    query: str | None = None


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
        channels = cfg.get("channels", []) or []
        if not channels:
            channels = [{"name": "Broadcast", "index": 0, "pinned": True}]
        return {"channels": channels}

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

    for source_name in config.capture.sources:
        if source_name == "serial":
            _add_serial_source(coordinator, config)
        elif source_name == "concentrator":
            _add_concentrator_source(coordinator, config)
        elif source_name == "meshcore_usb":
            _add_meshcore_usb_source(coordinator, config)

    if (
        "meshcore_usb" not in config.capture.sources
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
