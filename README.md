# Meshtak
**Meshtastic → TAK Gateway**

Meshtak is a lightweight Python service that bridges **Meshtastic mesh position data**
to **TAK (Team Awareness Kit)** by converting Meshtastic packets into
**Cursor-on-Target (CoT)** events and forwarding them to a TAK server over UDP.

It runs as a **systemd service** and supports both **APT-based** and **DNF-based**
Linux distributions.

---

## What Meshtak Does

- Connects to a Meshtastic node via TCP
- Subscribes to Meshtastic pubsub events
- Tracks node callsigns and positions
- Rate-limits position updates per node
- Converts positions into CoT XML
- Sends CoT events to a TAK server via UDP

TAK clients (ATAK / WinTAK) then display Meshtastic nodes as live map objects.

---

## Architecture Overview

```mermaid
flowchart LR
    Mesh[Meshtastic Mesh Network]
    Node[Meshtastic TCP Node]
    Gateway[Python Functions]
    TAK[TAK Server]
    Clients[ATAK and WinTAK Clients]

    Mesh --> Node
    Node --> Gateway
    Gateway --> TAK
    TAK --> Clients
