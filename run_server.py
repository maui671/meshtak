#!/usr/bin/env python3

from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path

import uvicorn

from src.config import load_config


def _repo_root() -> Path:
    return Path(__file__).resolve().parent


def _resolve_path(raw: str) -> Path:
    path = Path(raw)
    if path.is_absolute():
        return path
    return (_repo_root() / path).resolve()


def _ensure_tls_material(cert_path: Path, key_path: Path) -> None:
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    if cert_path.exists() and key_path.exists():
        return

    hostname = socket.getfqdn() or socket.gethostname() or "meshpoint.local"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-nodes",
            "-newkey",
            "rsa:2048",
            "-days",
            "3650",
            "-keyout",
            str(key_path),
            "-out",
            str(cert_path),
            "-subj",
            f"/C=US/ST=Local/L=Local/O=Meshpoint/CN={hostname}",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    os.chmod(key_path, 0o600)
    os.chmod(cert_path, 0o644)


def main() -> None:
    config_path = os.environ.get("CONCENTRATOR_CONFIG")
    cfg = load_config(config_path)
    dashboard = cfg.dashboard

    kwargs = {
        "app": "src.api.server:create_app",
        "factory": True,
        "host": dashboard.host,
        "port": dashboard.port,
        "log_level": "info",
    }

    if dashboard.tls_enabled:
        cert_path = _resolve_path(dashboard.tls_cert_path)
        key_path = _resolve_path(dashboard.tls_key_path)
        _ensure_tls_material(cert_path, key_path)
        kwargs["ssl_certfile"] = str(cert_path)
        kwargs["ssl_keyfile"] = str(key_path)

    uvicorn.run(**kwargs)


if __name__ == "__main__":
    main()
