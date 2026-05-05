/**
 * Single-page controller for the local Meshpoint dashboard.
 * Wires up map, node list, packet feed, health surfaces, and WebSocket.
 */
document.addEventListener('DOMContentLoaded', async () => {
    const nodeMap = new NodeMap('map-page');
    const packetFeed = new SimplePacketFeed('packet-tbody');
    window.nodeMapPage = nodeMap;

    const nodeDrawer = new NodeDrawer('node-drawer', {
        onSendMessage: (node) => _openMessagingForNode(node),
        onViewOnMap: (node) => {
            if (node.latitude && node.longitude) {
                const mapTab = document.querySelector('[data-tab="mapview"]');
                if (mapTab) mapTab.click();
                setTimeout(() => nodeMap.centerOn(node.latitude, node.longitude), 80);
            }
        },
    });

    const nodeCards = new NodeCards('node-list', (node) => nodeDrawer.open(node));

    const backdrop = document.getElementById('node-backdrop');
    if (backdrop) {
        backdrop.addEventListener('click', () => {
            nodeDrawer.close();
            backdrop.classList.remove('nd-backdrop--visible');
        });
    }

    const origOpen = nodeDrawer.open.bind(nodeDrawer);
    nodeDrawer.open = async (node) => {
        if (backdrop) backdrop.classList.add('nd-backdrop--visible');
        await origOpen(node);
    };
    const origClose = nodeDrawer.close.bind(nodeDrawer);
    nodeDrawer.close = () => {
        if (backdrop) backdrop.classList.remove('nd-backdrop--visible');
        origClose();
    };

    await _loadInitial(nodeMap, nodeCards, packetFeed);
    await _updateStats();
    _checkForUpdate();

    window.concentratorWS.on('packet', (packet) => {
        packetFeed.addPacket(packet);
        nodeMap.updateFromPacket(packet);
        nodeCards.updateFromPacket(packet);
        _incrementPacketCount();
    });

    _setupTabs();
    window.concentratorWS.connect();

    setInterval(() => {
        _refreshData(nodeMap, nodeCards);
        _updateStats();
    }, 15_000);

    setInterval(_checkForUpdate, 300_000);
});

function _openMessagingForNode(node) {
    const msgTab = document.querySelector('[data-tab="messages"]');
    if (msgTab) msgTab.click();

    setTimeout(() => {
        if (window.messagingPanel) {
            window.messagingPanel.openConversation({
                node_id: node.node_id,
                node_name: node.display_name || node.long_name || node.node_id,
                protocol: node.protocol || 'meshtastic',
                is_broadcast: false,
            });
        }
    }, 100);
}

async function _loadInitial(nodeMap, nodeList, packetFeed) {
    try {
        const [deviceRes, nodesRes, packetsRes, configRes] = await Promise.all([
            fetch('/api/device'),
            fetch('/api/nodes?enrich=true'),
            fetch('/api/packets?limit=50'),
            fetch('/api/config'),
        ]);
        const device = await deviceRes.json();
        const nodesData = await nodesRes.json();
        const packetsData = await packetsRes.json();
        const config = await configRes.json();

        _setText('device-name', device.device_name || 'Meshpoint');
        if (device.device_id) {
            const short = device.device_id.slice(0, 8);
            const idEl = document.getElementById('device-id');
            if (idEl) {
                idEl.textContent = short;
                idEl.title = device.device_id;
                idEl.addEventListener('click', () => {
                    navigator.clipboard.writeText(device.device_id).then(() => {
                        idEl.textContent = 'copied!';
                        setTimeout(() => { idEl.textContent = short; }, 1500);
                    });
                });
            }
        }

        const nodes = nodesData.nodes || nodesData || [];
        nodeMap.loadNodes(nodes, device);
        nodeList.loadNodes(nodes);

        const packets = packetsData.packets || packetsData || [];
        const sorted = packets.sort((a, b) => {
            const aTime = a.rx_time || new Date(a.timestamp || 0).getTime() / 1000;
            const bTime = b.rx_time || new Date(b.timestamp || 0).getTime() / 1000;
            return aTime - bTime;
        });
        sorted.forEach((pkt) => packetFeed.addPacket(pkt));
        _totalPackets = sorted.length;
        _renderOverview(device, config);
    } catch (e) {
        console.error('Initial load failed:', e);
    }
}

async function _refreshData(nodeMap, nodeList) {
    try {
        const [nodesRes, deviceRes] = await Promise.all([
            fetch('/api/nodes?enrich=true'),
            fetch('/api/device'),
        ]);
        const data = await nodesRes.json();
        const device = await deviceRes.json();
        const nodes = data.nodes || data || [];
        nodeMap.loadNodes(nodes, device);
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

        _renderServiceHealth(device);
        _renderHealthPage(device);

        if (metricsRes.ok) {
            const metrics = await metricsRes.json();
            _renderHealthMetrics(metrics, device);
        }
    } catch (e) {
        console.error('Failed to update stats:', e);
    }
}

function _renderOverview(device, config) {
    const radio = config.radio || {};
    const capture = (config.capture && config.capture.sources) || [];
    _setText('hero-region-chip', `Region ${radio.region || '--'}`);
    _setText(
        'hero-frequency-chip',
        `Freq ${radio.frequency_mhz ? `${radio.frequency_mhz} MHz` : '--'}`
    );
    _setText(
        'hero-source-chip',
        `Source ${capture.length ? capture.join(' + ') : '--'}`
    );
}

