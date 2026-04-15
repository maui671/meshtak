/**
 * MeshTAK dashboard controller
 * Adds message/history QoL improvements without touching
 * existing TAK/PLI handling:
 * - discrete message/packet purge actions
 * - filter controls for message and packet history
 * - sticky-follow scroll behavior that pauses while reviewing
 * - direct-message aware channel behavior
 * - node list to map focus
 * - draggable/resizable dashboard widgets
 * - node purge/delete controls
 */

const MESSAGE_LIMIT = 500;
const PACKET_LIMIT = 500;

const messageState = {
    allMessages: [],
    filters: {
        nodeId: '',
        channel: '',
        direction: '',
        query: '',
    },
    followLatest: true,
};

let packetFeedInstance = null;
let latestNodes = [];
let latestRadioNodes = [];
let latestChannels = [];
let latestDevice = null;
let latestSettings = {};
let latestRadioStatus = { connected: false, status: 'unknown' };
let latestAuth = { authenticated: false, role: '', username: '', show_update_notices: false };

document.addEventListener('DOMContentLoaded', async () => {
    const nodeMap = new NodeMap('map');
    const nodeList = new SimpleNodeList('node-list');
    const packetFeed = new SimplePacketFeed('packet-tbody', {
        scrollContainerId: 'packet-scroll',
        countId: 'packet-count',
        statusId: 'packet-feed-status',
        maxRows: PACKET_LIMIT,
        nodeLabelResolver: _packetNodeLabel,
        nodeMatcher: _packetNodeMatches,
    });

    packetFeedInstance = packetFeed;

    _bindMessageControls();
    _bindPacketControls();
    _bindNodeControls(nodeMap, nodeList);
    _bindDashboardWorkspace(nodeMap);
    _bindCommandShell(nodeMap);
    _bindSettingsControls();
    _bindRadioConfigControls();
    _bindAuthControls();
    _bindUserAccountControls();
    _startCommandClock();

    await _refreshAuthStatus();
    await _loadInitial(nodeMap, nodeList, packetFeed);
    await _updateStats();
    await _refreshRadioUi();
    _checkForUpdate();

    window.concentratorWS.on('packet', (packet) => {
        packetFeed.addPacket(packet);
        nodeMap.updateFromPacket(packet);
        nodeList.updateFromPacket(packet);
        _incrementPacketCount();
        _refreshCommandBanner();
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
        const [deviceRes, nodesRes, radioNodesRes, packetsRes] = await Promise.all([
            fetch('/api/device'),
            fetch('/api/nodes'),
            fetch('/api/radio/nodes'),
            fetch(`/api/packets?limit=${PACKET_LIMIT}`),
        ]);

        const device = await deviceRes.json();
        latestDevice = device;
        const nodesData = await nodesRes.json();
        const radioNodesData = await radioNodesRes.json();
        const packetsData = await packetsRes.json();

        _setText('device-name', device.device_name || 'MeshTAK');

        const nodes = Array.isArray(nodesData) ? nodesData : (nodesData.nodes || []);
        const radioNodes = radioNodesData.nodes || [];
        latestNodes = nodes.slice();
        latestRadioNodes = radioNodes.slice();
        const mergedNodes = _mergeNodeCollections(nodes, radioNodes);
        nodeMap.loadNodes(mergedNodes, device);
        nodeList.loadNodes(mergedNodes);
        _refreshNodeFilterOptions();

        const packets = (packetsData.packets || packetsData || [])
            .slice()
            .sort((a, b) => _packetTimestamp(b) - _packetTimestamp(a));

        packetFeed.setPackets(packets);
        _totalPackets = packets.length;
        _refreshCommandBanner();
    } catch (e) {
        console.error('Initial load failed:', e);
    }
}

async function _refreshData(nodeMap, nodeList) {
    try {
        const [nodesRes, radioNodesRes] = await Promise.all([
            fetch('/api/nodes'),
            fetch('/api/radio/nodes'),
        ]);
        const data = await nodesRes.json();
        const radioData = await radioNodesRes.json();
        const nodes = data.nodes || data || [];
        const radioNodes = radioData.nodes || [];
        latestNodes = nodes.slice();
        latestRadioNodes = radioNodes.slice();
        const mergedNodes = _mergeNodeCollections(nodes, radioNodes);
        nodeMap.loadNodes(mergedNodes, latestDevice);
        nodeList.loadNodes(mergedNodes);
        _refreshNodeFilterOptions();
        packetFeedInstance?.refreshLabels?.();
        _refreshCommandBanner();
    } catch (e) {
        console.error('Refresh failed:', e);
    }
}

async function _updateStats() {
    try {
        const [trafficRes, signalRes, nodeRes, deviceRes, metricsRes, settingsRes] = await Promise.all([
            fetch('/api/analytics/traffic'),
            fetch('/api/analytics/signal/summary'),
            fetch('/api/nodes/count'),
            fetch('/api/device/status'),
            fetch('/api/device/metrics'),
            fetch('/api/settings'),
        ]);

        const traffic = await trafficRes.json();
        const signal = await signalRes.json();
        const nodeCount = await nodeRes.json();
        const device = await deviceRes.json();
        if (settingsRes.ok) {
            latestSettings = await settingsRes.json();
        }

        _setText('stat-nodes-val', `${nodeCount.active} / ${nodeCount.count}`);
        _setText('stat-packets-val', traffic.total_packets);
        _setText('stat-rate-val', traffic.packets_per_minute);
        _setText('stat-rssi-val', signal.avg_rssi != null ? `${signal.avg_rssi} dBm` : '--');
        _setText('stat-nodes-copy', `${nodeCount.active} active / ${nodeCount.count} total`);
        _setText('stat-packets-copy', `${traffic.total_packets} captured`);

        const relay = device.relay || {};
        _setText('stat-relay-val', relay.relayed ?? 0);
        const evaluated = (relay.relayed ?? 0) + (relay.rejected ?? 0);
        _setText('stat-relay-sub', evaluated > 0
            ? `${evaluated} evaluated`
            : relay.enabled ? 'listening...' : 'relay off');
        _setText('stat-relay-copy', evaluated > 0
            ? `${relay.relayed ?? 0} relayed / ${relay.rejected ?? 0} rejected`
            : relay.enabled ? 'listening' : 'off');

        const uptime = _formatUptime(device.uptime_seconds || 0);
        _setText('stat-uptime-val', uptime);
        _setText('stat-uptime-copy', uptime);

        _setText('node-count-badge', `${nodeCount.active} / ${nodeCount.count} nodes`);
        _setText('packet-count-badge', `${traffic.total_packets} packets`);
        _setText('version-badge', device.firmware_version ? `v${device.firmware_version}` : '--');
        _setText('stat-collector-val', _moduleLabel(latestSettings.collector?.status, latestSettings.collector?.enabled));
        _setText('stat-tak-val', latestSettings.tak?.enabled ? 'Enabled' : 'Disabled');

        if (metricsRes.ok) {
            const metrics = await metricsRes.json();
            _setText('stat-cpu-val', `${metrics.cpu_percent}%`);
            _setText('stat-ram-val', `${metrics.memory_percent}%`);
            _setText('stat-ram-sub', `${metrics.memory_used_mb} / ${metrics.memory_total_mb} MB`);
            _setText('stat-disk-val', `${metrics.disk_percent}%`);
            _setText('stat-disk-sub', `${metrics.disk_used_gb} / ${metrics.disk_total_gb} GB`);
            _setText('stat-temp-val', metrics.cpu_temp_c != null ? `${metrics.cpu_temp_c}C` : 'N/A');
        }
        _renderSystemMessages();
        _refreshCommandBanner();
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
    packetFeedInstance?.refreshLabels?.();
}

async function _refreshRadioStatus() {
    try {
        const res = await fetch('/api/radio/status');
        const data = await res.json();
        latestRadioStatus = data || { connected: false, status: 'unknown' };
        const status = data.status || (data.connected ? 'connected' : 'disconnected');

        _setText('msg-status', status);
        _setText('stat-radio-val', data.connected ? 'Connected' : 'Offline');

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
        if (channel) channel.disabled = !enabled || _isDirectMessageSelected();
        _refreshCommandBanner();
    } catch (e) {
        console.error('Radio status refresh failed:', e);
        latestRadioStatus = { connected: false, status: 'disconnected' };
        _setText('msg-status', 'disconnected');
        _setText('stat-radio-val', 'Offline');
        _refreshCommandBanner();
    }
}

async function _refreshRadioNodes() {
    try {
        const res = await fetch('/api/radio/nodes');
        const data = await res.json();
        const nodes = _mergeNodeCollections(data.nodes || []);
        latestRadioNodes = nodes.slice();

        const target = document.getElementById('message-target');
        if (target) {
            const current = target.value;
            target.innerHTML = '';

            const broadcastOpt = document.createElement('option');
            broadcastOpt.value = '';
            broadcastOpt.textContent = 'Broadcast';
            target.appendChild(broadcastOpt);

            _sortedNodes(nodes).forEach((node) => {
                const nodeId = node.node_id || '';
                if (!nodeId || _isSelfNode(node)) {
                    return;
                }

                const opt = document.createElement('option');
                opt.value = nodeId;
                opt.textContent = _nodeTargetLabel(node);
                target.appendChild(opt);
            });

            if ([...target.options].some((o) => o.value === current)) {
                target.value = current;
            }
        }

        _refreshNodeFilterOptions();
        _syncMessageMode();
    } catch (e) {
        console.error('Radio nodes refresh failed:', e);
    }
}

async function _refreshChannels() {
    try {
        const res = await fetch('/api/radio/channels');
        const data = await res.json();
        const channels = data.channels || [];
        latestChannels = channels.slice();

        const select = document.getElementById('message-channel');
        if (select) {
            const current = select.value;
            select.innerHTML = '';

            _sortedChannels(channels).forEach((ch) => {
                const idx = Number.isFinite(Number(ch.index)) ? Number(ch.index) : 0;
                const name = _channelName(ch);
                const opt = document.createElement('option');
                opt.value = String(idx);
                opt.textContent = ch.pinned ? `${name} *` : name;
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

            if ([...select.options].some((o) => o.value === current)) {
                select.value = current;
            }
        }

        _refreshMessageChannelFilterOptions();
        _syncMessageMode();
    } catch (e) {
        console.error('Channel refresh failed:', e);
    }
}

async function _refreshMessages() {
    try {
        const res = await fetch(`/api/messages?limit=${MESSAGE_LIMIT}`);
        const data = await res.json();
        messageState.allMessages = data.messages || [];
        _refreshMessageChannelFilterOptions();
        _renderMessages({ preserveScroll: true });
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

    const to = target?.value || '';
    const targetNode = latestRadioNodes.find((node) => _normalizeNodeId(node.node_id) === _normalizeNodeId(to));
    if (to && _isSelfNode(targetNode)) {
        _setText('msg-status', 'cannot send to self');
        return;
    }
    const isDirect = !!to;
    const channelIndex = isDirect ? 0 : parseInt(channel?.value || '0', 10);
    const channelName = isDirect
        ? 'Direct message'
        : (channel?.selectedOptions?.[0]?.dataset?.channelName || 'Broadcast');

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

function _bindMessageControls() {
    const bindings = [
        ['message-filter-node', 'nodeId'],
        ['message-filter-channel', 'channel'],
        ['message-filter-direction', 'direction'],
        ['message-filter-query', 'query'],
    ];

    bindings.forEach(([id, key]) => {
        const el = document.getElementById(id);
        if (!el) {
            return;
        }
        const eventName = el.tagName === 'INPUT' ? 'input' : 'change';
        el.addEventListener(eventName, () => {
            messageState.filters[key] = el.value.trim();
            _renderMessages({ resetScroll: true });
        });
    });

    const target = document.getElementById('message-target');
    if (target) {
        target.addEventListener('change', _syncMessageMode);
    }

    const followToggle = document.getElementById('message-follow-toggle');
    if (followToggle) {
        followToggle.checked = true;
        followToggle.addEventListener('change', () => {
            messageState.followLatest = followToggle.checked;
            _updateMessageFollowIndicator();
            if (messageState.followLatest) {
                _scrollMessageFeedToBottom();
            }
        });
    }

    const clearBtn = document.getElementById('message-clear-filters');
    if (clearBtn) {
        clearBtn.addEventListener('click', () => {
            messageState.filters = {
                nodeId: '',
                channel: '',
                direction: '',
                query: '',
            };
            _setControlValue('message-filter-node', '');
            _setControlValue('message-filter-channel', '');
            _setControlValue('message-filter-direction', '');
            _setControlValue('message-filter-query', '');
            _renderMessages({ resetScroll: true });
        });
    }

    const purgeVisibleBtn = document.getElementById('message-purge-visible');
    if (purgeVisibleBtn) {
        purgeVisibleBtn.addEventListener('click', async () => {
            const visible = _filteredMessages();
            if (!visible.length) {
                _setText('message-feed-status', 'No matching messages to purge');
                return;
            }
            if (!window.confirm(`Purge ${visible.length} visible messages?`)) {
                return;
            }
            await _purgeMessages(messageState.filters);
        });
    }

    const purgeAllBtn = document.getElementById('message-purge-all');
    if (purgeAllBtn) {
        purgeAllBtn.addEventListener('click', async () => {
            if (!messageState.allMessages.length) {
                _setText('message-feed-status', 'No messages to purge');
                return;
            }
            if (!window.confirm('Purge all message history?')) {
                return;
            }
            await _purgeMessages({});
        });
    }

    const feed = document.getElementById('message-feed');
    if (feed) {
        feed.addEventListener('scroll', () => {
            _updateMessageFollowIndicator();
        });
    }
}

function _bindPacketControls() {
    const packetBindings = [
        ['packet-filter-node', 'nodeId'],
        ['packet-filter-protocol', 'protocol'],
        ['packet-filter-type', 'packetType'],
        ['packet-filter-query', 'query'],
    ];

    packetBindings.forEach(([id, key]) => {
        const el = document.getElementById(id);
        if (!el || !packetFeedInstance) {
            return;
        }
        const eventName = el.tagName === 'INPUT' ? 'input' : 'change';
        el.addEventListener(eventName, () => {
            packetFeedInstance.setFilters({ [key]: el.value.trim() });
        });
    });

    const clearBtn = document.getElementById('packet-clear-filters');
    if (clearBtn) {
        clearBtn.addEventListener('click', () => {
            _setControlValue('packet-filter-node', '');
            _setControlValue('packet-filter-protocol', '');
            _setControlValue('packet-filter-type', '');
            _setControlValue('packet-filter-query', '');
            if (packetFeedInstance) {
                packetFeedInstance.setFilters({
                    nodeId: '',
                    protocol: '',
                    packetType: '',
                    query: '',
                });
            }
        });
    }

    const followToggle = document.getElementById('packet-follow-toggle');
    if (followToggle) {
        followToggle.checked = true;
        followToggle.addEventListener('change', () => {
            if (packetFeedInstance) {
                packetFeedInstance.setFollowLive(followToggle.checked);
            }
        });
    }

    const purgeVisibleBtn = document.getElementById('packet-purge-visible');
    if (purgeVisibleBtn) {
        purgeVisibleBtn.addEventListener('click', async () => {
            if (!packetFeedInstance) {
                return;
            }
            const visible = packetFeedInstance.getVisiblePackets();
            if (!visible.length) {
                _setText('packet-feed-status', 'No matching packets to purge');
                return;
            }
            if (!window.confirm(`Purge ${visible.length} visible packets from history?`)) {
                return;
            }
            await _purgePackets(packetFeedInstance.getFilters());
        });
    }

    const purgeAllBtn = document.getElementById('packet-purge-all');
    if (purgeAllBtn) {
        purgeAllBtn.addEventListener('click', async () => {
            if (!packetFeedInstance || !packetFeedInstance.getTotalCount()) {
                _setText('packet-feed-status', 'No packets to purge');
                return;
            }
            if (!window.confirm('Purge all packet history?')) {
                return;
            }
            await _purgePackets({});
        });
    }
}

function _bindNodeControls(nodeMap, nodeList) {
    document.addEventListener('meshtak:node-selected', (event) => {
        const nodeId = event.detail?.node?.node_id;
        if (nodeId) {
            nodeMap.focusNode(nodeId);
        }
    });

    document.addEventListener('meshtak:node-delete-requested', async (event) => {
        const node = event.detail?.node;
        const nodeId = node?.node_id;
        if (!nodeId) {
            return;
        }
        const label = _nodeShortLabel(node);
        if (!window.confirm(`Delete node ${label} from the list and map?`)) {
            return;
        }
        await _deleteNodes([nodeId], nodeMap, nodeList, `Deleted ${label}`);
    });

    const purgeVisibleBtn = document.getElementById('node-purge-visible');
    if (purgeVisibleBtn) {
        purgeVisibleBtn.addEventListener('click', async () => {
            const visibleNodes = nodeList.getVisibleNodes();
            const nodeIds = visibleNodes
                .map((node) => String(node?.node_id || '').trim())
                .filter(Boolean);
            if (!nodeIds.length) {
                _setText('node-feed-status', 'No visible nodes to purge');
                return;
            }
            if (!window.confirm(`Purge ${nodeIds.length} visible nodes from the list and map?`)) {
                return;
            }
            await _deleteNodes(nodeIds, nodeMap, nodeList, `Purged ${nodeIds.length} visible nodes`);
        });
    }

    const purgeAllBtn = document.getElementById('node-purge-all');
    if (purgeAllBtn) {
        purgeAllBtn.addEventListener('click', async () => {
            const allNodes = _mergeNodeCollections(latestNodes, latestRadioNodes);
            const nodeIds = allNodes
                .map((node) => String(node?.node_id || '').trim())
                .filter(Boolean);
            if (!nodeIds.length) {
                _setText('node-feed-status', 'No nodes to purge');
                return;
            }
            if (!window.confirm(`Purge all ${nodeIds.length} nodes from the list and map?`)) {
                return;
            }
            await _deleteNodes(nodeIds, nodeMap, nodeList, `Purged ${nodeIds.length} nodes`);
        });
    }
}

function _bindDashboardWorkspace(nodeMap) {
    const workspace = document.getElementById('dashboard-workspace');
    if (!workspace) {
        return;
    }
    const storageKey = 'meshtak.dashboard.layout.v1';
    const widgets = Array.from(workspace.querySelectorAll('.dashboard-widget'));
    const state = _loadWidgetState(widgets, storageKey);
    let activeDragId = '';

    const applyState = () => {
        widgets.forEach((widget) => {
            const widgetId = widget.dataset.widgetId;
            const widgetState = state[widgetId];
            if (!widgetState) {
                return;
            }
            widget.style.order = String(widgetState.order);
            widget.style.gridColumn = `span ${widgetState.cols}`;
            widget.style.gridRow = `span ${widgetState.rows}`;
        });
        _saveWidgetState(state, storageKey);
        nodeMap.invalidateSize();
    };

    widgets.forEach((widget) => {
        const widgetId = widget.dataset.widgetId;
        const dragHandle = widget.querySelector('.widget-drag-handle');
        const resizeHandle = widget.querySelector('.widget-resize-handle');

        if (dragHandle) {
            dragHandle.addEventListener('dragstart', (event) => {
                if (window.innerWidth <= 1024) {
                    event.preventDefault();
                    return;
                }
                activeDragId = widgetId;
                widget.classList.add('dashboard-widget--dragging');
                if (event.dataTransfer) {
                    event.dataTransfer.effectAllowed = 'move';
                    event.dataTransfer.setData('text/plain', widgetId);
                }
            });

            dragHandle.addEventListener('dragend', () => {
                activeDragId = '';
                widget.classList.remove('dashboard-widget--dragging');
                widgets.forEach((item) => item.classList.remove('dashboard-widget--drop-target'));
            });
        }

        widget.addEventListener('dragover', (event) => {
            if (!activeDragId || activeDragId === widgetId || window.innerWidth <= 1024) {
                return;
            }
            event.preventDefault();
            widget.classList.add('dashboard-widget--drop-target');
        });

        widget.addEventListener('dragleave', () => {
            widget.classList.remove('dashboard-widget--drop-target');
        });

        widget.addEventListener('drop', (event) => {
            if (!activeDragId || activeDragId === widgetId || window.innerWidth <= 1024) {
                return;
            }
            event.preventDefault();
            widget.classList.remove('dashboard-widget--drop-target');
            const draggedState = state[activeDragId];
            const targetState = state[widgetId];
            if (!draggedState || !targetState) {
                return;
            }
            const order = draggedState.order;
            draggedState.order = targetState.order;
            targetState.order = order;
            applyState();
        });

        if (resizeHandle) {
            let startX = 0;
            let startY = 0;
            let startCols = 0;
            let startRows = 0;

            const stopResizing = () => {
                resizeHandle.classList.remove('is-dragging');
                window.removeEventListener('pointermove', handlePointerMove);
                window.removeEventListener('pointerup', stopResizing);
            };

            const handlePointerMove = (event) => {
                const rect = workspace.getBoundingClientRect();
                const columnGap = 12;
                const rowGap = 12;
                const colWidth = (rect.width - (11 * columnGap)) / 12;
                const rowHeight = 64;
                const deltaCols = Math.round((event.clientX - startX) / Math.max(colWidth + columnGap, 1));
                const deltaRows = Math.round((event.clientY - startY) / Math.max(rowHeight + rowGap, 1));
                const minCols = parseInt(widget.dataset.minCols || '4', 10);
                const minRows = parseInt(widget.dataset.minRows || '4', 10);
                const nextCols = Math.max(minCols, Math.min(12, startCols + deltaCols));
                const nextRows = Math.max(minRows, Math.min(12, startRows + deltaRows));
                state[widgetId].cols = nextCols;
                state[widgetId].rows = nextRows;
                applyState();
            };

            resizeHandle.addEventListener('pointerdown', (event) => {
                if (window.innerWidth <= 1024) {
                    return;
                }
                startX = event.clientX;
                startY = event.clientY;
                startCols = state[widgetId].cols;
                startRows = state[widgetId].rows;
                resizeHandle.classList.add('is-dragging');
                window.addEventListener('pointermove', handlePointerMove);
                window.addEventListener('pointerup', stopResizing, { once: true });
                event.preventDefault();
            });
        }
    });

    applyState();
}

function _loadWidgetState(widgets, storageKey) {
    const defaults = {};
    widgets.forEach((widget) => {
        defaults[widget.dataset.widgetId] = {
            order: parseInt(widget.dataset.defaultOrder || '1', 10),
            cols: parseInt(widget.dataset.defaultCols || '4', 10),
            rows: parseInt(widget.dataset.defaultRows || '4', 10),
        };
    });

    try {
        const raw = window.localStorage.getItem(storageKey);
        if (!raw) {
            return defaults;
        }
        const parsed = JSON.parse(raw);
        Object.keys(defaults).forEach((key) => {
            if (!parsed[key]) {
                parsed[key] = defaults[key];
                return;
            }
            parsed[key].order = Number(parsed[key].order) || defaults[key].order;
            parsed[key].cols = Number(parsed[key].cols) || defaults[key].cols;
            parsed[key].rows = Number(parsed[key].rows) || defaults[key].rows;
        });
        return parsed;
    } catch (_) {
        return defaults;
    }
}

function _saveWidgetState(state, storageKey) {
    try {
        window.localStorage.setItem(storageKey, JSON.stringify(state));
    } catch (_) {}
}

function _syncMessageMode() {
    const target = document.getElementById('message-target');
    const channel = document.getElementById('message-channel');
    if (!target || !channel) {
        return;
    }

    const direct = !!target.value;
    const radioUnavailable = !!target.disabled;
    channel.disabled = radioUnavailable || direct;
    channel.title = direct ? 'Direct messages do not use a broadcast channel' : '';
}

function _renderMessages({ preserveScroll = false, resetScroll = false } = {}) {
    const feed = document.getElementById('message-feed');
    const status = document.getElementById('message-feed-status');
    if (!feed) {
        return;
    }

    const filtered = _filteredMessages();
    const previousHeight = feed.scrollHeight;
    const previousTop = feed.scrollTop;
    const distanceFromBottom = previousHeight - previousTop - feed.clientHeight;
    const nearBottom = _isMessageFeedNearBottom();

    feed.innerHTML = '';

    filtered.forEach((msg) => {
        const row = document.createElement('article');
        row.className = `message-item message-item--${msg.direction || 'rx'}`;

        const endpoint = msg.direction === 'tx'
            ? _messageEndpointLabel(msg.to_name, msg.to_id, 'Broadcast')
            : _messageEndpointLabel(msg.from_name, msg.from_id, 'Unknown');
        const who = msg.direction === 'tx'
            ? `TX to ${endpoint}`
            : `RX from ${endpoint}`;

        const timestamp = msg.timestamp
            ? new Date(msg.timestamp * 1000).toLocaleTimeString()
            : '--';

        const channelLabel = msg.to_id
            ? 'DM'
            : (msg.channel || 'Broadcast');
        const hopPath = msg.hop_path || '--';

        row.innerHTML = `
            <div class="message-head">
                <span>${_escapeHtml(who)}</span>
                <span>${_escapeHtml(timestamp)}</span>
            </div>
            <div class="message-meta">
                <span>Channel: ${_escapeHtml(channelLabel)}</span>
                <span>Hop Path: ${_escapeHtml(hopPath)}</span>
            </div>
            <div class="message-body">${_escapeHtml(msg.text || '')}</div>
        `;

        feed.appendChild(row);
    });

    if (status) {
        status.textContent = filtered.length === messageState.allMessages.length
            ? `${filtered.length} messages`
            : `Showing ${filtered.length} of ${messageState.allMessages.length} messages`;
    }

    if (resetScroll) {
        feed.scrollTop = feed.scrollHeight;
    } else if (messageState.followLatest && nearBottom) {
        feed.scrollTop = feed.scrollHeight;
    } else if (preserveScroll) {
        const nextHeight = feed.scrollHeight;
        const nextTop = Math.max(0, nextHeight - feed.clientHeight - distanceFromBottom);
        feed.scrollTop = nextTop;
        if (previousHeight === 0) {
            feed.scrollTop = nextHeight;
        } else if (Math.abs(distanceFromBottom) < 1) {
            feed.scrollTop = nextHeight;
        } else if (!messageState.followLatest) {
            feed.scrollTop = previousTop;
        }
    }

    _updateMessageFollowIndicator();
}

function _filteredMessages() {
    const { nodeId, channel, direction, query } = messageState.filters;
    const queryText = (query || '').trim().toLowerCase();
    const directionFilter = (direction || '').trim().toLowerCase();
    const channelFilter = (channel || '').trim().toLowerCase();

    return messageState.allMessages.filter((msg) => {
        if (nodeId) {
            const fromId = String(msg.from_id || '').trim();
            const toId = String(msg.to_id || '').trim();
            if (fromId !== nodeId && toId !== nodeId) {
                return false;
            }
        }

        if (channelFilter && String(msg.channel || '').trim().toLowerCase() !== channelFilter) {
            return false;
        }

        if (directionFilter && directionFilter !== 'all' && String(msg.direction || '').trim().toLowerCase() !== directionFilter) {
            return false;
        }

        if (queryText) {
            const haystack = [
                msg.text || '',
                msg.from_name || '',
                msg.to_name || '',
                msg.from_id || '',
                msg.to_id || '',
                msg.channel || '',
                msg.hop_path || '',
            ].join(' ').toLowerCase();
            if (!haystack.includes(queryText)) {
                return false;
            }
        }

        return true;
    });
}

async function _purgeMessages(filters) {
    try {
        const res = await fetch('/api/messages/purge', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                node_id: filters.nodeId || null,
                channel: filters.channel || null,
                direction: filters.direction || null,
                query: filters.query || null,
            }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            throw new Error(data.detail || data.error || 'Unable to purge messages');
        }
        _setText('message-feed-status', `Purged ${data.deleted || 0} messages`);
        await _refreshMessages();
    } catch (err) {
        _setText('message-feed-status', err.message || 'Unable to purge messages');
    }
}

async function _purgePackets(filters) {
    try {
        const res = await fetch('/api/packets/purge', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                node_id: filters.nodeId || null,
                protocol: filters.protocol || null,
                packet_type: filters.packetType || null,
                query: filters.query || null,
            }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            throw new Error(data.detail || data.error || 'Unable to purge packets');
        }

        const packetsRes = await fetch(`/api/packets?limit=${PACKET_LIMIT}`);
        const packets = await packetsRes.json();
        const sorted = (packets || []).slice().sort((a, b) => _packetTimestamp(b) - _packetTimestamp(a));
        if (packetFeedInstance) {
            packetFeedInstance.setPackets(sorted);
        }
        _setText('packet-feed-status', `Purged ${data.deleted || 0} packets`);
        await _updateStats();
    } catch (err) {
        _setText('packet-feed-status', err.message || 'Unable to purge packets');
    }
}

async function _deleteNodes(nodeIds, nodeMap, nodeList, successMessage) {
    try {
        const uniqueNodeIds = Array.from(new Set((nodeIds || []).map((nodeId) => String(nodeId || '').trim()).filter(Boolean)));
        if (!uniqueNodeIds.length) {
            _setText('node-feed-status', 'No nodes selected');
            return;
        }

        let response;
        if (uniqueNodeIds.length === 1) {
            response = await fetch(`/api/nodes/${encodeURIComponent(uniqueNodeIds[0])}`, {
                method: 'DELETE',
            });
        } else {
            response = await fetch('/api/nodes/purge', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ node_ids: uniqueNodeIds }),
            });
        }

        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            throw new Error(data.detail || data.error || 'Unable to delete nodes');
        }

        _setText('node-feed-status', successMessage || `Deleted ${data.deleted || uniqueNodeIds.length} nodes`);
        await _refreshData(nodeMap, nodeList);
        await _refreshRadioUi();
        await _updateStats();
    } catch (error) {
        console.error('Node delete failed:', error);
        _setText('node-feed-status', error.message || 'Unable to delete nodes');
    }
}

function _refreshNodeFilterOptions() {
    const selects = [
        'message-filter-node',
        'packet-filter-node',
    ];
    const mergedNodes = _mergeNodeCollections(latestNodes, latestRadioNodes);
    const sorted = _sortedNodes(mergedNodes);

    selects.forEach((id) => {
        const select = document.getElementById(id);
        if (!select) {
            return;
        }

        const current = select.value;
        select.innerHTML = '<option value="">All nodes</option>';

        sorted.forEach((node) => {
            const nodeId = node.node_id || '';
            if (!nodeId) {
                return;
            }
            const option = document.createElement('option');
            option.value = nodeId;
            option.textContent = _nodeShortLabel(node);
            select.appendChild(option);
        });

        if ([...select.options].some((opt) => opt.value === current)) {
            select.value = current;
        }
    });
}

function _refreshMessageChannelFilterOptions() {
    const select = document.getElementById('message-filter-channel');
    if (!select) {
        return;
    }

    const current = select.value;
    const observed = new Set();

    latestChannels.forEach((channel) => {
        observed.add(_channelName(channel));
    });

    messageState.allMessages.forEach((msg) => {
        if (msg.channel) {
            observed.add(String(msg.channel).trim());
        }
    });

    const names = Array.from(observed)
        .filter(Boolean)
        .sort((a, b) => {
            const aPinned = latestChannels.some((ch) => _channelName(ch) === a && !!ch.pinned) ? -1 : 0;
            const bPinned = latestChannels.some((ch) => _channelName(ch) === b && !!ch.pinned) ? -1 : 0;
            if (aPinned !== bPinned) {
                return aPinned - bPinned;
            }
            return a.localeCompare(b);
        });

    select.innerHTML = '<option value="">All channels</option>';
    names.forEach((name) => {
        const option = document.createElement('option');
        option.value = name;
        option.textContent = name;
        select.appendChild(option);
    });

    if ([...select.options].some((opt) => opt.value === current)) {
        select.value = current;
    }
}

function _mergeNodeCollections(...collections) {
    const merged = new Map();
    collections.flat().forEach((node) => {
        const nodeId = _normalizeNodeId(node?.node_id);
        if (!nodeId) {
            return;
        }
        merged.set(nodeId, { ...(merged.get(nodeId) || {}), ...node, node_id: nodeId });
    });
    return Array.from(merged.values());
}

function _sortedNodes(nodes) {
    return (nodes || [])
        .slice()
        .sort((a, b) => _nodeShortLabel(a).localeCompare(_nodeShortLabel(b)));
}

function _sortedChannels(channels) {
    return (channels || [])
        .slice()
        .sort((a, b) => {
            const aPinned = !!a.pinned;
            const bPinned = !!b.pinned;
            if (aPinned !== bPinned) {
                return aPinned ? -1 : 1;
            }
            const aIndex = Number.isFinite(Number(a.index)) ? Number(a.index) : 999;
            const bIndex = Number.isFinite(Number(b.index)) ? Number(b.index) : 999;
            if (aIndex !== bIndex) {
                return aIndex - bIndex;
            }
            return _channelName(a).localeCompare(_channelName(b));
        });
}

function _nodeShortLabel(node) {
    return node.short_name || node.display_name || node.long_name || node.node_id || 'Unknown';
}

function _nodeTargetLabel(node) {
    if (_isSelfNode(node)) {
        return `${_nodeShortLabel(node)} (self)`;
    }
    return `${_nodeShortLabel(node)} (${_normalizeNodeId(node.node_id)})`;
}

function _messageEndpointLabel(name, nodeId, fallback) {
    const normalizedId = _normalizeNodeId(nodeId);
    if (!normalizedId) {
        return name || fallback;
    }

    const matchedNode = _mergeNodeCollections(latestNodes, latestRadioNodes)
        .find((node) => _normalizeNodeId(node.node_id) === normalizedId);
    const preferredName = String(
        matchedNode?.short_name ||
        matchedNode?.display_name ||
        matchedNode?.long_name ||
        name ||
        ''
    ).trim();

    if (preferredName && _normalizeNodeId(preferredName) !== normalizedId) {
        return `${preferredName} (${normalizedId})`;
    }
    return normalizedId;
}

function _packetNodeLabel(nodeId) {
    const normalizedId = _normalizeNodeId(nodeId);
    if (!normalizedId) {
        return '';
    }
    if (['!ffffffff', '!ffff'].includes(normalizedId)) {
        return 'BCAST';
    }

    const matchedNode = _findNodeForPacketId(nodeId);
    const preferredName = String(
        matchedNode?.short_name ||
        matchedNode?.display_name ||
        matchedNode?.long_name ||
        ''
    ).trim();
    if (preferredName && _normalizeNodeId(preferredName) !== normalizedId) {
        return `${preferredName} (${normalizedId})`;
    }
    return _shortPacketNodeId(normalizedId);
}

function _packetNodeMatches(packetNodeId, filterNodeId) {
    const packetId = _normalizeNodeId(packetNodeId);
    const filterId = _normalizeNodeId(filterNodeId);
    if (!packetId || !filterId) {
        return false;
    }
    if (packetId === filterId) {
        return true;
    }
    const matchedNode = _findNodeForPacketId(packetNodeId);
    return !!matchedNode && _normalizeNodeId(matchedNode.node_id) === filterId;
}

function _findNodeForPacketId(nodeId) {
    const normalizedId = _normalizeNodeId(nodeId);
    if (!normalizedId) {
        return null;
    }
    const normalizedSuffix = normalizedId.replace(/^!/, '');
    const nodes = _mergeNodeCollections(latestNodes, latestRadioNodes);
    return nodes.find((node) => {
        const candidateId = _normalizeNodeId(node.node_id);
        const candidateSuffix = candidateId.replace(/^!/, '');
        const hasUsableSuffix = candidateSuffix.length >= 4 && normalizedSuffix.length >= 4;
        return candidateId === normalizedId ||
            candidateSuffix === normalizedSuffix ||
            (hasUsableSuffix && (
                candidateSuffix.endsWith(normalizedSuffix) ||
                normalizedSuffix.endsWith(candidateSuffix)
            ));
    }) || null;
}

function _shortPacketNodeId(nodeId) {
    const normalizedId = _normalizeNodeId(nodeId);
    if (!normalizedId) {
        return '--';
    }
    const raw = normalizedId.replace(/^!/, '');
    if (['ffffffff', 'ffff'].includes(raw)) {
        return 'BCAST';
    }
    return raw.length > 6 ? `!${raw.slice(-4)}` : normalizedId;
}

function _normalizeNodeId(value) {
    const text = String(value || '').trim();
    if (!text) {
        return '';
    }
    return text.startsWith('!') ? text.toLowerCase() : `!${text.toLowerCase()}`;
}

function _isSelfNode(node) {
    if (!node || !latestDevice) {
        return false;
    }
    const deviceName = String(latestDevice.device_name || '').trim().toLowerCase();
    const nodeNames = [
        node.short_name,
        node.display_name,
        node.long_name,
    ]
        .map((value) => String(value || '').trim().toLowerCase())
        .filter(Boolean);
    if (deviceName && nodeNames.includes(deviceName)) {
        return true;
    }

    const nodeLat = Number(node.latitude ?? node.lat);
    const nodeLon = Number(node.longitude ?? node.lon);
    const deviceLat = Number(latestDevice.latitude);
    const deviceLon = Number(latestDevice.longitude);
    if (
        Number.isFinite(nodeLat) &&
        Number.isFinite(nodeLon) &&
        Number.isFinite(deviceLat) &&
        Number.isFinite(deviceLon)
    ) {
        const latDelta = Math.abs(nodeLat - deviceLat);
        const lonDelta = Math.abs(nodeLon - deviceLon);
        if (latDelta < 0.00001 && lonDelta < 0.00001) {
            return true;
        }
    }

    return false;
}

function _channelName(channel) {
    if (!channel) {
        return 'Broadcast';
    }
    return channel.name || (Number(channel.index) === 0 ? 'Broadcast' : `Channel ${channel.index ?? 0}`);
}

function _isDirectMessageSelected() {
    const target = document.getElementById('message-target');
    return !!(target && target.value);
}

function _isMessageFeedNearBottom() {
    const feed = document.getElementById('message-feed');
    if (!feed) {
        return true;
    }
    return (feed.scrollHeight - feed.scrollTop - feed.clientHeight) <= 24;
}

function _scrollMessageFeedToBottom() {
    const feed = document.getElementById('message-feed');
    if (feed) {
        feed.scrollTop = feed.scrollHeight;
    }
}

function _updateMessageFollowIndicator() {
    const indicator = document.getElementById('message-follow-state');
    if (!indicator) {
        return;
    }
    if (!messageState.followLatest) {
        indicator.textContent = 'manual scroll';
        return;
    }
    indicator.textContent = _isMessageFeedNearBottom() ? 'following latest' : 'paused';
}

function _packetTimestamp(packet) {
    if (!packet) {
        return 0;
    }
    if (packet.rx_time) {
        return Number(packet.rx_time);
    }
    if (packet.timestamp) {
        return Math.floor(new Date(packet.timestamp).getTime() / 1000);
    }
    return 0;
}

function _setControlValue(id, value) {
    const el = document.getElementById(id);
    if (el) {
        el.value = value;
    }
}

function _escapeHtml(str) {
    const el = document.createElement('span');
    el.textContent = String(str || '');
    return el.innerHTML;
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
    const badge = document.getElementById('update-badge');
    if (!badge || !latestAuth.show_update_notices) {
        if (badge) {
            badge.classList.add('hidden');
        }
        return;
    }
    try {
        const res = await fetch('/api/device/update-check');
        const data = await res.json();
        if (data.update_available) {
            badge.classList.remove('hidden');
            badge.title = `Update available (local: ${data.local_version}, remote: ${data.remote_version})`;
        } else {
            badge.classList.add('hidden');
        }
    } catch (_) {}
}

function _bindAuthControls() {
    const loginBtn = document.getElementById('auth-login');
    const logoutBtn = document.getElementById('auth-logout');
    const password = document.getElementById('auth-password');

    if (loginBtn) {
        loginBtn.addEventListener('click', _login);
    }
    if (logoutBtn) {
        logoutBtn.addEventListener('click', _logout);
    }
    if (password) {
        password.addEventListener('keydown', (event) => {
            if (event.key === 'Enter') {
                _login();
            }
        });
    }
}

function _setAuthShellState(authenticated) {
    document.body.classList.toggle('auth-authenticated', authenticated);
    document.body.classList.toggle('auth-unauthenticated', !authenticated);
}

function _bindUserAccountControls() {
    const createBtn = document.getElementById('account-create');
    const refreshBtn = document.getElementById('account-refresh');
    const password = document.getElementById('account-password');

    if (createBtn) {
        createBtn.addEventListener('click', _createUserAccount);
    }
    if (refreshBtn) {
        refreshBtn.addEventListener('click', _loadUserAccounts);
    }
    if (password) {
        password.addEventListener('keydown', (event) => {
            if (event.key === 'Enter') {
                _createUserAccount();
            }
        });
    }
}

async function _refreshAuthStatus() {
    try {
        const response = await fetch('/api/auth/status');
        latestAuth = await response.json();
    } catch (_) {
        latestAuth = { authenticated: false, role: '', username: '', show_update_notices: false };
    }
    _renderAuthState();
    await _loadUserAccounts();
}

async function _login() {
    const username = document.getElementById('auth-username')?.value.trim() || '';
    const password = document.getElementById('auth-password')?.value || '';
    if (!username || !password) {
        _setText('auth-status', 'Enter username and password');
        return;
    }

    try {
        const response = await fetch('/api/auth/login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password }),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            throw new Error(data.detail || 'Login failed');
        }
        latestAuth = data;
        _setText('auth-status', 'Access granted. Launching command board...');
        document.body.classList.add('auth-launching');
        _setValue('auth-password', '');
        _renderAuthState();
        setTimeout(async () => {
            document.body.classList.remove('auth-launching');
            _renderAuthState();
            await _loadUserAccounts();
            _checkForUpdate();
        }, 1050);
    } catch (error) {
        document.body.classList.remove('auth-launching');
        _setText('auth-status', error.message || 'Login failed');
    }
}

async function _logout() {
    try {
        await fetch('/api/auth/logout', { method: 'POST' });
    } catch (_) {}
    latestAuth = { authenticated: false, role: '', username: '', show_update_notices: false };
    document.body.classList.remove('auth-launching');
    _renderAuthState();
    _renderUserAccounts([]);
    _checkForUpdate();
}

function _renderAuthState() {
    const form = document.getElementById('auth-form');
    const logout = document.getElementById('auth-logout');
    const status = document.getElementById('auth-status');
    const sessionStatus = document.getElementById('auth-session-status');
    const radioConfigNav = document.getElementById('radio-config-nav');
    const radioConfigPanel = document.getElementById('radio-config-panel');
    const authenticated = !!latestAuth.authenticated;
    const isAdmin = authenticated && latestAuth.role === 'admin';

    _setAuthShellState(authenticated);
    if (form) {
        form.classList.toggle('hidden', authenticated);
    }
    if (logout) {
        logout.classList.toggle('hidden', !authenticated);
    }
    if (status) {
        status.textContent = authenticated && document.body.classList.contains('auth-launching')
            ? 'Access granted. Launching command board...'
            : authenticated
            ? `${latestAuth.username || 'user'} / ${latestAuth.role || 'user'}`
            : 'Not signed in';
    }
    if (sessionStatus) {
        sessionStatus.textContent = authenticated
            ? `${latestAuth.username || 'user'} / ${latestAuth.role || 'user'}`
            : 'Not signed in';
    }
    if (radioConfigNav) {
        radioConfigNav.classList.toggle('hidden', !isAdmin);
    }
    if (radioConfigPanel) {
        radioConfigPanel.classList.toggle('hidden', !isAdmin);
        radioConfigPanel.classList.toggle('active', false);
    }
    if (!isAdmin && window.location.hash === '#radio-config') {
        history.replaceState(null, '', '#dashboard');
    }
}

async function _loadUserAccounts() {
    const section = document.getElementById('admin-accounts-section');
    const isAdmin = latestAuth.authenticated && latestAuth.role === 'admin';
    if (section) {
        section.classList.toggle('hidden', !isAdmin);
    }
    if (!isAdmin) {
        _renderUserAccounts([]);
        _setText('account-settings-status', 'Admin login required');
        return;
    }

    try {
        const response = await fetch('/api/auth/users');
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            throw new Error(data.detail || 'Unable to load accounts');
        }
        _renderUserAccounts(data.users || []);
        _setText('account-settings-status', 'Accounts loaded');
    } catch (error) {
        _setText('account-settings-status', error.message || 'Unable to load accounts');
    }
}

