#!/usr/bin/env python3
import copy
import json
import os
import re
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", errors="replace")
        except Exception:
            return repr(value)

    if isinstance(value, bytearray):
        try:
            return bytes(value).decode("utf-8", errors="replace")
        except Exception:
            return repr(value)

    if isinstance(value, dict):
        safe: Dict[str, Any] = {}
        for k, v in value.items():
            safe[str(k)] = _json_safe(v)
        return safe

    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]

    try:
        json.dumps(value)
        return value
    except Exception:
        return repr(value)


class JsonFileStore:
    def __init__(self, path: str, default_data: Any):
        self.path = path
        self.default_data = copy.deepcopy(default_data)
        self.lock = threading.RLock()
        self._ensure_parent_dir()
        self._ensure_file()

    def _ensure_parent_dir(self) -> None:
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    def _ensure_file(self) -> None:
        if not os.path.exists(self.path):
            self.write(copy.deepcopy(self.default_data))
            return

        try:
            self.read()
        except Exception:
            self.write(copy.deepcopy(self.default_data))

    def read(self) -> Any:
        with self.lock:
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data
            except FileNotFoundError:
                data = copy.deepcopy(self.default_data)
                self.write(data)
                return data
            except json.JSONDecodeError:
                data = copy.deepcopy(self.default_data)
                self.write(data)
                return data

    def write(self, data: Any) -> None:
        with self.lock:
            self._ensure_parent_dir()
            safe_data = _json_safe(data)
            fd, tmp_path = tempfile.mkstemp(
                prefix=".meshtak-",
                suffix=".tmp",
                dir=os.path.dirname(self.path) or ".",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
                    json.dump(safe_data, tmp_file, indent=2, sort_keys=False)
                    tmp_file.flush()
                    os.fsync(tmp_file.fileno())
                os.replace(tmp_path, self.path)
            finally:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)

    def update(self, updater):
        with self.lock:
            data = self.read()
            new_data = updater(copy.deepcopy(data))
            self.write(new_data)
            return new_data


