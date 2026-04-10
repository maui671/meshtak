/**
 * Packet feed with client-side filtering and scroll preservation.
 * Keeps newest packets at the top while avoiding scroll jumps when
 * the user is reviewing older rows.
 */
class SimplePacketFeed {
    constructor(tbodyId, options = {}) {
        this._tbody = document.getElementById(tbodyId);
        this._scrollContainer = document.getElementById(options.scrollContainerId) || this._tbody?.closest('.panel__body');
        this._countEl = document.getElementById(options.countId || 'packet-count');
        this._statusEl = document.getElementById(options.statusId || 'packet-feed-status');
        this._maxRows = options.maxRows || 500;
        this._packets = [];
        this._filters = {
            nodeId: '',
            protocol: '',
            packetType: '',
            query: '',
        };
        this._followLive = true;

        if (this._scrollContainer) {
            this._scrollContainer.addEventListener('scroll', () => {
                if (!this._followLive) {
                    return;
                }
                this._updateFollowIndicator();
            });
        }
    }

    setPackets(packets) {
        const list = Array.isArray(packets) ? packets.slice(0, this._maxRows) : [];
        this._packets = list;
        this._render({ resetScroll: true });
    }

    addPacket(packet) {
        if (!packet || typeof packet !== 'object') {
            return;
        }
        this._packets.unshift(packet);
        this._packets = this._packets.slice(0, this._maxRows);
        this._render({ preserveAnchor: true });
    }

    clear() {
        this._packets = [];
        this._render({ resetScroll: true });
    }

    setFilters(filters = {}) {
        this._filters = {
            ...this._filters,
            ...filters,
        };
        this._render({ resetScroll: true });
    }

    setFollowLive(enabled) {
        this._followLive = !!enabled;
        this._updateFollowIndicator();
        if (this._followLive && this._scrollContainer) {
            this._scrollContainer.scrollTop = 0;
        }
    }

    getFilters() {
        return { ...this._filters };
    }

    getVisiblePackets() {
        return this._applyFilters(this._packets);
    }

    getTotalCount() {
        return this._packets.length;
    }

    _render({ preserveAnchor = false, resetScroll = false } = {}) {
        if (!this._tbody) {
            return;
        }

        const filtered = this._applyFilters(this._packets);
        const container = this._scrollContainer;
        const previousTop = container ? container.scrollTop : 0;
        const previousHeight = container ? container.scrollHeight : 0;
        const pinnedToNewest = this._isNearNewest();

        this._tbody.innerHTML = '';
        filtered.forEach((packet) => {
            const row = this._buildRow(packet);
            this._tbody.appendChild(row);
        });

        this._updateCounts(filtered.length, this._packets.length);

        if (!container) {
            return;
        }

        if (resetScroll) {
            container.scrollTop = 0;
            this._updateFollowIndicator();
            return;
        }

        if (this._followLive && pinnedToNewest) {
            container.scrollTop = 0;
            this._updateFollowIndicator();
            return;
        }

        if (preserveAnchor) {
            const nextHeight = container.scrollHeight;
            const delta = nextHeight - previousHeight;
            container.scrollTop = Math.max(0, previousTop + delta);
        }

        this._updateFollowIndicator();
    }

    _updateCounts(visibleCount, totalCount) {
        if (this._countEl) {
            this._countEl.textContent = String(visibleCount);
        }
        if (this._statusEl) {
            this._statusEl.textContent = visibleCount === totalCount
                ? `${totalCount} packets`
                : `Showing ${visibleCount} of ${totalCount} packets`;
        }
    }

    _updateFollowIndicator() {
        const indicator = document.getElementById('packet-follow-state');
        if (!indicator) {
            return;
        }
        if (!this._followLive) {
            indicator.textContent = 'manual scroll';
            return;
        }
        indicator.textContent = this._isNearNewest() ? 'following newest' : 'paused';
    }

    _isNearNewest() {
        if (!this._scrollContainer) {
            return true;
        }
        return this._scrollContainer.scrollTop <= 24;
    }

    _applyFilters(packets) {
        const nodeId = (this._filters.nodeId || '').trim();
        const protocol = (this._filters.protocol || '').trim().toLowerCase();
        const packetType = (this._filters.packetType || '').trim().toLowerCase();
        const query = (this._filters.query || '').trim().toLowerCase();

        return (packets || []).filter((packet) => {
            if (nodeId) {
                const sourceId = String(packet.source_id || '').trim();
                const destinationId = String(packet.destination_id || '').trim();
                if (sourceId !== nodeId && destinationId !== nodeId) {
                    return false;
                }
            }

            if (protocol && String(packet.protocol || '').trim().toLowerCase() !== protocol) {
                return false;
            }

            if (packetType && String(packet.packet_type || '').trim().toLowerCase() !== packetType) {
                return false;
            }

            if (query) {
                const details = this._summarize(packet).toLowerCase();
                const haystack = [
                    packet.source_id || '',
                    packet.destination_id || '',
                    packet.protocol || '',
                    packet.packet_type || '',
                    details,
                ].join(' ').toLowerCase();
                if (!haystack.includes(query)) {
                    return false;
                }
            }

            return true;
        });
    }

