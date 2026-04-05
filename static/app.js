// =========================
// GLOBAL STATE
// =========================
let map;
let markers = {};
let mapInitialized = false;

let takFormDirty = false;
let initialConfigLoaded = false;

// =========================
// INIT
// =========================
document.addEventListener("DOMContentLoaded", () => {
    initMap();
    initTakForm();

    loadInitialConfig();

    // Separate refresh loops
    setInterval(refreshStatus, 5000);
    setInterval(refreshMessages, 3000);
});

// =========================
// MAP
// =========================
function initMap() {
    map = L.map("map").setView([39.5, -98.35], 4);

    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19,
    }).addTo(map);
}

function updateMap(nodes) {
    if (!nodes) return;

    nodes.forEach(node => {
        if (!node.lat || !node.lon) return;

        let id = node.node_id;

        if (markers[id]) {
            markers[id].setLatLng([node.lat, node.lon]);
        } else {
            markers[id] = L.marker([node.lat, node.lon])
                .addTo(map)
                .bindPopup(node.callsign || id);
        }
    });

    if (!mapInitialized && nodes.length > 0) {
        let bounds = nodes
            .filter(n => n.lat && n.lon)
            .map(n => [n.lat, n.lon]);

        if (bounds.length > 0) {
            map.fitBounds(bounds);
            mapInitialized = true;
        }
    }
}

// =========================
// STATUS REFRESH
// =========================
async function refreshStatus() {
    try {
        let res = await fetch("/api/status");
        let data = await res.json();

        updateMap(data.nodes);
        updateNodeTable(data.nodes);
        updateTakStatus(data.tak);

    } catch (e) {
        console.error("Status refresh failed", e);
    }
}

// =========================
// MESSAGE REFRESH
// =========================
async function refreshMessages() {
    try {
        let res = await fetch("/api/messages");
        let data = await res.json();

        renderMessages(data.messages);

    } catch (e) {
        console.error("Message refresh failed", e);
    }
}

// =========================
// NODE TABLE
// =========================
function updateNodeTable(nodes) {
    const table = document.getElementById("nodeTableBody");
    table.innerHTML = "";

    nodes.forEach(n => {
        let row = document.createElement("tr");

        row.innerHTML = `
            <td>${n.callsign || ""}</td>
            <td>${n.node_id || ""}</td>
            <td>${n.lat || ""}</td>
            <td>${n.lon || ""}</td>
            <td>${new Date(n.last_seen * 1000).toLocaleTimeString()}</td>
        `;

        table.appendChild(row);
    });
}

// =========================
// MESSAGES
// =========================
function renderMessages(messages) {
    const container = document.getElementById("messageLog");
    container.innerHTML = "";

    messages.reverse().forEach(msg => {
        let div = document.createElement("div");

        let direction = msg.direction === "rx" ? "RX" : "TX";

        let sender = msg.direction === "rx"
            ? (msg.peer_callsign || msg.peer_node_id)
            : (msg.local_callsign || "ME");

        let target = msg.is_broadcast
            ? "BROADCAST"
            : (msg.destination || "");

        div.className = `message ${msg.direction}`;

        div.innerHTML = `
            <span class="msg-dir">${direction}</span>
            <span class="msg-from">${sender}</span>
            <span class="msg-arrow">→</span>
            <span class="msg-to">${target}</span>
            <span class="msg-text">${msg.text}</span>
        `;

        container.appendChild(div);
    });
}

// =========================
// SEND MESSAGE
// =========================
async function sendMessage() {
    let text = document.getElementById("msgText").value;
    let dest = document.getElementById("msgDest").value || "broadcast";

    await fetch("/api/send-message", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
            text: text,
            destination: dest
        })
    });

    document.getElementById("msgText").value = "";
}

// =========================
// TAK CONFIG
// =========================
function initTakForm() {
    const inputs = document.querySelectorAll("#takForm input");

    inputs.forEach(input => {
        input.addEventListener("input", () => {
            takFormDirty = true;
        });
    });
}

async function loadInitialConfig() {
    try {
        let res = await fetch("/api/config/tak");
        let data = await res.json();

        applyTakConfig(data.tak);
        initialConfigLoaded = true;

    } catch (e) {
        console.error("Failed to load config", e);
    }
}

function applyTakConfig(tak) {
    if (takFormDirty) return;

    document.getElementById("takEnabled").checked = tak.enabled;
    document.getElementById("takHost").value = tak.host;
    document.getElementById("takPort").value = tak.port;
}

async function saveTakConfig() {
    let payload = {
        enabled: document.getElementById("takEnabled").checked,
        host: document.getElementById("takHost").value,
        port: parseInt(document.getElementById("takPort").value)
    };

    let res = await fetch("/api/config/tak", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload)
    });

    let data = await res.json();

    if (data.ok) {
        takFormDirty = false;
        alert("TAK config saved");
    } else {
        alert("Error: " + data.error);
    }
}

function updateTakStatus(tak) {
    const el = document.getElementById("takStatus");
    el.innerText = tak.enabled ? "ENABLED" : "DISABLED";
}
