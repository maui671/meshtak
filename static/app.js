let map;
let markersLayer;

function ensureMap() {
  if (map) {
    return;
  }

  map = L.map("map").setView([32.5, -83.6], 7);

  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "&copy; OpenStreetMap contributors"
  }).addTo(map);

  markersLayer = L.layerGroup().addTo(map);
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

  for (const node of validNodes) {
    const lat = Number(node.lat);
    const lon = Number(node.lon);
    const callsign = node.callsign || node.node_id || "Unknown";
    const nodeId = node.node_id || "";
    const hae = node.hae ?? "";
    const source = node.source || "";
    const lastSeen = node.last_seen
      ? new Date(node.last_seen * 1000).toLocaleString()
      : "";

    const marker = L.marker([lat, lon]);
    marker.bindPopup(`
      <b>${callsign}</b><br>
      Node ID: ${nodeId}<br>
      Lat/Lon: ${lat}, ${lon}<br>
      HAE: ${hae}<br>
      Source: ${source}<br>
      Last Seen: ${lastSeen}
    `);

    marker.addTo(markersLayer);
    bounds.push([lat, lon]);
  }

  if (bounds.length === 1) {
    map.setView(bounds[0], 13);
  } else {
    map.fitBounds(bounds, { padding: [30, 30] });
  }
}

async function refreshStatus() {
  try {
    const res = await fetch("/api/status");
    const data = await res.json();

    const service = (data.service || "unknown").toLowerCase();
    const cls =
      service === "active"
        ? "active"
        : service === "inactive"
          ? "inactive"
          : "unknown";

    document.getElementById("serviceStatus").innerHTML =
      `<span class="pill ${cls}">${service.toUpperCase()}</span>`;

    document.getElementById("nodeCount").textContent =
      `Tracked nodes: ${data.node_count} | Updated: ${new Date(data.timestamp * 1000).toLocaleTimeString()} | UI Port: ${data.https_port}`;

    document.getElementById("logPath").textContent =
      `Log file: ${data.log_file}`;

    document.getElementById("recentTak").textContent =
      data.recent_tak && data.recent_tak.length
        ? data.recent_tak.join("\n")
        : "No TAK pushes yet.";

    document.getElementById("recentErrors").textContent =
      data.recent_errors && data.recent_errors.length
        ? data.recent_errors.join("\n")
        : "No recent errors.";

    document.getElementById("recentLog").textContent =
      data.recent_log && data.recent_log.length
        ? data.recent_log.join("\n")
        : "No log data yet.";

    const tbody = document.getElementById("nodesTable");
    tbody.innerHTML = "";

    if (!data.nodes || !data.nodes.length) {
      tbody.innerHTML = `<tr><td colspan="8">No nodes observed yet.</td></tr>`;
    } else {
      for (const node of data.nodes) {
        const lastSeen = node.last_seen
          ? new Date(node.last_seen * 1000).toLocaleTimeString()
          : "";

        const row = document.createElement("tr");
        row.innerHTML = `
          <td>${node.callsign || ""}</td>
          <td>${node.node_id || ""}</td>
          <td>${node.lat ?? ""}</td>
          <td>${node.lon ?? ""}</td>
          <td>${node.hae ?? ""}</td>
          <td>${node.source || ""}</td>
          <td>${lastSeen}</td>
          <td>${node.uid || ""}</td>
        `;
        tbody.appendChild(row);
      }
    }

    refreshMap(data.nodes || []);
  } catch (err) {
    document.getElementById("serviceStatus").innerHTML =
      `<span class="pill inactive">UI ERROR</span>`;
    document.getElementById("recentErrors").textContent = String(err);
  }
}

ensureMap();
refreshStatus();
setInterval(refreshStatus, 5000);
