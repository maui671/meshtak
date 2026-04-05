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

    configForm: document.getElementById("config-form"),
    configConnectionType: document.getElementById("config-connection-type"),
    configSerialRow: document.getElementById("config-serial-row"),
    configTcpRow: document.getElementById("config-tcp-row"),
    configPort: document.getElementById("config-port"),
    configHost: document.getElementById("config-host"),
    configTakEnabled: document.getElementById("config-tak-enabled"),
    configTakHost: document.getElementById("config-tak-host"),
    configTakPort: document.getElementById("config-tak-port"),
    configTakTls: document.getElementById("config-tak-tls"),
    configSaveBtn: document.getElementById("config-save-btn"),
    configResetBtn: document.getElementById("config-reset-btn"),
    configStatus: document.getElementById("config-status"),

    nodesTableBody: document.getElementById("nodes-table-body"),
    messagesList: document.getElementById("messages-list"),
    messageForm: document.getElementById("message-form"),
    messageText: document.getElementById("message-text"),
    messageTo: document.getElementById("message-to"),
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

  function isNodeOnline(node) {
    const lastHeard = Number(node.last_heard || 0);
    const now = Math.floor(Date.now() / 1000);
    return lastHeard > 0 && now - lastHeard <= 300;
  }

  async function apiGet(url) {
    const res = await fetch(url, {
      method: "GET",
      headers: { Accept: "application/json" },
      cache: "no-store",
    });
    if (!res.ok) {
      throw new Error(`GET ${url} failed (${res.status})`);
    }
    return await res.json();
  }

  async function apiPost(url, payload) {
    const res = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
      },
      body: JSON.stringify(payload),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.ok === false) {
      throw new Error(data.error || `POST ${url} failed (${res.status})`);
    }
    return data;
  }

  function toggleConnectionRows() {
    const type = els.configConnectionType?.value || "serial";
    if (els.configSerialRow) {
      els.configSerialRow.style.display = type === "serial" ? "" : "none";
    }
    if (els.configTcpRow) {
      els.configTcpRow.style.display = type === "tcp" ? "" : "none";
    }
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
        tls: !!els.configTakTls?.checked,
      },
    };
  }

  function configPayloadJson() {
    return JSON.stringify(serializeConfigForm());
  }

  function markConfigDirty() {
    const current = configPayloadJson();
    state.configDirty = current !== state.lastConfigJson;
    if (els.configSaveBtn) {
      els.configSaveBtn.disabled = false;
    }
    if (els.configStatus && state.configDirty) {
      els.configStatus.textContent = "Unsaved changes";
      els.configStatus.className = "form-status dirty";
    }
  }

  function clearConfigDirty(message = "Saved") {
    state.lastConfigJson = configPayloadJson();
    state.configDirty = false;
    if (els.configStatus) {
      els.configStatus.textContent = message;
      els.configStatus.className = "form-status clean";
    }
  }

  function applyConfig(config) {
    if (!config) return;

    if (els.configConnectionType) {
      els.configConnectionType.value = safeValue(config.connection?.type || "serial");
    }
    if (els.configPort) {
      els.configPort.value = safeValue(config.connection?.port || "");
    }
    if (els.configHost) {
      els.configHost.value = safeValue(config.connection?.host || "");
    }
    if (els.configTakEnabled) {
      els.configTakEnabled.checked = !!config.tak?.enabled;
    }
    if (els.configTakHost) {
      els.configTakHost.value = safeValue(config.tak?.host || "");
    }
    if (els.configTakPort) {
      els.configTakPort.value = safeValue(config.tak?.port || 8088);
    }
    if (els.configTakTls) {
      els.configTakTls.checked = !!config.tak?.tls;
    }

    toggleConnectionRows();
    clearConfigDirty("Loaded");
  }

  async function loadConfig({ force = false } = {}) {
    if (state.configDirty && !force) {
      return;
    }
    const data = await apiGet("/api/config");
    applyConfig(data);
  }

  function renderStatus(data) {
    const connected = !!data.connected;
    const takEnabled = !!data.tak_enabled;
    const stats = data.stats || {};

    setBadge(
      els.statusBadge,
      connected ? "Connected" : "Disconnected",
      connected ? "success" : "danger"
    );

    setText(
      els.connectionSummary,
      `${safeValue(data.connection_type || "unknown").toUpperCase()} connection`
    );

    setText(
      els.takSummary,
      takEnabled
        ? `TAK enabled • queue ${stats.queue_count ?? 0}`
        : "TAK disabled"
    );

    setText(els.statsNodes, String(stats.node_count ?? 0));
    setText(els.statsOnline, String(stats.online_count ?? 0));
    setText(els.statsPositions, String(stats.position_count ?? 0));
    setText(els.statsMessages, String(stats.message_count ?? 0));
    setText(els.statsQueue, String(stats.queue_count ?? 0));
  }

  function renderNodes(nodes) {
    if (!els.nodesTableBody) return;

    if (!nodes || nodes.length === 0) {
      setHtml(
        els.nodesTableBody,
        `<tr><td colspan="9" class="empty-state">No nodes seen yet.</td></tr>`
      );
      return;
    }

    const rows = nodes.map((node) => {
      const online = isNodeOnline(node);
      const statusClass = online ? "success" : "muted";
      const statusText = online ? "Online" : "Offline";
      return `
        <tr>
          <td>
            <div class="node-name">${escapeHtml(node.display_name || node.node_id || "Unknown")}</div>
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
      setHtml(
        els.messagesList,
        `<div class="empty-state">No messages yet.</div>`
      );
      return;
    }

    const html = messages
      .slice()
      .reverse()
      .map((msg) => {
        const isRx = msg.direction === "rx";
        const directionClass = isRx ? "rx" : "tx";
        const directionText = isRx ? "RX" : "TX";
        const source = messageSourceLabel(msg);
        const target = messageTargetLabel(msg);
        const meta = isRx
          ? `From ${escapeHtml(source)} → ${escapeHtml(target)}`
          : `To ${escapeHtml(target)}`;

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
              ${msg.channel ? `• Channel ${escapeHtml(msg.channel)}` : ""}
            </div>
          </div>
        `;
      })
      .join("");

    setHtml(els.messagesList, html);
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

  async function sendMessage(evt) {
    evt.preventDefault();
    const text = safeValue(els.messageText?.value).trim();
    const to = safeValue(els.messageTo?.value).trim();

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
      await apiPost("/api/messages/send", {
        text,
        to: to || null,
      });

      if (els.messageText) {
        els.messageText.value = "";
      }

      if (els.messageStatus) {
        els.messageStatus.textContent = "Queued";
        els.messageStatus.className = "form-status clean";
      }

      await refreshMessages();
      await refreshStatus();
    } catch (err) {
      if (els.messageStatus) {
        els.messageStatus.textContent = err.message || "Send failed";
        els.messageStatus.className = "form-status error";
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
    if (els.configConnectionType) {
      els.configConnectionType.addEventListener("change", () => {
        toggleConnectionRows();
        markConfigDirty();
      });
    }

    [
      els.configPort,
      els.configHost,
      els.configTakHost,
      els.configTakPort,
      els.configTakEnabled,
      els.configTakTls,
    ].forEach((el) => {
      if (!el) return;
      el.addEventListener("input", markConfigDirty);
      el.addEventListener("change", markConfigDirty);
    });

    if (els.configForm) {
      els.configForm.addEventListener("submit", saveConfig);
    }

    if (els.configResetBtn) {
      els.configResetBtn.addEventListener("click", resetConfig);
    }

    if (els.messageForm) {
      els.messageForm.addEventListener("submit", sendMessage);
    }

    if (els.refreshMessagesBtn) {
      els.refreshMessagesBtn.addEventListener("click", refreshMessages);
    }

    if (els.refreshNodesBtn) {
      els.refreshNodesBtn.addEventListener("click", async () => {
        await refreshNodes();
        await refreshMap();
      });
    }
  }

  function schedulePolling() {
    state.polling.status = window.setInterval(() => {
      refreshStatus().catch(() => {});
    }, 3000);

    state.polling.nodes = window.setInterval(() => {
      refreshNodes().catch(() => {});
    }, 5000);

    state.polling.messages = window.setInterval(() => {
      refreshMessages().catch(() => {});
    }, 3000);

    state.polling.map = window.setInterval(() => {
      refreshMap().catch(() => {});
    }, 5000);
  }

  async function bootstrap() {
    bindEvents();
    initMap();

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
    ]);

    schedulePolling();
  }

  window.addEventListener("DOMContentLoaded", bootstrap);
})();