async function _createUserAccount() {
    const username = document.getElementById('account-username')?.value.trim() || '';
    const password = document.getElementById('account-password')?.value || '';
    const role = document.getElementById('account-role')?.value || 'user';

    if (!latestAuth.authenticated || latestAuth.role !== 'admin') {
        _setText('account-settings-status', 'Admin login required');
        return;
    }
    if (!username || !password) {
        _setText('account-settings-status', 'Enter username and password');
        return;
    }

    try {
        const response = await fetch('/api/auth/users', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password, role }),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            throw new Error(data.detail || 'Unable to create account');
        }
        _setValue('account-username', '');
        _setValue('account-password', '');
        _setValue('account-role', 'user');
        _setText('account-settings-status', `Created ${data.user?.username || username}`);
        await _loadUserAccounts();
    } catch (error) {
        _setText('account-settings-status', error.message || 'Unable to create account');
    }
}

function _renderUserAccounts(users) {
    const list = document.getElementById('account-list');
    if (!list) {
        return;
    }
    list.innerHTML = '';

    if (!users.length) {
        const empty = document.createElement('div');
        empty.className = 'account-list__empty';
        empty.textContent = 'No accounts loaded';
        list.appendChild(empty);
        return;
    }

    users.forEach((user) => {
        const item = document.createElement('div');
        item.className = 'account-item';

        const name = document.createElement('span');
        name.className = 'account-item__name';
        name.textContent = user.username || 'unknown';

        const role = document.createElement('span');
        role.className = user.role === 'admin' ? 'account-item__role account-item__role--admin' : 'account-item__role';
        role.textContent = user.role || 'user';

        const deleteBtn = document.createElement('button');
        deleteBtn.className = 'account-item__delete';
        deleteBtn.type = 'button';
        deleteBtn.textContent = 'Delete';
        deleteBtn.disabled = latestAuth.username === user.username;
        deleteBtn.title = deleteBtn.disabled ? 'You cannot delete the active login' : `Delete ${user.username}`;
        deleteBtn.addEventListener('click', () => _deleteUserAccount(user.username));

        item.appendChild(name);
        item.appendChild(role);
        item.appendChild(deleteBtn);
        list.appendChild(item);
    });
}

