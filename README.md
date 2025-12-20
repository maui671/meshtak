# Meshtak  
**Meshtastic → TAK Gateway**

Meshtak is a lightweight Python service that bridges **Meshtastic mesh position data**
to **TAK (Team Awareness Kit)** by converting Meshtastic packets into
**Cursor-on-Target (CoT)** events and forwarding them to a TAK server over UDP.

It is designed to run as a **systemd service** on Linux systems and supports both
**APT-based** and **DNF-based** distributions.

---

## What Meshtak Does

- Connects to a Meshtastic node via **TCP**
- Listens for Meshtastic pubsub events
- Tracks node callsigns and positions
- Rate-limits updates per node
- Converts position data into **CoT XML**
- Sends CoT events to a TAK server via **UDP**

TAK clients (ATAK / WinTAK) then see Meshtastic nodes as live map objects.

---

## Architecture Overview

```mermaid
flowchart LR
    Mesh[Meshtastic Mesh Network]
    Node[Meshtastic TCP Node]
    Gateway[Meshtak Gateway<br/>meshtak.py]
    TAK[TAK Server]
    Clients[ATAK / WinTAK Clients]

    Mesh --> Node
    Node -->|TCP 4403| Gateway
    Gateway -->|UDP CoT 8087| TAK
    TAK --> Clients
# meshtak
