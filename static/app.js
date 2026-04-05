let map;
let markersLayer;
let hasAutoFit = false;
let userAdjustedMap = false;
let lastMarkerSignature = "";

function ensureMap() {
  if (map) {
    return;
  }

  map = L.map("map", {
    zoomControl: true,
    scrollWheelZoom: true,
    doubleClickZoom: true,
    boxZoom: true,
    keyboard: true,
    dragging: true
  }).setView([32.5, -83.6], 7);

  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "&copy; OpenStreetMap contributors"
  }).addTo(map);

  markersLayer = L.layerGroup().addTo(map);

  const markUserAdjusted = () => {
    userAdjustedMap = true;
  };

  map.on("zoomstart", markUserAdjusted);
  map.on("dragstart", markUserAdjusted);
}

function isValidCoord(lat, lon) {
  return (
    lat !== null &&
    lon !== null &&
    lat !== undefined &&
    lon !== undefined &&
    !Number.isNaN(Number(lat)) &&
    !Number.isNaN(Number(lon)) &&
    Number(lat) >= -90 &&
    Number(lat) <= 90 &&
    Number(lon) >= -180 &&
    Number(lon) <= 180
  );
}

function buildMarkerSignature(nodes) {
  return (nodes || [])
    .filter((node) => isValidCoord(node.lat, node.lon))
    .map((node) => {
      const lat = Number(node.lat).toFixed(6);
      const lon = Number(node.lon).toFixed(6);
      const id = node.node_id || "";
      const seen = node.last_seen || "";
      return `${id}:${lat}:${lon}:${seen}`;
    })
    .sort()
    .join("|");
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatDateTimeFromEpoch(epochSeconds) {
  if (!epochSeconds) {
    return "";
  }

  try {
    return new Date(Number(epochSeconds) * 1000).toLocaleString();
  } catch (_err) {
    return String(epochSeconds);
  }
}

function formatTimeFromEpoch(epochSeconds) {
  if (!epochSeconds) {
    return "";
  }

  try {
    return new Date(Number(epochSeconds) * 1000).toLocaleTimeString();
  } catch (_err) {
    return String(epochSeconds);
  }
}

function updateTakConfigSummary(tak) {
  const resultEl = document.getElementById("takConfigResult");
  if (!resultEl) {
    return;
  }

  if (!tak) {
    resultEl.textContent = "";
    return;
  }

  if (tak.enabled) {
    resultEl.textContent = `TAK enabled: ${tak.host || ""}:${tak.port ?? 8088}`;
  } else {
    resultEl.textContent = "TAK forwarding disabled.";
  }
}

function refreshMap(nodes) {
  ensureMap();
  markersLayer.clearLayers();

  const validNodes = (nodes || []).filter(
    (node) => isValidCoord(node.lat, node.lon)
  );

  if (!validNodes.length) {
    return;
  }

  const bounds = [];
  const markerSignature = buildMarkerSignature(validNodes);

  for (const node of validNodes) {
    const lat = Number(node.lat);
    const lon = Number(node.lon);
    const callsign = node.callsign || node.node_id || "Unknown";
    const nodeId = node.node_id || "";
    const hae = node.hae ?? "";
    const source = node.source || "";
    const lastSeen = formatDateTimeFromEpoch(node.last_seen);
    const uid = node.uid || "";

    const marker = L.marker([lat, lon]);
    marker.bindPopup(`
      <b>${escapeHtml(callsign)}</b><br>
      Node ID: ${escapeHtml(nodeId)}<br>
      Lat/Lon: ${lat}, ${lon}<br>
      HAE: ${escapeHtml(hae)}<br>
      Source: ${escapeHtml(source)}<br>
      Last Seen: ${escapeHtml(lastSeen)}<br>
      UID: ${escapeHtml(uid)}
    `);

    marker.addTo(markersLayer);
    bounds.push([lat, lon]);
  }

  const markersChanged = markerSignature !== lastMarkerSignature;
  lastMarkerSignature = markerSignature;

  if (!hasAutoFit || (!userAdjustedMap && markersChanged)) {
    if (bounds.length === 1) {
      map.setView(bounds[0], 13);
    } else {
      map.fitBounds(bounds, { padding: [30, 30] });
    }
    hasAutoFit = true;
  }
}

async function refreshStatus() {
  try {
    const res = await fetch("/api/status", {
      cache: "no-store"
    });

    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`);
    }

    const data = await res.json();

    const service = (data.service || "unknown").toLowerCase();
    const cls =
      service === "active"
        ? "active"
        : service === "inactive"
          ? "inactive"
          : "unknown";

    const serviceStatusEl = document.getElementById("serviceStatus");
    if (serviceStatusEl) {
      serviceStatusEl.innerHTML =
        `<span class="pill ${cls}">${escapeHtml(service.toUpperCase())}</span>`;
    }

    const nodeCountEl = document.getElementById("nodeCount");
    if (nodeCountEl) {
      nodeCountEl.textContent =
        `Tracked nodes: ${data.node_count ?? 0} | Updated: ${formatTimeFromEpoch(data.timestamp)} | UI Port: ${data.https_port ?? 8443}`;
    }

    const logPathEl = document.getElementById("logPath");
    if (logPathEl) {
      logPathEl.textContent = `Log file: ${data.log_file || ""}`;
    }

    const recentTakEl = document.getElementById("recentTak");
    if (recentTakEl) {
      recentTakEl.textContent =
        data.recent_tak && data.recent_tak.length
          ? data.recent_tak.join("\n")
          : "No TAK pushes yet.";
    }

    const recentErrorsEl = document.getElementById("recentErrors");
    if (recentErrorsEl) {
      recentErrorsEl.textContent =
        data.recent_errors && data.recent_errors.length
          ? data.recent_errors.join("\n")
          : "No recent errors.";
    }

    const recentLogEl = document.getElementById("recentLog");
    if (recentLogEl) {
      recentLogEl.textContent =
        data.recent_log && data.recent_log.length
          ? data.recent_log.join("\n")
          : "No log data yet.";
    }

    const tbody = document.getElementById("nodesTable");
    if (tbody) {
      tbody.innerHTML = "";

      if (!data.nodes || !data.nodes.length) {
        tbody.innerHTML = `<tr><td colspan="8">No nodes observed yet.</td></tr>`;
      } else {
        for (const node of data.nodes) {
          const row = document.createElement("tr");
          row.innerHTML = `
            <td>${escapeHtml(node.callsign || "")}</td>
            <td>${escapeHtml(node.node_id || "")}</td>
            <td>${escapeHtml(node.lat ?? "")}</td>
            <td>${escapeHtml(node.lon ?? "")}</td>
            <td>${escapeHtml(node.hae ?? "")}</td>
            <td>${escapeHtml(node.source || "")}</td>
            <td>${escapeHtml(formatTimeFromEpoch(node.last_seen))}</td>
            <td>${escapeHtml(node.uid || "")}</td>
          `;
          tbody.appendChild(row);
        }
      }
    }

    if (typeof document !== "undefined") {
      const takEnabledEl = document.getElementById("takEnabled");
      const takHostEl = document.getElementById("takHost");
      const takPortEl = document.getElementById("takPort");
      if (data.tak) {
        if (takEnabledEl) takEnabledEl.checked = !!data.tak.enabled;
        if (takHostEl) takHostEl.value = data.tak.host || "";
        if (takPortEl) takPortEl.value = data.tak.port ?? 8088;
        updateTakConfigSummary(data.tak);
      }
    }

    refreshMap(data.nodes || []);
    return data;
  } catch (err) {
    const serviceStatusEl = document.getElementById("serviceStatus");
    if (serviceStatusEl) {
      serviceStatusEl.innerHTML =
        `<span class="pill inactive">UI ERROR</span>`;
    }

    const recentErrorsEl = document.getElementById("recentErrors");
    if (recentErrorsEl) {
      recentErrorsEl.textContent = String(err);
    }

    throw err;
  }
}

ensureMap();
refreshStatus().catch(() => {});
setInterval(() => {
  refreshStatus().catch(() => {});
}, 5000);