async function _deleteUserAccount(username) {
    if (!username) {
        return;
    }
    if (!latestAuth.authenticated || latestAuth.role !== 'admin') {
        _setText('account-settings-status', 'Admin login required');
        return;
    }
    if (latestAuth.username === username) {
        _setText('account-settings-status', 'You cannot delete the active login');
        return;
    }
    if (!confirm(`Delete account "${username}"?`)) {
        return;
    }

    try {
        const response = await fetch(`/api/auth/users/${encodeURIComponent(username)}`, { method: 'DELETE' });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            throw new Error(data.detail || 'Unable to delete account');
        }
        _setText('account-settings-status', `Deleted ${data.deleted || username}`);
        await _loadUserAccounts();
    } catch (error) {
        _setText('account-settings-status', error.message || 'Unable to delete account');
    }
}

function _bindCommandShell(nodeMap) {
    const links = Array.from(document.querySelectorAll('[data-view-link]'));
    const isAdminViewAllowed = (viewName) => viewName !== 'radio-config' || (latestAuth.authenticated && latestAuth.role === 'admin');
    const viewFromHash = () => {
        const hashView = (window.location.hash || '#dashboard').replace('#', '') || 'dashboard';
        return hashView === 'overview' ? 'dashboard' : hashView;
    };
    const showView = (viewName) => {
        const requested = ['dashboard', 'map', 'nodes', 'messages', 'radio-config', 'packets', 'stats'].includes(viewName)
            ? viewName
            : 'dashboard';
        const normalized = isAdminViewAllowed(requested) ? requested : 'dashboard';
        const dashboardMode = normalized === 'dashboard';

        document.querySelectorAll('[data-view]').forEach((view) => {
            const isTarget = dashboardMode || view.dataset.view === normalized;
            const isBlocked = view.dataset.view === 'radio-config' && !isAdminViewAllowed('radio-config');
            view.classList.toggle('active', isTarget && !isBlocked);
        });

        links.forEach((link) => {
            link.classList.toggle('active', link.dataset.viewLink === normalized);
        });

        const workspace = document.querySelector('.workspace');
        if (workspace) {
            workspace.classList.toggle('workspace--dashboard', dashboardMode);
        }

        if (normalized === 'map' || dashboardMode) {
            nodeMap.invalidateSize();
        }
    };

    links.forEach((item) => {
        item.addEventListener('click', (event) => {
            event.preventDefault();
            const viewName = item.dataset.viewLink || 'dashboard';
            const targetView = isAdminViewAllowed(viewName) ? viewName : 'dashboard';
            history.pushState(null, '', `#${targetView}`);
            showView(targetView);
        });
    });

    window.addEventListener('popstate', () => showView(viewFromHash()));
    showView(viewFromHash());
}

