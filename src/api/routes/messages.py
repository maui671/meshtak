
from __future__ import annotations
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api", tags=["messages"])
_bridge = None

class SendMessageRequest(BaseModel):
    text: str
    to: str | None = None
    channel_index: int | None = None
    channel_name: str | None = None


def init_routes(bridge) -> None:
    global _bridge
    _bridge = bridge

@router.get('/status')
async def status():
    cfg = _bridge.get_config() if _bridge else {}
    return {
        'ok': True,
        'active_connected': _bridge.is_connected() if _bridge else False,
        'tak_enabled': bool(cfg.get('tak',{}).get('enabled', False)),
        'tak_protocol': cfg.get('tak',{}).get('protocol', 'udp'),
        'stats': _bridge.store.stats() if _bridge else {},
    }

@router.get('/message-targets')
async def message_targets():
    nodes = _bridge.store.get_nodes() if _bridge else []
    channels = (_bridge.get_config().get('channels') or []) if _bridge else []
    targets = [{'kind':'broadcast','label':'Broadcast','to':None,'channel_index':0,'channel_name':'Broadcast','pinned':True}]
    for ch in channels:
        targets.append({'kind':'channel','label':f"{ch.get('name','Channel')} (ch {ch.get('index',0)})",'to':None,'channel_index':int(ch.get('index',0)),'channel_name':ch.get('name',''),'pinned':bool(ch.get('pinned',True))})
    seen = set()
    node_targets = []
    for node in nodes:
        node_id = str(node.get('node_id') or '').strip()
        if not node_id or node_id in seen:
            continue
        seen.add(node_id)
        label = str(node.get('short_name') or node.get('display_name') or node.get('long_name') or node_id).strip() or node_id
        if label != node_id:
            label = f"{label} ({node_id})"
        node_targets.append({'kind':'node','label':label,'to':node_id,'channel_index':0,'channel_name':'Direct','pinned':False})
    node_targets.sort(key=lambda x: x['label'].lower())
    targets.extend(node_targets)
    return {'ok': True, 'targets': targets}

@router.get('/messages')
async def messages(limit: int = 200):
    limit = max(1, min(limit, 1000))
    return {'ok': True, 'messages': _bridge.store.get_messages(limit=limit) if _bridge else []}

@router.post('/messages/send')
async def send_message(req: SendMessageRequest):
    if not _bridge:
        raise HTTPException(status_code=503, detail='Messaging bridge unavailable')
    _bridge.queue_tx(req.text, req.to, req.channel_index, req.channel_name)
    return {'ok': True, 'queued': req.model_dump()}

@router.get('/meshtak-nodes')
async def meshtak_nodes():
    return {'ok': True, 'nodes': _bridge.store.get_nodes() if _bridge else []}
