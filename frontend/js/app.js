/**
 * MeshTAK dashboard controller
 * Preserves the Mesh Point dashboard behavior and adds:
 * - radio status polling
 * - Meshtastic node overlay for messaging recipients
 * - channel selector injection/population
 * - message feed loading
 * - send-message wiring
 */

document.addEventListener('DOMContentLoaded', async () => {
    const nodeMap = new NodeMap('map');
    const nodeList = new SimpleNodeList('node-list');
    const packetFeed = new SimplePacketFeed('packet-tbody');

    _ensureMessageChannelSelector();

    await _loadInitial(nodeMap, nodeList, packetFeed);
    await _updateStats();
    await _refreshRadioUi();
    _checkForUpdate();

    window.concentratorWS.on('packet', (packet) => {
        packetFeed.addPacket(packet);
        nodeMap.updateFromPacket(packet);
        nodeList.updateFromPacket(packet);
        _incrementPacketCount();
    });

    window.concentratorWS.connect();

    const sendBtn = document.getElementById('message-send');
    if (sendBtn) {
        sendBtn.addEventListener('click', _sendMessage);
    }

    setInterval(() => {
        _refreshData(nodeMap, nodeList);
        _updateStats();
    }, 15_000);

    setInterval(async () => {
        await _refreshRadioUi();
    }, 5_000);

    setInterval(_checkForUpdate, 300_000);
});

async function _loadInitial(nodeMap, nodeList, packetFeed) {
    try {
        const [deviceRes, nodesRes, packetsRes] = await Promise.all([
            fetch('/api/device'),
            fetch('/api/nodes'),
            fetch('/api/packets?limit=50'),
        ]);

        const device = await deviceRes.json();
        const nodesData = await nodesRes.json();
        const packetsData = await packetsRes.json();

        _setText('device-name', device.device_name || 'MeshTAK');

        const nodes = nodesData.nodes || nodesData || [];
        nodeMap.loadNodes(nodes, device);
        nodeList.loadNodes(nodes);

        const packets = packetsData.packets || packetsData || [];
        const sorted = packets.sort((a, b) => {
            const aTime = a.rx_time || new Date(a.timestamp || 0).getTime() / 1000;
            const bTime = b.rx_time || new Date(b.timestamp || 0).getTime() / 1000;
            return aTime - bTime;
        });

        sorted.forEach(pkt => packetFeed.addPacket(pkt));
        _totalPackets = sorted.length;
    } catch (e) {
        console.error('Initial load failed:', e);
    }
}

async function _refreshData(nodeMap, nodeList) {
    try {
        const res = await fetch('/api/nodes');
        const data = await res.json();
        const nodes = data.nodes || data || [];
        nodeMap.loadNodes(nodes);
        nodeList.loadNodes(nodes);
    } catch (e) {
        console.error('Refresh failed:', e);
    }
}

async function _updateStats() {
    try {
        const [trafficRes, signalRes, nodeRes, deviceRes, metricsRes] = await Promise.all([
            fetch('/api/analytics/traffic'),
            fetch('/api/analytics/signal/summary'),
            fetch('/api/nodes/count'),
            fetch('/api/device/status'),
            fetch('/api/device/metrics'),
        ]);

        const traffic = await trafficRes.json();
        const signal = await signalRes.json();
        const nodeCount = await nodeRes.json();
        const device = await deviceRes.json();

        _setText('stat-nodes-val', `${nodeCount.active} / ${nodeCount.count}`);
        _setText('stat-packets-val', traffic.total_packets);
        _setText('stat-rate-val', traffic.packets_per_minute);
        _setText('stat-rssi-val', signal.avg_rssi != null ? `${signal.avg_rssi} dBm` : '--');

        const relay = device.relay || {};
        _setText('stat-relay-val', relay.relayed ?? 0);
        const evaluated = (relay.relayed ?? 0) + (relay.rejected ?? 0);
        _setText('stat-relay-sub', evaluated > 0
            ? `${evaluated} evaluated`
            : relay.enabled ? 'listening...' : 'relay off');

        _setText('stat-uptime-val', _formatUptime(device.uptime_seconds || 0));

        _setText('node-count-badge', `${nodeCount.active} / ${nodeCount.count} nodes`);
        _setText('packet-count-badge', `${traffic.total_packets} packets`);
        _setText('version-badge', device.firmware_version ? `v${device.firmware_version}` : '--');

        if (metricsRes.ok) {
            const metrics = await metricsRes.json();
            _setText('stat-cpu-val', `${metrics.cpu_percent}%`);
            _setText('stat-ram-val', `${metrics.memory_percent}%`);
            _setText('stat-ram-sub', `${metrics.memory_used_mb} / ${metrics.memory_total_mb} MB`);
            _setText('stat-disk-val', `${metrics.disk_percent}%`);
            _setText('stat-disk-sub', `${metrics.disk_used_gb} / ${metrics.disk_total_gb} GB`);
            _setText('stat-temp-val', metrics.cpu_temp_c != null ? `${metrics.cpu_temp_c}°C` : 'N/A');
        }
    } catch (e) {
        console.error('Failed to update stats:', e);
    }
}

