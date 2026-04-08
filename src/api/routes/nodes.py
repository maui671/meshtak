
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from src.analytics.network_mapper import NetworkMapper
from src.storage.node_repository import NodeRepository

router = APIRouter(prefix="/api/nodes", tags=["nodes"])

_node_repo: NodeRepository | None = None
_network_mapper: NetworkMapper | None = None
_bridge = None


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


def _overlay_meshtak(node_dict: dict) -> dict:
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


@router.get("")
async def list_nodes(limit: int = 500, enrich: bool = True):
    nodes = await _node_repo.get_all_with_signal(limit) if enrich else [n.to_dict() for n in await _node_repo.get_all(limit)]
    return [_overlay_meshtak(n) for n in nodes]


@router.get("/count")
async def node_count():
    count = await _node_repo.get_count()
    active = await _node_repo.get_active_count()
    return {"count": count, "active": active}


@router.get("/map")
async def map_data():
    data = await _network_mapper.get_map_data()
    if isinstance(data, list):
        return [_overlay_meshtak(n) for n in data]
    if isinstance(data, dict) and 'nodes' in data:
        data['nodes'] = [_overlay_meshtak(n) for n in data['nodes']]
    return data


@router.get("/summary")
async def network_summary():
    return await _network_mapper.get_network_summary()


@router.get("/{node_id}")
async def get_node(node_id: str):
    node = await _node_repo.get_by_id(node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    return _overlay_meshtak(node.to_dict())