function _renderServiceHealth(status) {
    const mqtt = status.mqtt || {};
    const tak = status.tak || {};
    const upstream = status.upstream || {};

    _applyServiceState(
        'mqtt',
        mqtt.enabled ? (mqtt.connected ? 'online' : 'error') : 'idle',
        mqtt.enabled ? `${mqtt.broker || '--'}:${mqtt.port || '--'}` : 'disabled',
        mqtt.enabled
            ? (mqtt.connected ? `${mqtt.auth_mode || 'mqtt'} uplink active` : (mqtt.last_error || 'not connected'))
            : 'local MQTT disabled'
    );

    _applyServiceState(
        'tak',
        tak.enabled ? (tak.connected ? 'online' : 'error') : 'idle',
        tak.enabled ? (tak.target || '--') : 'disabled',
        tak.enabled
            ? `${tak.publish_count || 0} sends${tak.last_error ? ` • ${tak.last_error}` : ''}`
            : 'TAK injection disabled'
    );

    _applyServiceState(
        'upstream',
        upstream.enabled ? (upstream.connected ? 'online' : 'error') : 'idle',
        upstream.enabled ? (upstream.url || '--') : 'disabled',
        upstream.enabled
            ? (upstream.connected ? 'connected to Meshradar cloud' : 'enabled but not connected')
            : 'cloud uplink disabled'
    );
}

function _renderHealthMetrics(metrics, device) {
    _setText('health-cpu-val', `${metrics.cpu_percent}%`);
    _setText('health-ram-val', `${metrics.memory_percent}%`);
    _setText('health-ram-sub', `${metrics.memory_used_mb} / ${metrics.memory_total_mb} MB`);
    _setText('health-disk-val', `${metrics.disk_percent}%`);
    _setText('health-disk-sub', `${metrics.disk_used_gb} / ${metrics.disk_total_gb} GB`);
    _setText('health-temp-val', metrics.cpu_temp_c != null ? `${metrics.cpu_temp_c}°C` : 'N/A');
    _setText('health-uptime-val', _formatUptime(device.uptime_seconds || 0));
    _setText('health-ws-val', device.websocket_clients ?? 0);
}

function _renderHealthPage(status) {
    const mqtt = status.mqtt || {};
    const tak = status.tak || {};
    const upstream = status.upstream || {};
    const relay = status.relay || {};

    const serviceList = document.getElementById('health-service-list');
    if (serviceList) {
        serviceList.innerHTML = [
            ['MQTT', mqtt.enabled ? `${mqtt.connected ? 'online' : 'attention'} • ${mqtt.broker || '--'}:${mqtt.port || '--'}` : 'disabled'],
            ['TAK', tak.enabled ? `${tak.connected ? 'online' : 'attention'} • ${tak.target || '--'}` : 'disabled'],
            ['Cloud', upstream.enabled ? `${upstream.connected ? 'online' : 'attention'} • ${upstream.url || '--'}` : 'disabled'],
            ['Relay', relay.enabled ? `${relay.relayed ?? 0} relayed` : 'disabled'],
        ].map(([label, value]) => `
            <div class="health-kv">
                <span class="health-kv__label">${label}</span>
                <span class="health-kv__value">${value}</span>
            </div>
        `).join('');
    }

    const deviceList = document.getElementById('health-device-list');
    if (deviceList) {
        deviceList.innerHTML = [
            ['Device', status.device_id || '--'],
            ['Version', status.firmware_version ? `v${status.firmware_version}` : '--'],
            ['Uptime', _formatUptime(status.uptime_seconds || 0)],
            ['Web Clients', status.websocket_clients ?? 0],
        ].map(([label, value]) => `
            <div class="health-kv">
                <span class="health-kv__label">${label}</span>
                <span class="health-kv__value">${value}</span>
            </div>
        `).join('');
    }
}

function _applyServiceState(key, state, endpoint, meta) {
    const pill = document.getElementById(`service-${key}-pill`);
    const endpointEl = document.getElementById(`service-${key}-endpoint`);
    const metaEl = document.getElementById(`service-${key}-meta`);
    const card = document.getElementById(`service-${key}`);

    if (pill) {
        pill.textContent = state === 'online' ? 'Online' : state === 'error' ? 'Attention' : 'Idle';
        pill.className = `service-pill service-pill--${state}`;
    }
    if (endpointEl) endpointEl.textContent = endpoint;
    if (metaEl) metaEl.textContent = meta;
    if (card) card.dataset.state = state;
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

function _setupTabs() {
    document.querySelectorAll('.tab-bar__btn').forEach((btn) => {
        btn.addEventListener('click', () => {
            const tabId = btn.dataset.tab;
            document.querySelectorAll('.tab-bar__btn').forEach((b) => b.classList.remove('tab-bar__btn--active'));
            btn.classList.add('tab-bar__btn--active');

            document.querySelectorAll('.tab-content').forEach((tc) => tc.classList.remove('tab-content--active'));
            const target = document.getElementById(`tab-${tabId}`);
            if (target) target.classList.add('tab-content--active');

            if (tabId === 'messages' && window.messagingPanel) {
                window.messagingPanel.onActivated();
                window.messagingPanel.resetUnreadBadge();
            }
            if (tabId === 'radio' && window.radioSettings) {
                window.radioSettings.onActivated();
            }
            if (tabId === 'mapview' && window.nodeMapPage) {
                setTimeout(() => window.nodeMapPage.invalidateSize(), 60);
            }
        });
    });
}

function _setText(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
}