function _startCommandClock() {
    const tick = () => {
        const clock = document.getElementById('hdr-clock');
        if (clock) {
            clock.textContent = new Date().toLocaleTimeString();
        }
    };
    tick();
    setInterval(tick, 1000);
}

function _refreshCommandBanner() {
    const banner = document.querySelector('.status-banner');
    if (!banner) {
        return;
    }

    const nodes = _mergeNodeCollections(latestNodes, latestRadioNodes);
    const packetCount = packetFeedInstance?.getTotalCount?.() || _totalPackets || 0;
    const radioConnected = !!latestRadioStatus.connected || latestRadioStatus.status === 'connected';
    let posture = 'Online';

    banner.classList.remove('status-banner--degraded', 'status-banner--offline');

    if (!radioConnected && !nodes.length && !packetCount) {
        posture = 'Offline';
        banner.classList.add('status-banner--offline');
    } else if (!radioConnected) {
        posture = 'Degraded';
        banner.classList.add('status-banner--degraded');
    }

    _setText('network-posture', posture);
}

function _moduleLabel(status, enabled = true) {
    if (enabled === false) {
        return 'Disabled';
    }
    const text = String(status || '').trim().toLowerCase();
    if (!text || text === 'unknown') {
        return 'Unknown';
    }
    if (['running', 'connected', 'online'].includes(text)) {
        return text.charAt(0).toUpperCase() + text.slice(1);
    }
    if (['stopped', 'disconnected', 'offline', 'error'].includes(text)) {
        return text.charAt(0).toUpperCase() + text.slice(1);
    }
    return text.charAt(0).toUpperCase() + text.slice(1);
}

