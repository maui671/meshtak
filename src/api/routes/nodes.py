
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.analytics.network_mapper import NetworkMapper
from src.storage.node_repository import NodeRepository

router = APIRouter(prefix="/api/nodes", tags=["nodes"])

_node_repo: NodeRepository | None = None
_network_mapper: NetworkMapper | None = None
_bridge = None


class PurgeNodesRequest(BaseModel):
    node_ids: list[str] = []


def init_routes(node_repo: NodeRepository, network_mapper: NetworkMapper, bridge=None) -> None:
    global _node_repo, _network_mapper, _bridge
    _node_repo = node_repo
    _network_mapper = network_mapper
    _bridge = bridge


def _normalize(value: str | None) -> str:
    if value is None:
        return ""
    v = str(value).strip()
    if not v:
        return ""
    return v if v.startswith('!') else f'!{v}'


def _is_hidden_node(node_id: str | None) -> bool:
    if not _bridge:
        return False
    store = getattr(_bridge, "store", None)
    if not store or not hasattr(store, "is_node_hidden"):
        return False
    return bool(store.is_node_hidden(node_id))


def _last_heard_sort_value(value) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    if value is None:
        return 0
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    try:
        return int(datetime.fromisoformat(text).timestamp())
    except Exception:
        pass
    return 0


def _overlay_meshtak(node_dict: dict) -> dict:
    node_dict["node_id"] = _normalize(node_dict.get("node_id"))
    if not _bridge:
        return node_dict
    bridge_nodes = { _normalize(n.get('node_id')): n for n in _bridge.store.get_nodes() }
    node_id = _normalize(node_dict.get('node_id'))
    b = bridge_nodes.get(node_id)
    if not b:
        return node_dict
    # prefer Meshtastic short/long names when same node ID exists in both
    if b.get('short_name'):
        node_dict['short_name'] = b.get('short_name')
    if b.get('long_name'):
        node_dict['long_name'] = b.get('long_name')
    node_dict['display_name'] = b.get('short_name') or b.get('display_name') or node_dict.get('display_name') or node_dict.get('node_id')
    if b.get('lat') is not None:
        node_dict['latitude'] = b.get('lat')
    if b.get('lon') is not None:
        node_dict['longitude'] = b.get('lon')
    if b.get('alt') is not None:
        node_dict['altitude'] = b.get('alt')
    node_dict['meshtastic_seen'] = True
    node_dict['via'] = b.get('via') or node_dict.get('via')
    return node_dict


def _meshtak_only_nodes() -> list[dict]:
    if not _bridge:
        return []

    bridge_nodes = []
    for node in _bridge.store.get_nodes():
        node_id = _normalize(node.get("node_id"))
        if not node_id:
            continue
        bridge_nodes.append(
            {
                "node_id": node_id,
                "display_name": node.get("display_name") or node.get("short_name") or node.get("long_name") or node_id,
                "short_name": node.get("short_name"),
                "long_name": node.get("long_name"),
                "protocol": "meshtastic",
                "via": node.get("via") or "heltec",
                "latitude": node.get("lat"),
                "longitude": node.get("lon"),
                "altitude": node.get("alt"),
                "last_heard": node.get("last_heard"),
                "latest_rssi": node.get("rssi"),
                "latest_snr": node.get("snr"),
                "meshtastic_seen": True,
            }
        )
    return bridge_nodes


@router.get("")
async def list_nodes(limit: int = 500, enrich: bool = True):
    nodes = await _node_repo.get_all_with_signal(limit) if enrich else [n.to_dict() for n in await _node_repo.get_all(limit)]
    merged: dict[str, dict] = {}

    for node in nodes:
        normalized_id = _normalize(node.get("node_id"))
        if not normalized_id or _is_hidden_node(normalized_id):
            continue
        merged[normalized_id] = _overlay_meshtak(node)

    for node in _meshtak_only_nodes():
        normalized_id = _normalize(node.get("node_id"))
        if not normalized_id or _is_hidden_node(normalized_id) or normalized_id in merged:
            continue
        merged[normalized_id] = node

    return sorted(
        merged.values(),
        key=lambda node: (
            -_last_heard_sort_value(node.get("last_heard")),
            str(node.get("display_name") or node.get("short_name") or node.get("node_id") or ""),
        ),
    )[:limit]


@router.get("/count")
async def node_count():
    repo_nodes = await _node_repo.get_all(5000)
    merged: dict[str, dict] = {}

    for node in repo_nodes:
        node_dict = _overlay_meshtak(node.to_dict())
        normalized_id = _normalize(node_dict.get("node_id"))
        if normalized_id and not _is_hidden_node(normalized_id):
            merged[normalized_id] = node_dict

    for node in _meshtak_only_nodes():
        normalized_id = _normalize(node.get("node_id"))
        if normalized_id and not _is_hidden_node(normalized_id) and normalized_id not in merged:
            merged[normalized_id] = node

    active_cutoff = int((datetime.now(timezone.utc) - timedelta(hours=24)).timestamp())
    active = 0
    for node in merged.values():
        if _last_heard_sort_value(node.get("last_heard")) >= active_cutoff:
            active += 1

    return {"count": len(merged), "active": active}


@router.get("/map")
async def map_data():
    data = await _network_mapper.get_map_data()
    if isinstance(data, list):
        return [_overlay_meshtak(n) for n in data if not _is_hidden_node(n.get("node_id"))]
    if isinstance(data, dict) and 'nodes' in data:
        data['nodes'] = [_overlay_meshtak(n) for n in data['nodes'] if not _is_hidden_node(n.get("node_id"))]
    return data


@router.get("/summary")
async def network_summary():
    return await _network_mapper.get_network_summary()


@router.get("/{node_id}")
async def get_node(node_id: str):
    normalized_id = _normalize(node_id)
    if _is_hidden_node(normalized_id):
        raise HTTPException(status_code=404, detail="Node not found")
    node = await _node_repo.get_by_id(normalized_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    return _overlay_meshtak(node.to_dict())


@router.delete("/{node_id}")
async def delete_node(node_id: str):
    normalized_id = _normalize(node_id)
    deleted_repo = await _node_repo.delete_by_id(normalized_id)
    deleted_bridge = 0
    if _bridge:
        deleted_bridge = 1 if _bridge.store.delete_node(normalized_id) else 0

    deleted = max(deleted_repo, deleted_bridge)
    if deleted <= 0:
        raise HTTPException(status_code=404, detail="Node not found")

    return {"ok": True, "deleted": deleted, "node_id": normalized_id}


@router.post("/purge")
async def purge_nodes(payload: PurgeNodesRequest):
    normalized_ids = []
    seen = set()
    for node_id in payload.node_ids:
        normalized_id = _normalize(node_id)
        if normalized_id and normalized_id not in seen:
            normalized_ids.append(normalized_id)
            seen.add(normalized_id)

    if not normalized_ids:
        raise HTTPException(status_code=400, detail="No node IDs provided")

    deleted_repo = await _node_repo.delete_many(normalized_ids)
    deleted_bridge = 0
    if _bridge:
        deleted_bridge = _bridge.store.delete_nodes(normalized_ids)

    return {
        "ok": True,
        "deleted": max(deleted_repo, deleted_bridge),
        "requested": len(normalized_ids),
    }
