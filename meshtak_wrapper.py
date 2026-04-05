#!/usr/bin/env python3
import os
import sys
import time
import threading
import subprocess
import signal

BRIDGE_SCRIPT = "/opt/meshtak/meshtak.py"
WEBUI_SCRIPT = "/opt/meshtak/webui.py"

bridge_proc = None
web_proc = None
stop_requested = False


def start_process(cmd):
    return subprocess.Popen(cmd)


def stop_process(proc):
    if not proc:
        return
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def signal_handler(signum, frame):
    global stop_requested
    stop_requested = True
    stop_process(bridge_proc)
    stop_process(web_proc)
    sys.exit(0)


def main():
    global bridge_proc, web_proc

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    if not os.path.isfile(BRIDGE_SCRIPT):
        raise SystemExit(f"Bridge script not found: {BRIDGE_SCRIPT}")

    if not os.path.isfile(WEBUI_SCRIPT):
        raise SystemExit(f"Web UI script not found: {WEBUI_SCRIPT}")

    bridge_proc = start_process([sys.executable, BRIDGE_SCRIPT])
    web_proc = start_process([sys.executable, WEBUI_SCRIPT])

    while not stop_requested:
        bridge_rc = bridge_proc.poll()
        web_rc = web_proc.poll()

        if bridge_rc is not None:
            stop_process(web_proc)
            raise SystemExit(f"Bridge process exited with code {bridge_rc}")

        if web_rc is not None:
            stop_process(bridge_proc)
            raise SystemExit(f"Web UI process exited with code {web_rc}")

        time.sleep(2)


if __name__ == "__main__":
    main()
