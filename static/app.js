(() => {
  "use strict";

  const state = {
    map: null,
    markers: new Map(),
    initialMapFitDone: false,
    configDirty: false,
    lastConfigJson: "",
    targets: [],
  };

  const els = {
    statusBadge: document.getElementById("status-badge"),
    settingsToggle: document.getElementById("settings-toggle"),
    settingsDrawer: document.getElementById("settings-drawer"),
    settingsClose: document.getElementById("settings-close"),

    connectionSummary: document.getElementById("connection-summary"),
    takSummary: document.getElementById("tak-summary"),
    statsNodes: document.getElementById("stats-nodes"),
    statsOnline: document.getElementById("stats-online"),
    statsPositions: document.getElementById("stats-positions"),
    statsMessages: document.getElementById("stats-messages"),
    statsQueue: document.getElementById("stats-queue"),

    configForm: document.getElementById("config-form"),
    configConnectionType: document.getElementById("config-connection-type"),
    configSerialRow: document.getElementById("config-serial-row"),
    configTcpRow: document.getElementById("config-tcp-row"),
    configPort: document.getElementById("config-port"),
    configHost: document.getElementById("config-host"),
    configTakEnabled: document.getElementById("config-tak-enabled"),
    configTakHost: document.getElementById("config-tak-host"),
    configTakPort: document.getElementById("config-tak-port"),
    configTakProtocol: document.getElementById("config-tak-protocol"),
    configTakTls: document.getElementById("config-tak-tls"),
    configChannelsJson: document.getElementById("config-channels-json"),
    configSaveBtn: document.getElementById("config-save-btn"),
    configResetBtn: document.getElementById("config-reset-btn"),
    configStatus: document.getElementById("config-status"),
    takCertFields: document.getElementById("tak-cert-fields"),
    takCaCert: document.getElementById("tak-ca-cert"),
    takClientCert: document.getElementById("tak-client-cert"),
    takClientKey: document.getElementById("tak-client-key"),
    takCertStatus: document.getElementById("tak-cert-status"),

    messageForm: document.getElementById("message-form"),
    messageTarget: document.getElementById("message-target"),
    messageText: document.getElementById("message-text"),
    messageStatus: document.getElementById("message-status"),
    messagesList: document.getElementById("messages-list"),
    refreshMessagesBtn: document.getElementById("refresh-messages-btn"),
    refreshNodesBtn: document.getElementById("refresh-nodes-btn"),
    nodesTableBody: document.getElementById("nodes-table-body"),
  };

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function safeValue(value, fallback = "") {
    return value === null || value === undefined ? fallback : String(value);
  }

  function setText(el, value) {
    if (el) el.textContent = value;
  }

  function setBadge(text, cls) {
    if (!els.statusBadge) return;
    els.statusBadge.textContent = text;
    els.statusBadge.className = `badge ${cls || "muted"}`.trim();
  }

  function formatCoord(value) {
    const num = Number(value);
    return Number.isFinite(num) ? num.toFixed(6) : "—";
  }

  function formatEpoch(epoch) {
    if (!epoch) return "—";
    const d = new Date(Number(epoch) * 1000);
    return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString();
  }

  function formatRelativeEpoch(epoch) {
    if (!epoch) return "—";
    const diff = Math.max(0, Math.floor(Date.now() / 1000) - Number(epoch));
    if (diff < 60) return `${diff}s ago`;
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return `${Math.floor(diff / 86400)}d ago`;
  }

  function isNodeOnline(node) {
    const lastHeard = Number(node.last_heard || 0);
    return lastHeard > 0 && (Math.floor(Date.now() / 1000) - lastHeard) <= 300;
  }

  async function apiGet(url) {
    const res = await fetch(url, { headers: { Accept: "application/json" }, cache: "no-store" });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `${url} failed (${res.status})`);
    return data;
  }

  async function apiPost(url, payload) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.ok === false) throw new Error(data.error || `${url} failed (${res.status})`);
    return data;
  }

  async function apiPostForm(url, formData) {
    const res = await fetch(url, { method: "POST", body: formData });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.ok === false) throw new Error(data.error || `${url} failed (${res.status})`);
    return data;
  }

  function toggleDrawer(forceOpen = null) {
    if (!els.settingsDrawer) return;
    const open = forceOpen === null ? els.settingsDrawer.classList.contains("hidden") : !!forceOpen;
    els.settingsDrawer.classList.toggle("hidden", !open);
  }

  function toggleConnectionRows() {
    const type = els.configConnectionType?.value || "serial";
    if (els.configSerialRow) els.configSerialRow.style.display = type === "serial" ? "" : "none";
    if (els.configTcpRow) els.configTcpRow.style.display = type === "tcp" ? "" : "none";
  }

  function toggleTakCertFields() {
    const visible = !!els.configTakTls?.checked;
    if (els.takCertFields) els.takCertFields.style.display = visible ? "grid" : "none";
  }

  function parseChannelsJson() {
    const raw = safeValue(els.configChannelsJson?.value).trim();
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) throw new Error("Channels JSON must be an array");
    return parsed;
  }

  function serializeConfigForm() {
    return {
      connection: {
        type: els.configConnectionType?.value || "serial",
        port: safeValue(els.configPort?.value).trim(),
        host: safeValue(els.configHost?.value).trim(),
      },
      tak: {
        enabled: !!els.configTakEnabled?.checked,
        host: safeValue(els.configTakHost?.value).trim(),
        port: Number(els.configTakPort?.value || 8088),
        protocol: safeValue(els.configTakProtocol?.value || "tcp").trim().toLowerCase(),
        tls: !!els.configTakTls?.checked,
      },
      channels: parseChannelsJson(),
    };
  }

  function markConfigDirty() {
    try {
      state.configDirty = JSON.stringify(serializeConfigForm()) !== state.lastConfigJson;
      if (els.configStatus && state.configDirty) {
        els.configStatus.textContent = "Unsaved changes";
        els.configStatus.className = "form-status dirty";
      }
    } catch (err) {
      if (els.configStatus) {
        els.configStatus.textContent = err.message || "Invalid JSON";
        els.configStatus.className = "form-status error";
      }
    }
  }

  function clearConfigDirty(message = "Loaded") {
    state.lastConfigJson = JSON.stringify(serializeConfigForm());
    state.configDirty = false;
    if (els.configStatus) {
      els.configStatus.textContent = message;
      els.configStatus.className = "form-status clean";
    }
  }

  function applyConfig(config) {
    if (!config) return;
    els.configConnectionType.value = safeValue(config.connection?.type || "serial");
    els.configPort.value = safeValue(config.connection?.port || "");
    els.configHost.value = safeValue(config.connection?.host || "");
    els.configTakEnabled.checked = !!config.tak?.enabled;
    els.configTakHost.value = safeValue(config.tak?.host || "");
    els.configTakPort.value = safeValue(config.tak?.port || 8088);
    els.configTakProtocol.value = safeValue(config.tak?.protocol || "tcp");
    els.configTakTls.checked = !!config.tak?.tls;
    els.configChannelsJson.value = JSON.stringify(config.channels || [], null, 2);
    toggleConnectionRows();
    toggleTakCertFields();
    clearConfigDirty("Loaded");
  }

  function renderStatus(data) {
    const connected = !!data.connected;
    setBadge(connected ? "Connected" : "Disconnected", connected ? "success" : "danger");
    setText(els.connectionSummary, `${safeValue(data.connection_type || "unknown").toUpperCase()} link`);
    setText(els.takSummary, data.tak_enabled ? `${safeValue(data.tak_protocol || "tcp").toUpperCase()} enabled` : "Disabled");
    setText(els.statsNodes, String(data.stats?.node_count ?? 0));
    setText(els.statsOnline, String(data.stats?.online_count ?? 0));
    setText(els.statsPositions, String(data.stats?.position_count ?? 0));
    setText(els.statsMessages, String(data.stats?.message_count ?? 0));
    setText(els.statsQueue, String(data.stats?.queue_count ?? 0));
  }

  function renderTargets(targets) {
    state.targets = targets || [];
    if (!els.messageTarget) return;
    const html = state.targets.map((t, idx) => {
      const label = t.kind === "node" ? `${t.label} • ${t.to}` : t.label;
      return `<option value="${idx}">${escapeHtml(label)}</option>`;
    }).join("");
    els.messageTarget.innerHTML = html || `<option value="">Broadcast</option>`;
  }

  function selectedTarget() {
    const idx = Number(els.messageTarget?.value || 0);
    return state.targets[idx] || { kind: "broadcast", to: null, channel_index: 0, channel_name: "Broadcast" };
  }

  function renderNodes(nodes) {
    if (!els.nodesTableBody) return;
    if (!nodes?.length) {
      els.nodesTableBody.innerHTML = `<tr><td colspan="9" class="empty-state">No nodes yet</td></tr>`;
      return;
    }
    els.nodesTableBody.innerHTML = nodes.map((node) => {
      const online = isNodeOnline(node);
      return `
        <tr>
          <td><div class="node-name">${escapeHtml(node.display_name || node.short_name || node.long_name || node.node_id || "Unknown")}</div></td>
          <td><code>${escapeHtml(node.node_id || "")}</code></td>
          <td>${escapeHtml(node.short_name || "—")}</td>
          <td>${escapeHtml(formatCoord(node.lat))}</td>
          <td>${escapeHtml(formatCoord(node.lon))}</td>
          <td>${escapeHtml(node.batt ?? "—")}</td>
          <td>${escapeHtml(node.snr ?? "—")}</td>
          <td><span class="pill ${online ? "success" : "muted"}">${online ? "Online" : "Stale"}</span></td>
          <td title="${escapeHtml(formatEpoch(node.last_heard))}">${escapeHtml(formatRelativeEpoch(node.last_heard))}</td>
        </tr>`;
    }).join("");
  }

  function renderMessages(messages) {
    if (!els.messagesList) return;
    if (!messages?.length) {
      els.messagesList.innerHTML = `<div class="empty-state">No messages yet</div>`;
      return;
    }
    els.messagesList.innerHTML = messages.map((msg) => `
      <div class="message-card ${escapeHtml(msg.direction || "")}">
        <div class="message-header">
          <span class="pill ${msg.direction === "rx" ? "rx" : "tx"}">${escapeHtml((msg.direction || "?").toUpperCase())}</span>
          <strong>${escapeHtml(msg.from_name || msg.from_id || "Unknown")}</strong>
          <span class="message-meta">→ ${escapeHtml(msg.to_name || msg.to_id || "Broadcast")}</span>
          <span class="message-meta">${escapeHtml(msg.channel || "")}</span>
          <span class="message-time">${escapeHtml(formatEpoch(msg.timestamp || msg.created_at))}</span>
        </div>
        <div class="message-body">${escapeHtml(msg.text || "")}</div>
      </div>`).join("");
  }

  function initMap() {
    const mapEl = document.getElementById("map");
    if (!mapEl || !window.L) return;
    state.map = L.map(mapEl).setView([32.0, -83.0], 6);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: "&copy; OpenStreetMap contributors",
    }).addTo(state.map);
  }

  function markerPopup(node) {
    return `
      <div class="map-popup">
        <div class="map-popup-title">${escapeHtml(node.display_name || node.short_name || node.node_id || "Unknown")}</div>
        <div><code>${escapeHtml(node.node_id || "")}</code></div>
        <div>Lat: ${escapeHtml(formatCoord(node.lat))}</div>
        <div>Lon: ${escapeHtml(formatCoord(node.lon))}</div>
        <div>Battery: ${escapeHtml(node.batt ?? "—")}</div>
        <div>Last heard: ${escapeHtml(formatRelativeEpoch(node.last_heard))}</div>
      </div>`;
  }

  function renderMap(nodes) {
    if (!state.map || !window.L) return;
    const seen = new Set();
    const bounds = [];
    (nodes || []).forEach((node) => {
      const lat = Number(node.lat);
      const lon = Number(node.lon);
      if (!Number.isFinite(lat) || !Number.isFinite(lon)) return;
      seen.add(node.node_id);
      bounds.push([lat, lon]);
      const existing = state.markers.get(node.node_id);
      if (existing) {
        existing.setLatLng([lat, lon]);
        existing.setPopupContent(markerPopup(node));
      } else {
        const marker = L.marker([lat, lon]);
        marker.bindPopup(markerPopup(node));
        marker.addTo(state.map);
        state.markers.set(node.node_id, marker);
      }
    });
    for (const [nodeId, marker] of state.markers.entries()) {
      if (!seen.has(nodeId)) {
        state.map.removeLayer(marker);
        state.markers.delete(nodeId);
      }
    }
    if (!state.initialMapFitDone && bounds.length) {
      state.map.fitBounds(bounds, { padding: [20, 20] });
      state.initialMapFitDone = true;
    }
  }

  async function refreshStatus() { renderStatus(await apiGet("/api/status")); }
  async function refreshNodes() { renderNodes((await apiGet("/api/nodes")).nodes || []); }
  async function refreshMessages() { renderMessages((await apiGet("/api/messages?limit=250")).messages || []); }
  async function refreshMap() { renderMap((await apiGet("/api/map")).nodes || []); }
  async function refreshTargets() { renderTargets((await apiGet("/api/message-targets")).targets || []); }
  async function loadConfig(force = false) {
    if (state.configDirty && !force) return;
    applyConfig(await apiGet("/api/config"));
  }

  async function uploadTakCerts() {
    const formData = new FormData();
    if (els.takCaCert?.files?.[0]) formData.append("ca_cert", els.takCaCert.files[0]);
    if (els.takClientCert?.files?.[0]) formData.append("client_cert", els.takClientCert.files[0]);
    if (els.takClientKey?.files?.[0]) formData.append("client_key", els.takClientKey.files[0]);
    if (![...formData.keys()].length) return;
    setText(els.takCertStatus, "Uploading certs...");
    els.takCertStatus.className = "form-status working";
    await apiPostForm("/api/tak-certs", formData);
    setText(els.takCertStatus, "Certs uploaded");
    els.takCertStatus.className = "form-status clean";
  }

  async function saveConfig(evt) {
    evt.preventDefault();
    try {
      els.configStatus.textContent = "Saving...";
      els.configStatus.className = "form-status working";
      const payload = serializeConfigForm();
      const data = await apiPost("/api/config", payload);
      await uploadTakCerts();
      applyConfig(data.config || payload);
      await Promise.allSettled([refreshStatus(), refreshTargets()]);
      els.configStatus.textContent = "Saved";
      els.configStatus.className = "form-status clean";
    } catch (err) {
      els.configStatus.textContent = err.message || "Save failed";
      els.configStatus.className = "form-status error";
    }
  }

  async function sendMessage(evt) {
    evt.preventDefault();
    const target = selectedTarget();
    const text = safeValue(els.messageText?.value).trim();
    if (!text) {
      els.messageStatus.textContent = "Message text is required";
      els.messageStatus.className = "form-status error";
      return;
    }
    try {
      els.messageStatus.textContent = "Sending...";
      els.messageStatus.className = "form-status working";
      await apiPost("/api/messages/send", {
        text,
        to: target.to,
        channel_index: target.channel_index,
        channel_name: target.channel_name,
      });
      els.messageText.value = "";
      els.messageStatus.textContent = "Queued";
      els.messageStatus.className = "form-status clean";
      await Promise.allSettled([refreshMessages(), refreshStatus()]);
    } catch (err) {
      els.messageStatus.textContent = err.message || "Send failed";
      els.messageStatus.className = "form-status error";
    }
  }

  function bindEvents() {
    els.settingsToggle?.addEventListener("click", () => toggleDrawer());
    els.settingsClose?.addEventListener("click", () => toggleDrawer(false));
    els.configConnectionType?.addEventListener("change", () => { toggleConnectionRows(); markConfigDirty(); });
    els.configTakTls?.addEventListener("change", () => { toggleTakCertFields(); markConfigDirty(); });
    [els.configPort, els.configHost, els.configTakEnabled, els.configTakHost, els.configTakPort, els.configTakProtocol, els.configChannelsJson].forEach((el) => {
      el?.addEventListener("input", markConfigDirty);
      el?.addEventListener("change", markConfigDirty);
    });
    els.configForm?.addEventListener("submit", saveConfig);
    els.configResetBtn?.addEventListener("click", () => loadConfig(true).catch(() => {}));
    els.messageForm?.addEventListener("submit", sendMessage);
    els.refreshMessagesBtn?.addEventListener("click", () => refreshMessages().catch(() => {}));
    els.refreshNodesBtn?.addEventListener("click", async () => { await Promise.allSettled([refreshNodes(), refreshMap(), refreshTargets()]); });
  }

  function schedulePolling() {
    window.setInterval(() => refreshStatus().catch(() => {}), 3000);
    window.setInterval(() => refreshNodes().catch(() => {}), 5000);
    window.setInterval(() => refreshMessages().catch(() => {}), 3000);
    window.setInterval(() => refreshMap().catch(() => {}), 5000);
    window.setInterval(() => refreshTargets().catch(() => {}), 10000);
  }

  async function bootstrap() {
    bindEvents();
    initMap();
    try { await loadConfig(true); } catch (_) {}
    await Promise.allSettled([refreshStatus(), refreshNodes(), refreshMessages(), refreshMap(), refreshTargets()]);
    schedulePolling();
  }

  window.addEventListener("DOMContentLoaded", bootstrap);
})();