function _renderSystemMessages() {
    const el = document.getElementById('system-message-list');
    if (!el) {
        return;
    }
    const messages = [];
    if (!latestRadioStatus.connected) {
        messages.push('Radio offline.');
    }
    if (latestSettings.collector?.enabled === false || ['stopped', 'error'].includes(String(latestSettings.collector?.status || '').toLowerCase())) {
        messages.push(`Collector ${_moduleLabel(latestSettings.collector?.status, latestSettings.collector?.enabled).toLowerCase()}.`);
    }
    if (!latestSettings.tak?.enabled) {
        messages.push('TAK disabled.');
    }
    el.innerHTML = messages.length
        ? messages.map((message) => `<div>${_escapeHtml(message)}</div>`).join('')
        : 'No system messages.';
}

function _bindRadioConfigControls() {
    const refresh = document.getElementById('radio-config-refresh');
    if (refresh) {
        refresh.addEventListener('click', _loadRadioConfig);
    }

    const reconnect = document.getElementById('radio-config-reconnect');
    if (reconnect) {
        reconnect.addEventListener('click', async () => {
            await _postJson('/api/control/radio/reconnect', {}, 'radio-config-status', 'Radio reconnect requested');
            await _refreshRadioUi();
            await _loadRadioConfig();
        });
    }

    const save = document.getElementById('radio-config-save-floating');
    if (save) {
        save.addEventListener('click', _applyRadioConfig);
    }

    const generate = document.getElementById('private-channel-generate');
    if (generate) {
        generate.addEventListener('click', () => {
            _setValue('private-channel-psk', _generatePskBase64());
            _setText('radio-config-status', 'Generated channel PSK');
        });
    }

    const cliDryRun = document.getElementById('radio-cli-dry-run');
    if (cliDryRun) {
        cliDryRun.addEventListener('click', () => _runRadioCli(true));
    }

    const cliRun = document.getElementById('radio-cli-run');
    if (cliRun) {
        cliRun.addEventListener('click', () => _runRadioCli(false));
    }

    const create = document.getElementById('private-channel-create');
    if (create) {
        create.addEventListener('click', _createPrivateChannel);
    }

    const importButton = document.getElementById('private-channel-import');
    if (importButton) {
        importButton.addEventListener('click', _importPrivateChannel);
    }

    _loadRadioConfig();
}

