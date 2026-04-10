/**
 * Leaflet map with marker clustering for the local Mesh Point dashboard.
 * Displays the Mesh Point device and captured nodes with protocol-colored markers.
 */
class NodeMap {
    constructor(containerId) {
        this._containerId = containerId;
        this._map = null;
        this._markerGroup = null;
        this._deviceMarker = null;
        this._markers = {};
        this._initialized = false;
        this._hasFitBounds = false;
        this._init();
    }

    _init() {
        const el = document.getElementById(this._containerId);
        if (!el) return;

        this._map = L.map(this._containerId, {
            zoomControl: true,
            scrollWheelZoom: true,
        }).setView([39.8, -98.5], 4);

        L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
            attribution: '&copy; CARTO',
            subdomains: 'abcd',
            maxZoom: 19,
        }).addTo(this._map);

        this._markerGroup = L.markerClusterGroup({
            maxClusterRadius: 50,
            disableClusteringAtZoom: 13,
            spiderfyOnMaxZoom: true,
            showCoverageOnHover: false,
            iconCreateFunction: (cluster) => {
                const count = cluster.getChildCount();
                let size = 'small';
                if (count > 50) size = 'large';
                else if (count > 10) size = 'medium';
                return L.divIcon({
                    html: `<div><span>${count}</span></div>`,
                    className: `marker-cluster marker-cluster-${size}`,
                    iconSize: L.point(40, 40),
                });
            },
        });
        this._map.addLayer(this._markerGroup);
        this._initialized = true;
    }

    loadNodes(nodes, device) {
        if (!this._initialized) return;

        this._markerGroup.clearLayers();
        this._markers = {};

        const bounds = [];
        const seenNodeIds = new Set();

        if (device && device.latitude && device.longitude) {
            this._addDeviceMarker(device);
            bounds.push([device.latitude, device.longitude]);
        }

        for (const n of nodes) {
            const nodeId = this._normalizeNodeId(n.node_id);
            if (!nodeId || seenNodeIds.has(nodeId)) continue;
            const lat = n.latitude ?? n.lat;
            const lon = n.longitude ?? n.lon;
            if (lat == null || lon == null) continue;

            seenNodeIds.add(nodeId);
            bounds.push([lat, lon]);
            this._addNodeMarker({ ...n, node_id: nodeId }, lat, lon);
        }

        if (!this._hasFitBounds && bounds.length > 0) {
            if (bounds.length > 1) {
                this._map.fitBounds(bounds, { padding: [40, 40], maxZoom: 14 });
            } else {
                this._map.setView(bounds[0], 13);
            }
            this._hasFitBounds = true;
        }
    }

    focusNode(nodeId) {
        const normalizedId = this._normalizeNodeId(nodeId);
        if (!this._initialized || !normalizedId) {
            return;
        }
        const marker = this._markers[normalizedId];
        if (!marker) {
            return;
        }
        const latLng = marker.getLatLng();
        this._map.flyTo(latLng, Math.max(this._map.getZoom(), 14), {
            animate: true,
            duration: 0.75,
        });
        marker.openPopup();
    }

    invalidateSize() {
        if (this._initialized && this._map) {
            setTimeout(() => this._map.invalidateSize(), 50);
        }
    }

    _addDeviceMarker(device) {
        if (this._deviceMarker) {
            this._map.removeLayer(this._deviceMarker);
        }

        this._deviceMarker = L.marker([device.latitude, device.longitude], {
            icon: L.divIcon({
                html: '<div class="device-marker"></div>',
                className: '',
                iconSize: [16, 16],
                iconAnchor: [8, 8],
            }),
            zIndexOffset: 1000,
        });

        const name = device.device_name || 'Mesh Point';
        this._deviceMarker.bindPopup(
            `<strong>${this._esc(name)}</strong><br>` +
            `Type: Mesh Point<br>` +
            `Lat: ${device.latitude.toFixed(4)}<br>` +
            `Lon: ${device.longitude.toFixed(4)}`
        );

        this._deviceMarker.addTo(this._map);
    }

    _addNodeMarker(n, lat, lon) {
        const protocol = n.protocol || n.via || 'meshtastic';
        const isMeshtastic = protocol === 'meshtastic' || protocol === 'heltec';
        const color = isMeshtastic ? '#5cb6ff' : '#83c4ff';

        const heard = n.last_heard || n.last_seen;
        const heardTs = typeof heard === 'number' ? heard * 1000 : new Date(heard).getTime();
        const isRecent = heard && (Date.now() - heardTs) < 60000;

        const marker = L.circleMarker([lat, lon], {
            radius: 6,
            fillColor: color,
            fillOpacity: 0.8,
            color: isRecent ? '#dff1ff' : color,
            weight: isRecent ? 2 : 1,
            className: isRecent ? 'node-pulse' : '',
        });

        const name = n.short_name || n.display_name || n.long_name || n.name || n.node_id || '--';
        const fullName = n.long_name || n.display_name || n.node_id || '--';
        const rssi = (n.rssi ?? n.latest_rssi) != null
            ? `${Number(n.rssi ?? n.latest_rssi).toFixed(0)} dBm` : '--';

        marker.bindPopup(
            `<strong>${this._esc(name)}</strong><br>` +
            `Name: ${this._esc(fullName)}<br>` +
            `Node: ${this._esc(n.node_id || '--')}<br>` +
            `Protocol: ${this._esc(protocol)}<br>` +
            `RSSI: ${this._esc(rssi)}`
        );
        marker.bindTooltip(this._esc(name), {
            permanent: true,
            direction: 'top',
            offset: [0, -8],
            className: 'node-map-label',
        });

        this._markerGroup.addLayer(marker);
        this._markers[this._normalizeNodeId(n.node_id)] = marker;
    }

    updateFromPacket(packet) {
        if (!packet.source_id || !this._initialized) return;
        const marker = this._markers[this._normalizeNodeId(packet.source_id)];
        if (marker) {
            marker.setStyle({ color: '#dff1ff', weight: 2 });
            this._drawPacketLine(marker);
            setTimeout(() => {
                const proto = (packet.protocol || 'meshtastic') === 'meshtastic' ? '#5cb6ff' : '#83c4ff';
                marker.setStyle({ color: proto, weight: 1 });
            }, 5000);
        }
    }

    _drawPacketLine(sourceMarker) {
        if (!this._deviceMarker) return;
        const deviceLatLng = this._deviceMarker.getLatLng();
        const nodeLatLng = sourceMarker.getLatLng();

        const line = L.polyline([nodeLatLng, deviceLatLng], {
            color: '#00e5a0',
            weight: 2,
            opacity: 0.8,
            dashArray: '6, 4',
            className: 'packet-line',
        }).addTo(this._map);

        let opacity = 0.8;
        const fade = setInterval(() => {
            opacity -= 0.1;
            if (opacity <= 0) {
                clearInterval(fade);
                this._map.removeLayer(line);
            } else {
                line.setStyle({ opacity });
            }
        }, 200);
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
