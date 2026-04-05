#!/usr/bin/env python3
import os
import signal
import subprocess
import sys
import time

APP_DIR = "/opt/meshtak"
VENV_BIN = f"{APP_DIR}/venv/bin"

MESHTAK_CMD = [f"{VENV_BIN}/python", f"{APP_DIR}/meshtak.py"]
WEBUI_CMD   = [f"{VENV_BIN}/python", f"{APP_DIR}/webui.py"]

processes = []


def start_process(cmd, name):
    print(f"[WRAPPER] Starting {name}: {' '.join(cmd)}", flush=True)
    proc = subprocess.Popen(cmd)
    processes.append((name, proc))
    return proc


def stop_all():
    print("[WRAPPER] Stopping all processes...", flush=True)

    for name, proc in processes:
        if proc.poll() is None:
            print(f"[WRAPPER] Terminating {name}", flush=True)
            proc.terminate()

    # give them a moment
    time.sleep(3)

    for name, proc in processes:
        if proc.poll() is None:
            print(f"[WRAPPER] Killing {name}", flush=True)
            proc.kill()


def signal_handler(sig, frame):
    print(f"[WRAPPER] Received signal {sig}", flush=True)
    stop_all()
    sys.exit(0)


def main():
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # ensure working dir
    os.chdir(APP_DIR)

    # start both components
    meshtak_proc = start_process(MESHTAK_CMD, "meshtak")
    webui_proc   = start_process(WEBUI_CMD, "webui")

    # monitor loop
    while True:
        time.sleep(2)

        for name, proc in processes:
            if proc.poll() is not None:
                print(f"[WRAPPER] {name} exited with code {proc.returncode}", flush=True)
                stop_all()
                sys.exit(1)


if __name__ == "__main__":
    main()