async function _loadRadioConfig() {
    try {
        const response = await fetch('/api/radio/config');
        const data = await response.json();
        if (!response.ok) {
            throw new Error(data.detail || 'Unable to load radio config');
        }

        const connection = data.connection || {};
        _setText('radio-config-status', data.connected ? 'Radio connected' : 'Radio offline');
        _setText('radio-config-connection', _formatRadioConnection(connection));
        _renderStoredChannels(data.stored_channel_keys || {}, data.channels || []);

        const myInfo = data.radio?.myInfo || {};
        const user = myInfo.my_node_info?.user || myInfo.user || {};
        if (user.longName && !document.getElementById('radio-config-long-name')?.value) {
            _setValue('radio-config-long-name', user.longName);
        }
        if (user.shortName && !document.getElementById('radio-config-short-name')?.value) {
            _setValue('radio-config-short-name', user.shortName);
        }
    } catch (error) {
        _setText('radio-config-status', error.message || 'Unable to load radio config');
    }
}

function _formatRadioConnection(connection) {
    const type = String(connection?.type || 'serial').toLowerCase();
    if (['tcp', 'ip', 'wifi'].includes(type)) {
        return connection?.host ? `IP ${connection.host}` : 'IP radio';
    }
    return connection?.serial_port || connection?.port || '/dev/ttyACM0';
}