    _buildRow(packet) {
        const tr = document.createElement('tr');
        tr.classList.add('packet-row');

        const time = packet.rx_time
            ? new Date(packet.rx_time * 1000).toLocaleTimeString()
            : packet.timestamp
                ? new Date(packet.timestamp).toLocaleTimeString()
                : new Date().toLocaleTimeString();

        const srcShort = this._shortId(packet.source_id);
        const destShort = this._shortId(packet.destination_id);
        const sig = packet.signal || {};
        const rawRssi = sig.rssi != null ? sig.rssi : packet.rssi;
        const rawSnr = sig.snr != null ? sig.snr : packet.snr;
        const rssiVal = rawRssi != null ? Number(rawRssi).toFixed(0) : null;
        const rssi = rssiVal != null ? rssiVal : '--';
        const snr = rawSnr != null ? `${Number(rawSnr).toFixed(1)}` : '--';
        const type = packet.packet_type || '--';
        const protocol = packet.protocol || 'meshtastic';
        const details = this._summarize(packet);
        const hops = packet.hop_start > 0
            ? `${packet.hop_start - packet.hop_limit}/${packet.hop_start}`
            : '--';

        const typeClass = `type-${type.replace(/[^a-zA-Z0-9_-]/g, '')}`;
        const protocolClass = `protocol-${protocol}`;
        const rssiClass = this._rssiClass(rssiVal);

        tr.innerHTML = `
            <td>${time}</td>
            <td class="${protocolClass}">${protocol}</td>
            <td class="td-source">${srcShort}</td>
            <td>${destShort}</td>
            <td class="${typeClass}">${type}</td>
            <td class="${rssiClass}">${rssi}</td>
            <td>${snr}</td>
            <td>${hops}</td>
            <td class="packet-details-cell ${typeClass}">${this._esc(details)}</td>
        `;

        tr.addEventListener('click', () => this._toggleDetail(tr, packet));
        return tr;
    }

    _toggleDetail(tr, packet) {
        const next = tr.nextElementSibling;
        if (next && next.classList.contains('packet-detail-row')) {
            next.remove();
            return;
        }

        const prev = this._tbody.querySelector('.packet-detail-row');
        if (prev) {
            prev.remove();
        }

        const detailTr = document.createElement('tr');
        detailTr.classList.add('packet-detail-row');
        const td = document.createElement('td');
        td.colSpan = 9;

        const payload = packet.decoded_payload;
        if (payload && typeof payload === 'object') {
            td.textContent = JSON.stringify(payload, null, 2);
        } else {
            td.textContent = `Source: ${packet.source_id || '--'}\nType: ${packet.packet_type || '--'}\nRSSI: ${packet.rssi || '--'} dBm\nSNR: ${packet.snr || '--'} dB`;
        }

        detailTr.appendChild(td);
        tr.after(detailTr);
    }

    _summarize(packet) {
        const p = packet.decoded_payload;
        if (!p) {
            return '--';
        }

        switch (packet.packet_type) {
            case 'text':
                return p.text || '--';
            case 'position': {
                const parts = [];
                if (p.latitude != null) {
                    parts.push(`${p.latitude.toFixed(4)}`);
                }
                if (p.longitude != null) {
                    parts.push(`${p.longitude.toFixed(4)}`);
                }
                if (p.altitude != null) {
                    parts.push(`alt ${p.altitude}m`);
                }
                return parts.join(', ') || '--';
            }
            case 'nodeinfo':
                return [p.long_name, p.short_name, p.hw_model].filter(Boolean).join(' ') || '--';
            case 'telemetry': {
                const parts = [];
                if (p.battery_level != null) {
                    parts.push(`batt=${p.battery_level}%`);
                }
                if (p.voltage != null) {
                    parts.push(`${Number(p.voltage).toFixed(1)}V`);
                }
                if (p.temperature != null) {
                    parts.push(`${Number(p.temperature).toFixed(0)}C`);
                }
                return parts.join(' ') || '--';
            }
            default:
                return '--';
        }
    }

    _rssiClass(val) {
        if (val == null) {
            return '';
        }
        const n = Number(val);
        if (n >= -90) {
            return 'rssi-good';
        }
        if (n >= -110) {
            return 'rssi-mid';
        }
        return 'rssi-bad';
    }

    _shortId(id) {
        if (!id) {
            return '--';
        }
        if (id === 'ffffffff' || id === 'ffff') {
            return 'BCAST';
        }
        return id.length > 6 ? `!${id.slice(-4)}` : id;
    }

    _esc(str) {
        const el = document.createElement('span');
        el.textContent = str;
        return el.innerHTML;
    }
}
