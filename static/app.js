(() => {
  "use strict";

  const state = {
    map: null,
    markers: new Map(),
    configDirty: false,
    lastConfigJson: "",
    initialMapFitDone: false,
    polling: {
      status: null,
      nodes: null,
      messages: null,
      map: null,
      targets: null,
    },
    targets: {
      channels: [],
      nodes: [],
    },
  };

  const els = {
    statusBadge: document.getElementById("status-badge"),
    connectionSummary: document.getElementById("connection-summary"),
    takSummary: document.getElementById("tak-summary"),
    statsNodes: document.getElementById("stats-nodes"),
    statsOnline: document.getElementById("stats-online"),
    statsPositions: document.getElementById("stats-positions"),
    statsMessages: document.getElementById("stats-messages"),
    statsQueue: document.getElementById("stats-queue"),

    settingsToggle: document.getElementById("settings-toggle"),
    settingsClose: document.getElementById("settings-close"),
    settingsDrawer: document.getElementById("settings-drawer"),

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
    configTakVerifyServer: document.getElementById("config-tak-verify-server"),
    takSettingsBlock: document.getElementById("tak-settings-block"),
    takTcpOptions: document.getElementById("tak-tcp-options"),
    takTlsOptions: document.getElementById("tak-tls-options"),
    configSaveBtn: document.getElementById("config-save-btn"),
    configResetBtn: document.getElementById("config-reset-btn"),
    configStatus: document.getElementById("config-status"),

    takCaCertStatus: document.getElementById("tak-ca-cert-status"),
    takClientCertStatus: document.getElementById("tak-client-cert-status"),
    takClientKeyStatus: document.getElementById("tak-client-key-status"),
    takCaCert: document.getElementById("tak-ca-cert"),
    takClientCert: document.getElementById("tak-client-cert"),
    takClientKey: document.getElementById("tak-client-key"),
    takCertUploadBtn: document.getElementById("tak-cert-upload-btn"),
    takCertStatus: document.getElementById("tak-cert-status"),

    nodesTableBody: document.getElementById("nodes-table-body"),
    messagesList: document.getElementById("messages-list"),
    messageForm: document.getElementById("message-form"),
    messageTarget: document.getElementById("message-target"),
    messageText: document.getElementById("message-text"),
    messageStatus: document.getElementById("message-status"),
    refreshMessagesBtn: document.getElementById("refresh-messages-btn"),
    refreshNodesBtn: document.getElementById("refresh-nodes-btn"),
  };

  function setText(el, value) {
    if (el) el.textContent = value;
  }

  function setHtml(el, value) {
    if (el) el.innerHTML = value;
  }

  function setBadge(el, text, className) {
    if (!el) return;
    el.textContent = text;
    el.className = `badge ${className || ""}`.trim();
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function safeValue(value, fallback = "") {
    if (value === null || value === undefined) return fallback;
    return String(value);
  }

  function formatEpoch(epoch) {
    if (!epoch) return "—";
    const d = new Date(Number(epoch) * 1000);
    if (Number.isNaN(d.getTime())) return "—";
    return d.toLocaleString();
  }

  function formatRelativeEpoch(epoch) {
    if (!epoch) return "—";
    const now = Math.floor(Date.now() / 1000);
    const diff = Math.max(0, now - Number(epoch));
    if (diff < 5) return "just now";
    if (diff < 60) return `${diff}s ago`;
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return `${Math.floor(diff / 86400)}d ago`;
  }

  function formatCoord(value) {
    if (value === null || value === undefined || value === "") return "—";
    const num = Number(value);
    if (Number.isNaN(num)) return "—";
    return num.toFixed(6);
  }

  async function apiGet(url) {
    const response = await fetch(url, {
      method: "GET",
      headers: { "Accept": "application/json" },
      cache: "no-store",
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.ok === false) {
      throw new Error(data.error || `Request failed: ${response.status}`);
    }
    return data;
  }

  async function apiPost(url, payload) {
    const response = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Accept": "application/json",
      },
      body: JSON.stringify(payload || {}),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.ok === false) {
      throw new Error(data.error || `Request failed: ${response.status}`);
    }
    return data;
  }

  async function apiPostForm(url, formData) {
    const response = await fetch(url, {
      method: "POST",
      body: formData,
      headers: { "Accept": "application/json" },
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.ok === false) {
      throw new Error(data.error || `Request failed: ${response.status}`);
    }
    return data;
  }

  function openSettings() {
    if (!els.settingsDrawer) return;
    els.settingsDrawer.classList.remove("hidden");
    els.settingsDrawer.setAttribute("aria-hidden", "false");
  }

  function closeSettings() {
    if (!els.settingsDrawer) return;
    els.settingsDrawer.classList.add("hidden");
    els.settingsDrawer.setAttribute("aria-hidden", "true");
  }

  function toggleConnectionRows() {
    const isSerial = safeValue(els.configConnectionType?.value) === "serial";
    els.configSerialRow?.classList.toggle("is-hidden", !isSerial);
    els.configTcpRow?.classList.toggle("is-hidden", isSerial);
  }

  function toggleTakRows() {
    const enabled = !!els.configTakEnabled?.checked;
    const protocol = safeValue(els.configTakProtocol?.value, "udp").toLowerCase();
    const useTls = !!els.configTakTls?.checked;

    els.takSettingsBlock?.classList.toggle("is-hidden", !enabled);
    els.takTcpOptions?.classList.toggle("is-hidden", !enabled || protocol !== "tcp");
    els.takTlsOptions?.classList.toggle("is-hidden", !enabled || protocol !== "tcp" || !useTls);
  }

  function serializeConfigForm() {
    return {
      connection: {
        type: safeValue(els.configConnectionType?.value, "serial").toLowerCase(),
        port: safeValue(els.configPort?.value).trim(),
        host: safeValue(els.configHost?.value).trim(),
      },
      tak: {
        enabled: !!els.configTakEnabled?.checked,
        host: safeValue(els.configTakHost?.value).trim(),
        port: Number(els.configTakPort?.value || 8088),
        protocol: safeValue(els.configTakProtocol?.value, "udp").toLowerCase(),
        tls: !!els.configTakTls?.checked,
        verify_server: !!els.configTakVerifyServer?.checked,
      },
    };
  }

  function updateCertStatus(certs) {
    const renderStatus = (item) => {
      if (!item) return "—";
      return item.present ? `Present · ${item.name || item.path || "file"}` : `Missing · ${item.name || item.path || "file"}`;
    };

    setText(els.takCaCertStatus, renderStatus(certs?.ca_cert));
    setText(els.takClientCertStatus, renderStatus(certs?.client_cert));
    setText(els.takClientKeyStatus, renderStatus(certs?.client_key));
  }

  function applyConfig(config) {
    if (!config) return;

    const connection = config.connection || {};
    const tak = config.tak || {};

    if (els.configConnectionType) els.configConnectionType.value = connection.type || "serial";
    if (els.configPort) els.configPort.value = connection.port || "";
    if (els.configHost) els.configHost.value = connection.host || "";

    if (els.configTakEnabled) els.configTakEnabled.checked = !!tak.enabled;
    if (els.configTakHost) els.configTakHost.value = tak.host || "";
    if (els.configTakPort) els.configTakPort.value = tak.port ?? 8088;
    if (els.configTakProtocol) els.configTakProtocol.value = tak.protocol || "udp";
    if (els.configTakTls) els.configTakTls.checked = !!tak.tls;
    if (els.configTakVerifyServer) els.configTakVerifyServer.checked = !!tak.verify_server;

    toggleConnectionRows();
    toggleTakRows();
    updateCertStatus(config.certs || {});

    state.lastConfigJson = JSON.stringify(serializeConfigForm());
    state.configDirty = false;
    if (els.configStatus) {
      els.configStatus.textContent = "Loaded";
      els.configStatus.className = "form-status clean";
    }
  }

  function markConfigDirty() {
    const current = JSON.stringify(serializeConfigForm());
    const dirty = current !== state.lastConfigJson;
    state.configDirty = dirty;
    if (els.configStatus) {
      els.configStatus.textContent = dirty ? "Unsaved changes" : "Loaded";
      els.configStatus.className = dirty ? "form-status dirty" : "form-status clean";
    }
  }

  async function loadConfig({ force = false } = {}) {
    if (!force && !state.configDirty && state.lastConfigJson) return;
    const data = await apiGet("/api/config");
    applyConfig(data);
  }

  function renderStatus(data) {
    const connected = !!data.connected;
    const takEnabled = !!data.tak_enabled;
    const stats = data.stats || {};

    setBadge(els.statusBadge, connected ? "Connected" : "Disconnected", connected ? "success" : "danger");
    setText(els.connectionSummary, `${safeValue(data.connection_type || "serial").toUpperCase()} · ${connected ? "Online" : "Offline"}`);
    setText(els.takSummary, takEnabled ? `Enabled · Queue ${stats.queue_count ?? 0}` : "Disabled");
    setText(els.statsNodes, stats.node_count ?? 0);
    setText(els.statsOnline, stats.online_count ?? 0);
    setText(els.statsPositions, stats.position_count ?? 0);
    setText(els.statsMessages, stats.message_count ?? 0);
    setText(els.statsQueue, stats.queue_count ?? 0);
  }

  function renderNodes(nodes) {
    if (!els.nodesTableBody) return;

    if (!nodes || nodes.length === 0) {
      setHtml(els.nodesTableBody, '<tr><td colspan="9" class="empty-state">No nodes seen yet.</td></tr>');
      return;
    }

    const rows = nodes.map((node) => {
      const isOnline = node.last_heard && (Math.floor(Date.now() / 1000) - Number(node.last_heard) <= 300);
      const statusClass = isOnline ? "success" : "muted";
      const statusText = isOnline ? "Online" : "Stale";
      return `
        <tr>
          <td>
            <div class="node-name">${escapeHtml(node.display_name || node.short_name || node.node_id || "Unknown")}</div>
            <div class="node-subtle">${escapeHtml(node.long_name || "")}</div>
          </td>
          <td><code>${escapeHtml(node.node_id || "")}</code></td>
          <td>${escapeHtml(node.short_name || "—")}</td>
          <td>${formatCoord(node.lat)}</td>
          <td>${formatCoord(node.lon)}</td>
          <td>${node.batt ?? "—"}</td>
          <td>${node.snr ?? "—"}</td>
          <td><span class="pill ${statusClass}">${statusText}</span></td>
          <td title="${escapeHtml(formatEpoch(node.last_heard))}">${escapeHtml(formatRelativeEpoch(node.last_heard))}</td>
        </tr>
      `;
    });

    setHtml(els.nodesTableBody, rows.join(""));
  }

  function messageTargetLabel(msg) {
    const toName = safeValue(msg.to_name).trim();
    const toId = safeValue(msg.to_id).trim();
    if (!toName && !toId) return "Broadcast";
    return toName || toId;
  }

  function messageSourceLabel(msg) {
    const fromName = safeValue(msg.from_name).trim();
    const fromId = safeValue(msg.from_id).trim();
    return fromName || fromId || "Unknown";
  }

  function renderMessages(messages) {
    if (!els.messagesList) return;

    if (!messages || messages.length === 0) {
      setHtml(els.messagesList, '<div class="empty-state">No messages yet.</div>');
      return;
    }

    const html = messages.slice().reverse().map((msg) => {
      const isRx = msg.direction === "rx";
      const directionClass = isRx ? "rx" : "tx";
      const directionText = isRx ? "RX" : "TX";
      const source = messageSourceLabel(msg);
      const target = messageTargetLabel(msg);
      const meta = isRx ? `From ${escapeHtml(source)} → ${escapeHtml(target)}` : `To ${escapeHtml(target)}`;

      return `
        <div class="message-card ${directionClass}">
          <div class="message-header">
            <span class="pill ${directionClass}">${directionText}</span>
            <span class="message-meta">${meta}</span>
            <span class="message-time" title="${escapeHtml(formatEpoch(msg.timestamp))}">${escapeHtml(formatRelativeEpoch(msg.timestamp))}</span>
          </div>
          <div class="message-body">${escapeHtml(msg.text || "")}</div>
          <div class="message-subtle">
            ${escapeHtml(source)}
            ${msg.channel ? `• ${escapeHtml(msg.channel)}` : ""}
          </div>
        </div>
      `;
    }).join("");

    setHtml(els.messagesList, html);
  }

  function renderTargets(data) {
    state.targets.channels = data.channels || [];
    state.targets.nodes = data.nodes || [];

    if (!els.messageTarget) return;

    const previous = safeValue(els.messageTarget.value);
    const channelOptions = state.targets.channels.map((item) => (
      `<option value="${escapeHtml(item.value)}">${escapeHtml(item.label)}</option>`
    )).join("");
    const nodeOptions = state.targets.nodes.map((item) => (
      `<option value="${escapeHtml(item.value)}">${escapeHtml(item.label)}</option>`
    )).join("");

    setHtml(els.messageTarget, `
      <optgroup label="Channels">${channelOptions}</optgroup>
      <optgroup label="Nodes">${nodeOptions}</optgroup>
    `);

    const allValues = [...state.targets.channels, ...state.targets.nodes].map((item) => item.value);
    if (allValues.includes(previous)) {
      els.messageTarget.value = previous;
    } else if (state.targets.channels[0]) {
      els.messageTarget.value = state.targets.channels[0].value;
    }
  }

  function initMap() {
    if (!window.L) return;
    const mapEl = document.getElementById("map");
    if (!mapEl) return;

    state.map = L.map(mapEl).setView([32.0, -83.0], 6);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: "&copy; OpenStreetMap contributors",
    }).addTo(state.map);
  }

  function markerPopup(node) {
    return `
      <div class="map-popup">
        <div class="map-popup-title">${escapeHtml(node.display_name || node.node_id || "Unknown")}</div>
        <div><code>${escapeHtml(node.node_id || "")}</code></div>
        <div>Lat: ${escapeHtml(formatCoord(node.lat))}</div>
        <div>Lon: ${escapeHtml(formatCoord(node.lon))}</div>
        <div>Alt: ${escapeHtml(node.alt ?? "—")}</div>
        <div>Battery: ${escapeHtml(node.batt ?? "—")}</div>
        <div>Last heard: ${escapeHtml(formatRelativeEpoch(node.last_heard))}</div>
      </div>
    `;
  }

  function renderMap(nodes) {
    if (!state.map || !window.L) return;

    const seen = new Set();
    const bounds = [];

    (nodes || []).forEach((node) => {
      const lat = Number(node.lat);
      const lon = Number(node.lon);
      if (Number.isNaN(lat) || Number.isNaN(lon)) return;

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

    if (!state.initialMapFitDone && bounds.length > 0) {
      state.map.fitBounds(bounds, { padding: [20, 20] });
      state.initialMapFitDone = true;
    }
  }

  function parseSelectedTarget() {
    const raw = safeValue(els.messageTarget?.value).trim();
    if (!raw) return { to: null, channel_index: 0, channel_name: "Default Channel" };
    const [type, value] = raw.split(":", 2);
    if (type === "node") {
      return { to: value || null, channel_index: 0, channel_name: "Direct Message" };
    }
    const selected = state.targets.channels.find((item) => item.value === raw);
    return {
      to: null,
      channel_index: Number(selected?.channel_index ?? 0),
      channel_name: selected?.channel_name || "Default Channel",
    };
  }

  async function refreshStatus() {
    const data = await apiGet("/api/status");
    renderStatus(data);
  }

  async function refreshNodes() {
    const data = await apiGet("/api/nodes");
    renderNodes(data.nodes || []);
  }

  async function refreshMessages() {
    const data = await apiGet("/api/messages?limit=250");
    renderMessages(data.messages || []);
  }

  async function refreshMap() {
    const data = await apiGet("/api/map");
    renderMap(data.nodes || []);
  }

  async function refreshTargets() {
    const data = await apiGet("/api/message-targets");
    renderTargets(data);
  }

  async function sendMessage(evt) {
    evt.preventDefault();
    const text = safeValue(els.messageText?.value).trim();

    if (!text) {
      if (els.messageStatus) {
        els.messageStatus.textContent = "Message text is required";
        els.messageStatus.className = "form-status error";
      }
      return;
    }

    if (els.messageStatus) {
      els.messageStatus.textContent = "Sending...";
      els.messageStatus.className = "form-status working";
    }

    try {
      const target = parseSelectedTarget();
      await apiPost("/api/messages/send", {
        text,
        to: target.to,
        channel_index: target.channel_index,
        channel_name: target.channel_name,
      });

      if (els.messageText) els.messageText.value = "";
      if (els.messageStatus) {
        els.messageStatus.textContent = "Queued";
        els.messageStatus.className = "form-status clean";
      }

      await Promise.allSettled([refreshMessages(), refreshStatus()]);
    } catch (err) {
      if (els.messageStatus) {
        els.messageStatus.textContent = err.message || "Send failed";
        els.messageStatus.className = "form-status error";
      }
    }
  }

  async function uploadTakCerts() {
    const formData = new FormData();
    if (els.takCaCert?.files?.[0]) formData.append("ca_cert", els.takCaCert.files[0]);
    if (els.takClientCert?.files?.[0]) formData.append("client_cert", els.takClientCert.files[0]);
    if (els.takClientKey?.files?.[0]) formData.append("client_key", els.takClientKey.files[0]);

    if ([...formData.keys()].length === 0) {
      if (els.takCertStatus) {
        els.takCertStatus.textContent = "Choose at least one file";
        els.takCertStatus.className = "form-status error";
      }
      return;
    }

    if (els.takCertStatus) {
      els.takCertStatus.textContent = "Uploading...";
      els.takCertStatus.className = "form-status working";
    }

    try {
      const data = await apiPostForm("/api/tak/certs", formData);
      updateCertStatus(data.config?.certs || {});
      if (els.takCaCert) els.takCaCert.value = "";
      if (els.takClientCert) els.takClientCert.value = "";
      if (els.takClientKey) els.takClientKey.value = "";
      if (els.takCertStatus) {
        els.takCertStatus.textContent = data.message || "Uploaded";
        els.takCertStatus.className = "form-status clean";
      }
    } catch (err) {
      if (els.takCertStatus) {
        els.takCertStatus.textContent = err.message || "Upload failed";
        els.takCertStatus.className = "form-status error";
      }
    }
  }

  async function saveConfig(evt) {
    evt.preventDefault();
    if (els.configStatus) {
      els.configStatus.textContent = "Saving...";
      els.configStatus.className = "form-status working";
    }

    try {
      const payload = serializeConfigForm();
      const data = await apiPost("/api/config", payload);
      applyConfig(data.config || payload);
      if (els.configStatus) {
        els.configStatus.textContent = "Saved";
        els.configStatus.className = "form-status clean";
      }
      await refreshStatus();
    } catch (err) {
      if (els.configStatus) {
        els.configStatus.textContent = err.message || "Save failed";
        els.configStatus.className = "form-status error";
      }
    }
  }

  async function resetConfig() {
    try {
      await loadConfig({ force: true });
    } catch (err) {
      if (els.configStatus) {
        els.configStatus.textContent = err.message || "Reload failed";
        els.configStatus.className = "form-status error";
      }
    }
  }

  function bindEvents() {
    els.settingsToggle?.addEventListener("click", openSettings);
    els.settingsClose?.addEventListener("click", closeSettings);
    els.settingsDrawer?.addEventListener("click", (evt) => {
      if (evt.target === els.settingsDrawer) closeSettings();
    });

    document.addEventListener("keydown", (evt) => {
      if (evt.key === "Escape") closeSettings();
    });

    [
      els.configConnectionType,
      els.configPort,
      els.configHost,
      els.configTakEnabled,
      els.configTakHost,
      els.configTakPort,
      els.configTakProtocol,
      els.configTakTls,
      els.configTakVerifyServer,
    ].forEach((el) => {
      if (!el) return;
      el.addEventListener("input", () => {
        toggleConnectionRows();
        toggleTakRows();
        markConfigDirty();
      });
      el.addEventListener("change", () => {
        toggleConnectionRows();
        toggleTakRows();
        markConfigDirty();
      });
    });

    els.configForm?.addEventListener("submit", saveConfig);
    els.configResetBtn?.addEventListener("click", resetConfig);
    els.messageForm?.addEventListener("submit", sendMessage);
    els.refreshMessagesBtn?.addEventListener("click", refreshMessages);
    els.refreshNodesBtn?.addEventListener("click", async () => {
      await Promise.allSettled([refreshNodes(), refreshTargets(), refreshMap()]);
    });
    els.takCertUploadBtn?.addEventListener("click", uploadTakCerts);
  }

  function schedulePolling() {
    clearInterval(state.polling.status);
    clearInterval(state.polling.nodes);
    clearInterval(state.polling.messages);
    clearInterval(state.polling.map);
    clearInterval(state.polling.targets);

    state.polling.status = setInterval(() => void refreshStatus().catch(() => {}), 5000);
    state.polling.nodes = setInterval(() => void refreshNodes().catch(() => {}), 12000);
    state.polling.messages = setInterval(() => void refreshMessages().catch(() => {}), 8000);
    state.polling.map = setInterval(() => void refreshMap().catch(() => {}), 12000);
    state.polling.targets = setInterval(() => void refreshTargets().catch(() => {}), 15000);
  }

  async function bootstrap() {
    initMap();
    bindEvents();

    try {
      await loadConfig({ force: true });
    } catch (err) {
      if (els.configStatus) {
        els.configStatus.textContent = err.message || "Config load failed";
        els.configStatus.className = "form-status error";
      }
    }

    await Promise.allSettled([
      refreshStatus(),
      refreshNodes(),
      refreshMessages(),
      refreshMap(),
      refreshTargets(),
    ]);

    schedulePolling();
  }

  window.addEventListener("DOMContentLoaded", bootstrap);
})();