function _radioApplyPayload() {
    return {
        long_name: document.getElementById('radio-config-long-name')?.value.trim(),
        short_name: document.getElementById('radio-config-short-name')?.value.trim(),
        region: document.getElementById('radio-config-region')?.value,
        modem_preset: document.getElementById('radio-config-modem')?.value,
        tx_power: document.getElementById('radio-config-tx-power')?.value,
        position_broadcast_secs: document.getElementById('radio-config-position')?.value,
    };
}

async function _applyRadioConfig() {
    const result = await _postJson('/api/radio/config/apply', _radioApplyPayload(), 'radio-config-status', 'Radio config saved');
    if (result) {
        _setRadioOutput(JSON.stringify(result, null, 2));
        await _refreshRadioUi();
        await _loadRadioConfig();
    }
}

async function _runRadioCli(dryRun) {
    const result = await _postJson('/api/radio/config/cli', {
        args: document.getElementById('radio-cli-args')?.value.trim(),
        dry_run: !!dryRun,
    }, 'radio-config-status', dryRun ? 'CLI preview ready' : 'CLI command complete');
    if (result) {
        _setRadioOutput(JSON.stringify(result, null, 2));
        if (!dryRun) {
            await _refreshRadioUi();
            await _loadRadioConfig();
        }
    }
}

function _generatePskBase64() {
    const bytes = new Uint8Array(32);
    crypto.getRandomValues(bytes);
    let binary = '';
    bytes.forEach((byte) => {
        binary += String.fromCharCode(byte);
    });
    return btoa(binary);
}

async function _createPrivateChannel() {
    const payload = {
        name: document.getElementById('private-channel-name')?.value.trim(),
        index: document.getElementById('private-channel-index')?.value,
        psk: document.getElementById('private-channel-psk')?.value.trim(),
        apply_to_radio: !!document.getElementById('private-channel-apply-radio')?.checked,
    };
    if (!payload.psk) {
        payload.psk = _generatePskBase64();
        _setValue('private-channel-psk', payload.psk);
    }
    const result = await _postJson('/api/radio/channels/create-private', payload, 'radio-config-status', 'Private channel saved');
    if (result) {
        _showChannelExport(result);
        await _refreshChannels();
        await _loadRadioConfig();
    }
}

