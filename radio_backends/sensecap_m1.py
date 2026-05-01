#!/usr/bin/env python3
import json
import logging
import os
import subprocess
import threading
import time
from typing import Any, Callable, Dict, Optional

log = logging.getLogger("meshtak.sensecap_m1")

class SenseCAPM1Backend:
    def __init__(self, config: Dict[str, Any], packet_callback: Callable[[Dict[str, Any], Any], None]) -> None:
        self.config = config or {}
        self.packet_callback = packet_callback

        self.enabled = bool(self.config.get("enabled", True))
        self.spi_device = str(self.config.get("spi_device", "/dev/spidev0.0"))
        self.rx_command = self.config.get("rx_command")
        self.rx_jsonl_path = str(self.config.get("rx_jsonl_path", "/run/meshtak/sensecap_m1_rx.jsonl"))
        self.tx_enabled = bool(self.config.get("tx_enabled", False))

        self.running = False
        self.connected = False
        self._thread: Optional[threading.Thread] = None
        self._proc: Optional[subprocess.Popen] = None

    def start(self) -> None:
        if not self.enabled:
            return

        self.running = True
        self.connected = True
        os.makedirs(os.path.dirname(self.rx_jsonl_path), exist_ok=True)

        if self.rx_command:
            self._start_rx_command()

        self._thread = threading.Thread(target=self._jsonl_reader_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.running = False
        self.connected = False

        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                self._proc.kill()

    def is_connected(self) -> bool:
        return self.running

    def sendText(self, text: str, destinationId: Optional[str] = None):
        raise RuntimeError("M1 TX not implemented")

    def close(self) -> None:
        self.stop()

    def _start_rx_command(self) -> None:
        self._proc = subprocess.Popen(
            self.rx_command,
            shell=isinstance(self.rx_command, str),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        threading.Thread(target=self._stdout_loop, daemon=True).start()

    def _stdout_loop(self):
        for line in self._proc.stdout:
            if not self.running:
                break
            self._handle_line(line)

    def _jsonl_reader_loop(self):
        last_size = 0
        while self.running:
            try:
                if not os.path.exists(self.rx_jsonl_path):
                    time.sleep(1)
                    continue

                with open(self.rx_jsonl_path, "r") as f:
                    f.seek(last_size)
                    for line in f:
                        self._handle_line(line)
                    last_size = f.tell()

                time.sleep(0.25)
            except Exception:
                time.sleep(1)

    def _handle_line(self, line: str):
        try:
            packet = json.loads(line.strip())
            packet.setdefault("rxTime", int(time.time()))
            packet.setdefault("via", "sensecap_m1")
            self.packet_callback(packet, self)
        except Exception:
            pass

