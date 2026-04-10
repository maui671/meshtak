/**
 * Simple node list for the local MeshTAK dashboard.
 * Shows nodes with name, protocol badge, RSSI, and time ago.
 * Clicking a node emits a selection event for the map.
 */
class SimpleNodeList {
    constructor(containerId) {
        this._container = document.getElementById(containerId);
        this._nodes = [];
        this._visibleNodes = [];
        this._searchQuery = '';
        this._initSearch();
        this._bindClicks();
    }

    get nodeCount() { return this._nodes.length; }
    getVisibleNodes() { return this._visibleNodes.slice(); }

    _initSearch() {
        const input = document.getElementById('node-search');
        if (!input) return;
        let timer;
        input.addEventListener('input', () => {
            clearTimeout(timer);
            timer = setTimeout(() => {
                this._searchQuery = input.value.trim().toLowerCase();
                this._render();
            }, 200);
        });
    }

    _bindClicks() {
        if (!this._container) {
            return;
        }
        this._container.addEventListener('click', (event) => {
            const deleteBtn = event.target.closest('[data-node-delete]');
            if (deleteBtn) {
                event.stopPropagation();
                const nodeId = deleteBtn.dataset.nodeDelete || '';
                const node = this._nodes.find((entry) => String(entry.node_id || '') === nodeId);
                if (!node) {
                    return;
                }
                document.dispatchEvent(new CustomEvent('meshtak:node-delete-requested', {
                    detail: { node },
                }));
                return;
            }

            const item = event.target.closest('.node-item');
            if (!item) {
                return;
            }
            const nodeId = item.dataset.nodeId || '';
            const node = this._nodes.find((entry) => String(entry.node_id || '') === nodeId);
            if (!node) {
                return;
            }
            document.dispatchEvent(new CustomEvent('meshtak:node-selected', {
                detail: { node },
            }));
        });
    }

    loadNodes(nodes) {
        const deduped = new Map();
        (nodes || []).forEach((node) => {
            const nodeId = this._normalizeNodeId(node.node_id);
            if (!nodeId) {
                return;
            }
            deduped.set(nodeId, {
                ...(deduped.get(nodeId) || {}),
                ...node,
                node_id: nodeId,
                rssi: node.rssi ?? node.latest_rssi ?? null,
                snr: node.snr ?? node.latest_snr ?? null,
                latitude: node.latitude ?? node.lat ?? null,
                longitude: node.longitude ?? node.lon ?? null,
            });
        });
        this._nodes = Array.from(deduped.values());
        this._nodes.sort((a, b) => {
            const aTime = this._heardValue(a);
            const bTime = this._heardValue(b);
            return bTime - aTime;
        });
        this._render();
    }

    updateFromPacket(packet) {
        if (!packet.source_id) return;
        const sig = packet.signal || {};
        const pktRssi = sig.rssi != null ? sig.rssi : packet.rssi;
        const pktSnr = sig.snr != null ? sig.snr : packet.snr;
        const sourceId = this._normalizeNodeId(packet.source_id);

        const existing = this._nodes.find(n => n.node_id === sourceId);
        if (existing) {
            existing.last_heard = new Date().toISOString();
            if (pktRssi != null) existing.rssi = pktRssi;
            if (pktSnr != null) existing.snr = pktSnr;
            if (packet.packet_type === 'nodeinfo' && packet.decoded_payload) {
                const p = packet.decoded_payload;
                if (p.long_name) existing.long_name = p.long_name;
                if (p.short_name) existing.short_name = p.short_name;
            }
        } else {
            this._nodes.push({
                node_id: sourceId,
                protocol: packet.protocol || 'meshtastic',
                rssi: pktRssi,
                snr: pktSnr,
                last_heard: new Date().toISOString(),
            });
        }
        this._nodes.sort((a, b) => this._heardValue(b) - this._heardValue(a));
        this._render();
    }

    _render() {
        let filtered = this._nodes;
        if (this._searchQuery) {
            filtered = filtered.filter(n => {
                const name = (n.display_name || n.short_name || n.long_name || n.name || '').toLowerCase();
                const id = (n.node_id || '').toLowerCase();
                return name.includes(this._searchQuery) || id.includes(this._searchQuery);
            });
        }

        this._visibleNodes = filtered.slice();
        const status = document.getElementById('node-feed-status');
        if (status) {
            status.textContent = filtered.length === this._nodes.length
                ? `${filtered.length} nodes`
                : `Showing ${filtered.length} of ${this._nodes.length} nodes`;
        }

        if (filtered.length === 0) {
            this._container.innerHTML = '<div style="padding:1rem;text-align:center;color:var(--text-muted);font-size:0.8rem;">No nodes found</div>';
            return;
        }

        this._container.innerHTML = filtered.map(n => {
            const name = this._esc(n.display_name || n.short_name || n.long_name || n.name || n.node_id || '--');
            const proto = n.protocol || n.via || 'meshtastic';
            const rssi = n.rssi != null ? `${Number(n.rssi).toFixed(0)} dBm` : '';
            const heard = n.last_heard || n.last_seen;
            const ago = heard ? this._timeAgo(heard) : '';
            const flyable = n.latitude != null && n.longitude != null;
            const nodeId = this._esc(n.node_id || '');

            return `<div class="node-item${flyable ? ' node-item--flyable' : ''}" data-node-id="${this._esc(n.node_id || '')}">
                <div class="node-item__header">
                    <div class="node-item__content">
                        <div class="node-item__top">
                            <span class="node-item__name">${name}</span>
                            <span class="node-item__ago">${ago || '--'}</span>
                        </div>
                        <div class="node-item__bottom">
                            <span class="node-item__proto node-item__proto--${this._esc(proto)}">${this._esc(proto)}</span>
                            <span class="node-item__rssi">${rssi || '--'}</span>
                        </div>
                        <span class="node-item__id">${nodeId || '--'}</span>
                    </div>
                    <div class="node-item__actions">
                        <button class="node-item__delete" type="button" data-node-delete="${this._esc(n.node_id || '')}">Delete</button>
                    </div>
                </div>
            </div>`;
        }).join('');
    }

    _heardValue(node) {
        const heard = node.last_heard || node.last_seen || 0;
        if (typeof heard === 'number') {
            return heard;
        }
        const parsed = new Date(heard).getTime();
        return Number.isFinite(parsed) ? parsed : 0;
    }

    _timeAgo(value) {
        const ts = typeof value === 'number'
            ? (value > 1e12 ? value : value * 1000)
            : new Date(value).getTime();
        const diff = Date.now() - ts;
        const mins = Math.floor(diff / 60000);
        if (mins < 1) return 'now';
        if (mins < 60) return `${mins}m`;
        const hours = Math.floor(mins / 60);
        if (hours < 24) return `${hours}h`;
        return `${Math.floor(hours / 24)}d`;
    }

    _esc(str) {
        const el = document.createElement('span');
        el.textContent = str;
        return el.innerHTML;
    }

    _normalizeNodeId(value) {
        const text = String(value || '').trim();
        if (!text) {
            return '';
        }
        return text.startsWith('!') ? text.toLowerCase() : `!${text.toLowerCase()}`;
    }
}
