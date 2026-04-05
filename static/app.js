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
      `Tracked nodes: ${data.node_count} | Updated: ${new Date(data.timestamp * 1000).toLocaleTimeString()}`;

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
      return;
    }

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
  } catch (err) {
    document.getElementById("serviceStatus").innerHTML =
      `<span class="pill inactive">UI ERROR</span>`;
    document.getElementById("recentErrors").textContent = String(err);
  }
}

refreshStatus();
setInterval(refreshStatus, 5000);
