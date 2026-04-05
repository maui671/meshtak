import threading
import time

lock = threading.Lock()

nodes = {}

def update_node(node_id, data):
    with lock:
        node = nodes.get(node_id, {})
        node.update(data)
        node["last_seen"] = time.time()
        nodes[node_id] = node


def get_nodes():
    with lock:
        return dict(nodes)