async function _refreshRadioUi() {
    await Promise.allSettled([
        _refreshRadioStatus(),
        _refreshRadioNodes(),
        _refreshChannels(),
        _refreshMessages(),
    ]);
}

async function _refreshRadioStatus() {
    try {
        const res = await fetch('/api/radio/status');
        const data = await res.json();
        const status = data.status || (data.connected ? 'connected' : 'disconnected');

        _setText('msg-status', status);

        const settingsStatus = document.getElementById('radio-settings-status');
        if (settingsStatus) {
            settingsStatus.textContent = status;
        }

        const sendBtn = document.getElementById('message-send');
        const msgBox = document.getElementById('message-text');
        const target = document.getElementById('message-target');
        const channel = document.getElementById('message-channel');

        const enabled = !!data.connected;
        if (sendBtn) sendBtn.disabled = !enabled;
        if (msgBox) msgBox.disabled = !enabled;
        if (target) target.disabled = !enabled;
        if (channel) channel.disabled = !enabled;
    } catch (e) {
        console.error('Radio status refresh failed:', e);
        _setText('msg-status', 'disconnected');
    }
}

async function _refreshRadioNodes() {
    try {
        const res = await fetch('/api/radio/nodes');
        const data = await res.json();
        const nodes = data.nodes || [];

        const target = document.getElementById('message-target');
        if (!target) return;

        const current = target.value;
        target.innerHTML = '';

        const broadcastOpt = document.createElement('option');
        broadcastOpt.value = '';
        broadcastOpt.textContent = 'Broadcast';
        target.appendChild(broadcastOpt);

        nodes
            .slice()
            .sort((a, b) => {
                const an = _nodeDisplayName(a).toLowerCase();
                const bn = _nodeDisplayName(b).toLowerCase();
                return an.localeCompare(bn);
            })
            .forEach((node) => {
                const nodeId = node.node_id || '';
                if (!nodeId) return;

                const opt = document.createElement('option');
                opt.value = nodeId;
                opt.textContent = `${_nodeDisplayName(node)} (${nodeId})`;
                target.appendChild(opt);
            });

        if ([...target.options].some(o => o.value === current)) {
            target.value = current;
        }
    } catch (e) {
        console.error('Radio nodes refresh failed:', e);
    }
}

async function _refreshChannels() {
    try {
        const res = await fetch('/api/radio/channels');
        const data = await res.json();
        const channels = data.channels || [];

        const select = document.getElementById('message-channel');
        if (!select) return;

        const current = select.value;
        select.innerHTML = '';

        channels.forEach((ch) => {
            const idx = Number.isFinite(Number(ch.index)) ? Number(ch.index) : 0;
            const name = ch.name || `Channel ${idx}`;
            const opt = document.createElement('option');
            opt.value = String(idx);
            opt.textContent = ch.pinned ? `${name} ★` : name;
            opt.dataset.channelName = name;
            select.appendChild(opt);
        });

        if (select.options.length === 0) {
            const opt = document.createElement('option');
            opt.value = '0';
            opt.textContent = 'Broadcast';
            opt.dataset.channelName = 'Broadcast';
            select.appendChild(opt);
        }

        if ([...select.options].some(o => o.value === current)) {
            select.value = current;
        }
    } catch (e) {
        console.error('Channel refresh failed:', e);
    }
}