class NodeStore:
    def __init__(
        self,
        nodes_path: str,
        messages_path: str,
        queue_path: str,
        max_messages: int = 500,
        max_queue: int = 500,
    ):
        self.nodes_store = JsonFileStore(nodes_path, default_data={})
        self.messages_store = JsonFileStore(messages_path, default_data=[])
        self.queue_store = JsonFileStore(queue_path, default_data=[])
        self.max_messages = max_messages
        self.max_queue = max_queue

    @staticmethod
    def _now_ts() -> int:
        return int(time.time())

    @staticmethod
    def _normalize_text(value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()

    @staticmethod
    def _safe_float(value: Any) -> Optional[float]:
        try:
            if value is None or value == "":
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _safe_int(value: Any) -> Optional[int]:
        try:
            if value is None or value == "":
                return None
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _normalize_node_id(value: Any) -> str:
        if value is None:
            return ""

        if isinstance(value, int):
            return f"!{value & 0xFFFFFFFF:08x}"

        node_id = str(value).strip()
        if not node_id:
            return ""

        # BROADCAST HANDLING (FIX)
        if node_id.lower() in {
            "broadcast",
            "all",
            "*",
            "none",
            "null",
            "undefined",
            "any",
            "everyone",
        }:
            return ""

        if node_id.startswith("!"):
            node_id = node_id[1:].strip()

        if not node_id:
            return ""

        if node_id.lower().startswith("0x"):
            try:
                return f"!{int(node_id, 16) & 0xFFFFFFFF:08x}"
            except Exception:
                pass

        if re.fullmatch(r"[0-9a-fA-F]{8}", node_id):
            return f"!{node_id.lower()}"

        if re.fullmatch(r"\d+", node_id):
            try:
                return f"!{int(node_id, 10) & 0xFFFFFFFF:08x}"
            except Exception:
                pass

        cleaned = re.sub(r"[^0-9a-zA-Z]+", "", node_id).lower()
        if re.fullmatch(r"[0-9a-f]{8}", cleaned):
            return f"!{cleaned}"

        return f"!{node_id.lower()}"

    @staticmethod
    def _best_text(*values: Any) -> str:
        for value in values:
            text = str(value or "").strip()
            if text:
                return text
        return ""

    def _preferred_display_name(self, node: Dict[str, Any]) -> str:
        return self._best_text(
            node.get("short_name"),
            node.get("long_name"),
            node.get("display_name"),
            node.get("node_id"),
        )

    def _merge_nodes(self, base: Dict[str, Any], incoming: Dict[str, Any], node_id: str) -> Dict[str, Any]:
        now = self._now_ts()

        if not isinstance(base, dict):
            base = {}
        if not isinstance(incoming, dict):
            incoming = {}

        merged = {
            "node_id": node_id,
            "long_name": self._best_text(incoming.get("long_name"), base.get("long_name")),
            "short_name": self._best_text(incoming.get("short_name"), base.get("short_name")),
            "display_name": "",
            "hw_model": self._best_text(incoming.get("hw_model"), base.get("hw_model")),
            "role": self._best_text(incoming.get("role"), base.get("role")),
            "lat": incoming.get("lat") if incoming.get("lat") is not None else base.get("lat"),
            "lon": incoming.get("lon") if incoming.get("lon") is not None else base.get("lon"),
            "alt": incoming.get("alt") if incoming.get("alt") is not None else base.get("alt"),
            "batt": incoming.get("batt") if incoming.get("batt") is not None else base.get("batt"),
            "snr": incoming.get("snr") if incoming.get("snr") is not None else base.get("snr"),
            "rssi": incoming.get("rssi") if incoming.get("rssi") is not None else base.get("rssi"),
            "hop_limit": incoming.get("hop_limit") if incoming.get("hop_limit") is not None else base.get("hop_limit"),
            "via": self._best_text(incoming.get("via"), base.get("via")),
            "last_heard": incoming.get("last_heard") if incoming.get("last_heard") is not None else base.get("last_heard"),
            "created_at": min(
                [v for v in [base.get("created_at"), incoming.get("created_at"), now] if isinstance(v, int)]
            ),
            "updated_at": max(
                [v for v in [base.get("updated_at"), incoming.get("updated_at"), now] if isinstance(v, int)]
            ),
            "raw": incoming.get("raw") if incoming.get("raw") not in (None, {}) else base.get("raw", {}),
        }

        if merged.get("last_heard") is None:
            merged["last_heard"] = now

        merged["display_name"] = self._preferred_display_name(merged)
        return merged

    def _dedupe_nodes_dict(self, nodes: Any) -> Dict[str, Dict[str, Any]]:
        if not isinstance(nodes, dict):
            return {}

        deduped: Dict[str, Dict[str, Any]] = {}

        for raw_key, raw_node in nodes.items():
            if not isinstance(raw_node, dict):
                raw_node = {}

            candidate_id = (
                raw_node.get("node_id")
                or raw_node.get("id")
                or raw_node.get("from_id")
                or raw_key
            )
            canonical_id = self._normalize_node_id(candidate_id)
            if not canonical_id:
                continue

            normalized_node = copy.deepcopy(raw_node)
            normalized_node["node_id"] = canonical_id
            normalized_node["long_name"] = self._normalize_text(normalized_node.get("long_name"))
            normalized_node["short_name"] = self._normalize_text(normalized_node.get("short_name"))
            normalized_node["hw_model"] = self._normalize_text(normalized_node.get("hw_model"))
            normalized_node["role"] = self._normalize_text(normalized_node.get("role"))
            normalized_node["via"] = self._normalize_text(normalized_node.get("via"))
            normalized_node["lat"] = self._safe_float(normalized_node.get("lat"))
            normalized_node["lon"] = self._safe_float(normalized_node.get("lon"))
            normalized_node["alt"] = self._safe_float(normalized_node.get("alt"))
            normalized_node["batt"] = self._safe_int(normalized_node.get("batt"))
            normalized_node["snr"] = self._safe_float(normalized_node.get("snr"))
            normalized_node["rssi"] = self._safe_int(normalized_node.get("rssi"))
            normalized_node["hop_limit"] = self._safe_int(normalized_node.get("hop_limit"))
            normalized_node["last_heard"] = self._safe_int(normalized_node.get("last_heard"))
            normalized_node["created_at"] = self._safe_int(normalized_node.get("created_at")) or self._now_ts()
            normalized_node["updated_at"] = self._safe_int(normalized_node.get("updated_at")) or self._now_ts()
            normalized_node["raw"] = _json_safe(normalized_node.get("raw", {}))

            existing = deduped.get(canonical_id, {})
            deduped[canonical_id] = self._merge_nodes(existing, normalized_node, canonical_id)

        for node_id, node in deduped.items():
            node["display_name"] = self._preferred_display_name(node)
            deduped[node_id] = node

        return deduped

    def _read_nodes_deduped(self) -> Dict[str, Dict[str, Any]]:
        nodes = self.nodes_store.read()
        deduped = self._dedupe_nodes_dict(nodes)
        if deduped != nodes:
            self.nodes_store.write(deduped)
        return deduped

    def upsert_node(
        self,
        node_id: Any,
        *,
        long_name: Optional[str] = None,
        short_name: Optional[str] = None,
        hw_model: Optional[str] = None,
        role: Optional[str] = None,
        lat: Optional[Any] = None,
        lon: Optional[Any] = None,
        alt: Optional[Any] = None,
        batt: Optional[Any] = None,
        snr: Optional[Any] = None,
        rssi: Optional[Any] = None,
        hop_limit: Optional[Any] = None,
        via: Optional[str] = None,
        last_heard: Optional[Any] = None,
        raw: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        node_id_norm = self._normalize_node_id(node_id)
        if not node_id_norm:
            raise ValueError("node_id is required")

        def updater(nodes: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
            nodes = self._dedupe_nodes_dict(nodes)
            existing = nodes.get(node_id_norm, {})
            now = self._now_ts()

            if not isinstance(existing, dict):
                existing = {}

            node = {
                "node_id": node_id_norm,
                "long_name": existing.get("long_name", ""),
                "short_name": existing.get("short_name", ""),
                "display_name": existing.get("display_name", node_id_norm),
                "hw_model": existing.get("hw_model", ""),
                "role": existing.get("role", ""),
                "lat": existing.get("lat"),
                "lon": existing.get("lon"),
                "alt": existing.get("alt"),
                "batt": existing.get("batt"),
                "snr": existing.get("snr"),
                "rssi": existing.get("rssi"),
                "hop_limit": existing.get("hop_limit"),
                "via": existing.get("via", ""),
                "last_heard": existing.get("last_heard"),
                "created_at": existing.get("created_at", now),
                "updated_at": now,
                "raw": existing.get("raw", {}),
            }

            if long_name is not None:
                node["long_name"] = self._normalize_text(long_name)
            if short_name is not None:
                node["short_name"] = self._normalize_text(short_name)
            if hw_model is not None:
                node["hw_model"] = self._normalize_text(hw_model)
            if role is not None:
                node["role"] = self._normalize_text(role)
            if via is not None:
                node["via"] = self._normalize_text(via)
            if raw is not None:
                node["raw"] = _json_safe(raw if isinstance(raw, dict) else {"value": raw})

            lat_f = self._safe_float(lat)
            lon_f = self._safe_float(lon)
            alt_f = self._safe_float(alt)
            batt_i = self._safe_int(batt)
            snr_f = self._safe_float(snr)
            rssi_i = self._safe_int(rssi)
            hop_i = self._safe_int(hop_limit)
            heard_i = self._safe_int(last_heard)

            if lat_f is not None:
                node["lat"] = lat_f
            if lon_f is not None:
                node["lon"] = lon_f
            if alt_f is not None:
                node["alt"] = alt_f
            if batt_i is not None:
                node["batt"] = batt_i
            if snr_f is not None:
                node["snr"] = snr_f
            if rssi_i is not None:
                node["rssi"] = rssi_i
            if hop_i is not None:
                node["hop_limit"] = hop_i
            if heard_i is not None:
                node["last_heard"] = heard_i
            elif node.get("last_heard") is None:
                node["last_heard"] = now

            node["display_name"] = self._preferred_display_name(node)
            nodes[node_id_norm] = node
            return self._dedupe_nodes_dict(nodes)

        nodes = self.nodes_store.update(updater)
        return copy.deepcopy(nodes[node_id_norm])

    def get_node(self, node_id: Any) -> Optional[Dict[str, Any]]:
        node_id_norm = self._normalize_node_id(node_id)
        if not node_id_norm:
            return None
        nodes = self._read_nodes_deduped()
        node = nodes.get(node_id_norm)
        return copy.deepcopy(node) if isinstance(node, dict) else None

    def get_nodes(self) -> List[Dict[str, Any]]:
        nodes = self._read_nodes_deduped()
        if not isinstance(nodes, dict):
            return []

        items = []
        for node_id, node in nodes.items():
            if not isinstance(node, dict):
                continue
            entry = copy.deepcopy(node)
            entry["node_id"] = self._normalize_node_id(entry.get("node_id") or node_id)
            entry["display_name"] = self._preferred_display_name(entry)
            items.append(entry)

        items.sort(
            key=lambda n: (
                -(n.get("last_heard") or 0),
                str(n.get("display_name") or n.get("node_id") or ""),
            )
        )
        return items

    def add_message(
        self,
        *,
        direction: str,
        text: Any,
        from_id: Optional[Any] = None,
        from_name: Optional[str] = None,
        to_id: Optional[Any] = None,
        to_name: Optional[str] = None,
        channel: Optional[str] = None,
        message_id: Optional[Any] = None,
        acked: bool = False,
        rx_timestamp: Optional[Any] = None,
        raw: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        direction = self._normalize_text(direction).lower()
        if direction not in {"rx", "tx"}:
            raise ValueError("direction must be 'rx' or 'tx'")

        text_norm = self._normalize_text(text)
        now = self._now_ts()
        ts = self._safe_int(rx_timestamp) or now

        from_id_norm = self._normalize_node_id(from_id)
        to_id_norm = self._normalize_node_id(to_id)
        from_name_norm = self._normalize_text(from_name)
        to_name_norm = self._normalize_text(to_name)

        if not from_name_norm and from_id_norm:
            node = self.get_node(from_id_norm)
            if node:
                from_name_norm = self._preferred_display_name(node) or from_id_norm

        if not to_name_norm and to_id_norm:
            node = self.get_node(to_id_norm)
            if node:
                to_name_norm = self._preferred_display_name(node) or to_id_norm

        msg = {
            "id": self._normalize_text(message_id) or f"{direction}-{now}-{int(time.time_ns() % 1000000)}",
            "direction": direction,
            "text": text_norm,
            "from_id": from_id_norm,
            "from_name": from_name_norm or from_id_norm,
            "to_id": to_id_norm,
            "to_name": to_name_norm or to_id_norm,
            "channel": self._normalize_text(channel),
            "acked": bool(acked),
            "timestamp": ts,
            "created_at": now,
            "raw": _json_safe(raw if isinstance(raw, dict) else {}),
        }

        def updater(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            if not isinstance(messages, list):
                messages = []
            messages.append(msg)
            messages = messages[-self.max_messages :]
            return messages

        self.messages_store.update(updater)
        return copy.deepcopy(msg)

    def get_messages(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        messages = self.messages_store.read()
        if not isinstance(messages, list):
            return []
        items = [copy.deepcopy(m) for m in messages if isinstance(m, dict)]
        items.sort(key=lambda m: (m.get("timestamp") or 0, m.get("created_at") or 0))
        if limit is not None:
            try:
                limit_i = max(0, int(limit))
                items = items[-limit_i:]
            except (TypeError, ValueError):
                pass
        return items

    def enqueue_tak(self, cot_xml: str, *, event_type: str = "position", node_id: Optional[Any] = None) -> Dict[str, Any]:
        cot_xml = self._normalize_text(cot_xml)
        if not cot_xml:
            raise ValueError("cot_xml is required")

        item = {
            "id": f"tak-{self._now_ts()}-{int(time.time_ns() % 1000000)}",
            "event_type": self._normalize_text(event_type) or "position",
            "node_id": self._normalize_node_id(node_id),
            "cot": cot_xml,
            "timestamp": self._now_ts(),
            "attempts": 0,
            "last_error": "",
        }

        def updater(queue: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            if not isinstance(queue, list):
                queue = []
            queue.append(item)
            queue = queue[-self.max_queue :]
            return queue

        self.queue_store.update(updater)
        return copy.deepcopy(item)

    def get_queue(self) -> List[Dict[str, Any]]:
        queue = self.queue_store.read()
        if not isinstance(queue, list):
            return []
        items = []
        for q in queue:
            if not isinstance(q, dict):
                continue
            entry = copy.deepcopy(q)
            entry["node_id"] = self._normalize_node_id(entry.get("node_id"))
            items.append(entry)
        return items

    def pop_queue(self) -> Optional[Dict[str, Any]]:
        popped: Dict[str, Any] = {}

        def updater(queue: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            nonlocal popped
            if not isinstance(queue, list) or not queue:
                popped = {}
                return []
            popped = copy.deepcopy(queue[0])
            return queue[1:]

        self.queue_store.update(updater)
        if popped:
            popped["node_id"] = self._normalize_node_id(popped.get("node_id"))
        return popped or None

    def requeue_failed(self, item: Dict[str, Any], error: Any) -> None:
        if not isinstance(item, dict):
            return

        item_copy = copy.deepcopy(item)
        item_copy["attempts"] = int(item_copy.get("attempts", 0)) + 1
        item_copy["last_error"] = self._normalize_text(error)
        item_copy["timestamp"] = self._now_ts()
        item_copy["node_id"] = self._normalize_node_id(item_copy.get("node_id"))

        def updater(queue: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            if not isinstance(queue, list):
                queue = []
            queue.append(item_copy)
            queue = queue[-self.max_queue :]
            return queue

        self.queue_store.update(updater)

    def clear_queue(self) -> None:
        self.queue_store.write([])

    def stats(self) -> Dict[str, Any]:
        nodes = self.get_nodes()
        messages = self.get_messages()
        queue = self.get_queue()

        now = self._now_ts()
        online_cutoff = now - 300

        online = 0
        with_position = 0
        for node in nodes:
            last_heard = node.get("last_heard") or 0
            if last_heard >= online_cutoff:
                online += 1
            if node.get("lat") is not None and node.get("lon") is not None:
                with_position += 1

        return {
            "node_count": len(nodes),
            "online_count": online,
            "position_count": with_position,
            "message_count": len(messages),
            "queue_count": len(queue),
            "updated_at": now,
        }