async function _importPrivateChannel() {
    const payload = {
        import_text: document.getElementById('private-channel-import-text')?.value.trim(),
        name: document.getElementById('private-channel-import-name')?.value.trim(),
        index: document.getElementById('private-channel-import-index')?.value,
        psk: document.getElementById('private-channel-import-psk')?.value.trim(),
        apply_to_radio: !!document.getElementById('private-channel-import-apply-radio')?.checked,
    };
    const result = await _postJson('/api/radio/channels/import', payload, 'radio-config-status', 'Private channel imported');
    if (result) {
        _showChannelExport(result);
        await _refreshChannels();
        await _loadRadioConfig();
    }
}

function _renderStoredChannels(storedKeys, bridgeChannels) {
    const list = document.getElementById('radio-channel-list');
    if (!list) {
        return;
    }
    const bridgeByName = new Map((bridgeChannels || []).map((channel) => [String(channel.name || ''), channel]));
    const rows = Object.values(storedKeys || {});
    if (!rows.length) {
        list.textContent = 'No private channels stored.';
        return;
    }
    list.innerHTML = rows.map((row) => {
        const channel = bridgeByName.get(String(row.name || '')) || {};
        const index = channel.index ?? 1;
        const exportData = encodeURIComponent(JSON.stringify({ name: row.name, index, psk: row.psk }));
        return `
            <div class="radio-channel-item">
                <div>
                    <strong>${_escapeHtml(row.name || 'Unnamed')}</strong>
                    <span>Index ${_escapeHtml(index)} | ${_escapeHtml(row.masked || 'stored')}</span>
                </div>
                <button class="action-btn" type="button" data-radio-export="${exportData}">Export</button>
            </div>
        `;
    }).join('');
    list.querySelectorAll('[data-radio-export]').forEach((button) => {
        button.addEventListener('click', () => {
            const data = JSON.parse(decodeURIComponent(button.dataset.radioExport || '{}'));
            _showChannelExport({ export: _buildClientChannelExport(data.name, Number(data.index || 1), data.psk) });
        });
    });
}

function _showChannelExport(result) {
    const exportData = result?.export || {};
    const command = result?.command ? `\n\nRadio CLI output:\n${JSON.stringify(result.command, null, 2)}` : '';
    _setRadioOutput(`MeshTAK import URL:\n${exportData.meshtak_url || ''}\n\nOther-device CLI:\n${exportData.meshtastic_cli || ''}\n\nJSON:\n${JSON.stringify(exportData.json || {}, null, 2)}${command}`);
}

function _buildClientChannelExport(name, index, psk) {
    const payload = { name, index, psk, format: 'meshtak-channel-v1' };
    const encoded = btoa(JSON.stringify(payload)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/g, '');
    return {
        meshtak_url: `meshtak://channel/${encoded}`,
        meshtastic_cli: `meshtastic --ch-index ${index} --ch-set name "${name}" --ch-set psk "${psk}"`,
        json: payload,
    };
}

function _setRadioOutput(value) {
    const output = document.getElementById('radio-config-output');
    if (output) {
        output.value = value || '';
    }
}

function _bindSettingsControls() {
    const drawer = document.getElementById('settings-drawer');
    const backdrop = document.getElementById('settings-backdrop');
    const openBtn = document.getElementById('settings-toggle');
    const closeBtn = document.getElementById('settings-close');

    if (drawer && backdrop && openBtn && closeBtn) {
        const openDrawer = () => {
            drawer.classList.add('open');
            backdrop.classList.add('open');
            drawer.setAttribute('aria-hidden', 'false');
        };
        const closeDrawer = () => {
            drawer.classList.remove('open');
            backdrop.classList.remove('open');
            drawer.setAttribute('aria-hidden', 'true');
        };
        openBtn.addEventListener('click', openDrawer);
        closeBtn.addEventListener('click', closeDrawer);
        backdrop.addEventListener('click', closeDrawer);
        document.addEventListener('keydown', (event) => {
            if (event.key === 'Escape') {
                closeDrawer();
            }
        });
    }

    _bindSettingsPost('tak-save', '/api/settings/tak', () => ({
        enabled: document.getElementById('tak-enabled')?.checked,
        host: document.getElementById('tak-host')?.value.trim(),
        port: parseInt(document.getElementById('tak-port')?.value || '0', 10),
        protocol: document.getElementById('tak-protocol')?.value,
        tls: latestSettings.tak?.tls ?? false,
    }), 'tak-settings-status', 'TAK settings saved');
    _bindSettingsPost('tak-test', '/api/settings/tak/test', () => ({}), 'tak-settings-status', 'TAK test sent');
    _bindCollectorPost('collector-start', '/api/control/collector/start', 'Collector started');
    _bindSettingsPost('collector-stop', '/api/control/collector/stop', () => ({}), 'collector-settings-status', 'Collector stopped');
    _bindCollectorPost('collector-reconnect', '/api/control/collector/reconnect', 'Collector reconnect requested');
    _bindSettingsPost('radio-disconnect', '/api/control/radio/disconnect', () => ({}), 'radio-settings-status', 'Radio disconnected');
    _bindSettingsPost('radio-reconnect', '/api/control/radio/reconnect', () => ({}), 'radio-settings-status', 'Radio reconnect requested');
    _bindSettingsPost('radio-connect', '/api/control/radio/connect', () => ({
        enabled: true,
        type: document.getElementById('radio-type')?.value,
        serial_port: document.getElementById('radio-serial')?.value.trim(),
        host: document.getElementById('radio-host')?.value.trim(),
        port: parseInt(document.getElementById('radio-port')?.value || '0', 10),
    }), 'radio-settings-status', 'Radio connect requested');

    const reload = document.getElementById('system-refresh-settings');
    if (reload) {
        reload.addEventListener('click', _loadSettings);
    }
    _loadSettings();
}

function _bindSettingsPost(buttonId, url, bodyBuilder, statusElId, okText) {
    const button = document.getElementById(buttonId);
    if (!button) {
        return;
    }
    button.addEventListener('click', async () => {
        await _postJson(url, bodyBuilder(), statusElId, okText);
        if (url.includes('/radio/')) {
            await _refreshRadioUi();
        }
    });
}

function _bindCollectorPost(buttonId, url, okText) {
    const button = document.getElementById(buttonId);
    if (!button) {
        return;
    }
    button.addEventListener('click', async () => {
        await _postJson('/api/settings/collector', {
            spi_device: document.getElementById('collector-spi-device')?.value,
        }, 'collector-settings-status', 'Collector device saved');
        await _postJson(url, {}, 'collector-settings-status', okText);
    });
}

async function _loadSettings() {
    try {
        const response = await fetch('/api/settings');
        const data = await response.json();
        latestSettings = data || {};
        const tak = data.tak || {};
        const collector = data.collector || {};
        const radio = data.radio || {};

        _setChecked('tak-enabled', !!tak.enabled);
        _setValue('tak-host', tak.host || '');
        _setValue('tak-port', tak.port || '');
        _setValue('tak-protocol', tak.protocol || 'tcp');
        _setValue('collector-spi-device', collector.spi_device || collector.device || '/dev/spidev0.0');
        _setText('collector-settings-status', collector.status || 'Collector status unknown');
        _setValue('radio-type', radio.type || 'serial');
        _setValue('radio-serial', radio.serial_port || '');
        _setValue('radio-host', radio.host || '');
        _setValue('radio-port', radio.port || '');
        _setText('radio-settings-status', radio.status || 'Radio status unknown');
        _setText('system-settings-status', 'Settings loaded');
    } catch (error) {
        _setText('system-settings-status', 'Unable to load settings');
    }
}

async function _postJson(url, body, statusElId, okText) {
    const statusEl = document.getElementById(statusElId);
    if (statusEl) {
        statusEl.textContent = 'Working...';
    }
    try {
        const response = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body || {}),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
            throw new Error(data.detail || data.error || 'Request failed');
        }
        if (statusEl) {
            statusEl.textContent = okText || 'Saved';
        }
        return data;
    } catch (error) {
        if (statusEl) {
            statusEl.textContent = error.message || 'Request failed';
        }
        return null;
    }
}

function _setValue(id, value) {
    const el = document.getElementById(id);
    if (el) {
        el.value = value;
    }
}

function _setChecked(id, value) {
    const el = document.getElementById(id);
    if (el) {
        el.checked = !!value;
    }
}

function _setText(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
}
