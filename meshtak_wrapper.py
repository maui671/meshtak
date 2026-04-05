#!/usr/bin/env python3
import logging
import os
import threading

from meshtak import MeshTAK
from webui import MeshTAKWebUI

BASE_DIR = "/opt/meshtak"
LOG_DIR = os.path.join(BASE_DIR, "logs")
LOG_PATH = os.path.join(LOG_DIR, "wrapper.log")


def ensure_log_dir():
    os.makedirs(LOG_DIR, exist_ok=True)


def setup_logging():
    ensure_log_dir()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_PATH),
            logging.StreamHandler()
        ]
    )


def main():
    setup_logging()
    log = logging.getLogger("meshtak.wrapper")

    log.info("Starting MeshTAK wrapper")

    mesh = MeshTAK()

    # Run backend in thread
    backend_thread = threading.Thread(target=mesh.start, daemon=True)
    backend_thread.start()

    # Run Web UI (blocking)
    ui = MeshTAKWebUI(mesh=mesh)
    ui.run()


if __name__ == "__main__":
    main()
