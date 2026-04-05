#!/usr/bin/env python3
import os
import signal
import subprocess
import sys
import time

APP_DIR = "/opt/meshtak"
VENV_PYTHON = f"{APP_DIR}/venv/bin/python"

MESHTAK_CMD = [VENV_PYTHON, f"{APP_DIR}/meshtak.py"]
WEBUI_CMD = [VENV_PYTHON, f"{APP_DIR}/webui.py"]

children = []
stopping = False


def log(message):
    print(f"[WRAPPER] {message}", flush=True)


def start_child(name, cmd):
    log(f"Starting {name}: {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd,
        cwd=APP_DIR,
        env=os.environ.copy(),
    )
    children.append((name, proc))
    return proc


def terminate_children():
    global stopping
    if stopping:
        return
    stopping = True

    log("Stopping child processes")

    for name, proc in children:
        if proc.poll() is None:
            try:
                log(f"Terminating {name} (pid={proc.pid})")
                proc.terminate()
            except Exception as exc:
                log(f"Failed to terminate {name}: {exc}")

    deadline = time.time() + 8
    while time.time() < deadline:
        alive = [proc for _, proc in children if proc.poll() is None]
        if not alive:
            break
        time.sleep(0.25)

    for name, proc in children:
        if proc.poll() is None:
            try:
                log(f"Killing {name} (pid={proc.pid})")
                proc.kill()
            except Exception as exc:
                log(f"Failed to kill {name}: {exc}")


def handle_signal(signum, frame):
    log(f"Received signal {signum}")
    terminate_children()
    sys.exit(0)


def main():
    os.chdir(APP_DIR)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    start_child("meshtak", MESHTAK_CMD)
    start_child("webui", WEBUI_CMD)

    while True:
        time.sleep(2)

        for name, proc in children:
            rc = proc.poll()
            if rc is not None:
                log(f"{name} exited with code {rc}")
                terminate_children()
                sys.exit(rc if isinstance(rc, int) else 1)


if __name__ == "__main__":
    main()