async function _refreshMessages() {
    try {
        const res = await fetch('/api/messages');
        const data = await res.json();
        const messages = data.messages || [];

        const feed = document.getElementById('message-feed');
        if (!feed) return;

        feed.innerHTML = '';

        messages
            .slice(-50)
            .forEach((msg) => {
                const row = document.createElement('div');
                row.style.padding = '8px 10px';
                row.style.borderBottom = '1px solid rgba(255,255,255,0.08)';
                row.style.fontSize = '13px';

                const who = msg.direction === 'tx'
                    ? `TX → ${msg.to_name || msg.to_id || 'Broadcast'}`
                    : `RX ← ${msg.from_name || msg.from_id || 'Unknown'}`;

                const chan = msg.channel ? ` [${msg.channel}]` : '';
                row.textContent = `${who}${chan}: ${msg.text || ''}`;
                feed.appendChild(row);
            });

        feed.scrollTop = feed.scrollHeight;
    } catch (e) {
        console.error('Message refresh failed:', e);
    }
}

async function _sendMessage() {
    const target = document.getElementById('message-target');
    const channel = document.getElementById('message-channel');
    const textBox = document.getElementById('message-text');

    const text = (textBox?.value || '').trim();
    if (!text) {
        _setText('msg-status', 'message required');
        return;
    }

    const channelIndex = channel ? parseInt(channel.value || '0', 10) : 0;
    const channelName = channel?.selectedOptions?.[0]?.dataset?.channelName || 'Broadcast';
    const to = target?.value || '';

    try {
        _setText('msg-status', 'sending...');

        const res = await fetch('/api/messages/send', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                text,
                to: to || null,
                channel_index: Number.isFinite(channelIndex) ? channelIndex : 0,
                channel_name: channelName,
            }),
        });

        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            throw new Error(data.detail || data.error || 'send failed');
        }

        if (textBox) textBox.value = '';
        _setText('msg-status', 'sent');
        await _refreshMessages();
    } catch (e) {
        console.error('Send failed:', e);
        _setText('msg-status', e.message || 'send failed');
    }
}

function _ensureMessageChannelSelector() {
    const existing = document.getElementById('message-channel');
    if (existing) return; // don't create duplicates

    const panel = document.querySelector('.messaging-panel');
    if (!panel) return;

    const select = document.createElement('select');
    select.id = 'message-channel';
    select.className = 'node-search';
    select.style.marginBottom = '8px';

    const label = document.createElement('div');
    label.textContent = 'Channel';
    label.style.fontSize = '12px';
    label.style.opacity = '0.7';
    label.style.marginTop = '6px';

    panel.appendChild(label);
    panel.appendChild(select);
}

function _nodeDisplayName(node) {
    return node.short_name || node.display_name || node.long_name || node.node_id || 'Unknown';
}

let _totalPackets = 0;

function _incrementPacketCount() {
    _totalPackets++;
}

function _formatUptime(totalSeconds) {
    const days = Math.floor(totalSeconds / 86400);
    const hours = Math.floor((totalSeconds % 86400) / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    if (days > 0) return `${days}d ${hours}h`;
    if (hours > 0) return `${hours}h ${minutes}m`;
    return `${minutes}m`;
}

async function _checkForUpdate() {
    try {
        const res = await fetch('/api/device/update-check');
        const data = await res.json();
        const badge = document.getElementById('update-badge');
        if (!badge) return;
        if (data.update_available) {
            badge.classList.remove('hidden');
            badge.title = `Update available (local: ${data.local_version}, remote: ${data.remote_version})`;
        } else {
            badge.classList.add('hidden');
        }
    } catch (_) {}
}

function _setText(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
}
