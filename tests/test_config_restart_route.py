"""Tests for the POST /api/config/restart endpoint."""

from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes import config_routes


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(config_routes.router)
    return app


class TestConfigRestartRoute(unittest.TestCase):

    def setUp(self) -> None:
        self.app = _build_app()
        self.client = TestClient(self.app)

    def test_restart_returns_success_when_systemctl_succeeds(self):
        with patch("src.api.routes.config_routes.subprocess.Popen") as popen_mock:
            popen_mock.return_value.pid = 1234

            res = self.client.post("/api/config/restart")

        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["status"], "restarting")
        self.assertIn("requested_at", body)
        datetime.fromisoformat(body["requested_at"])
        popen_mock.assert_called_once_with(
            ["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "restart", "meshpoint"],
            stdout=unittest.mock.ANY,
            stderr=unittest.mock.ANY,
        )

    def test_restart_returns_500_when_spawn_fails(self):
        with patch("src.api.routes.config_routes.subprocess.Popen") as popen_mock:
            popen_mock.side_effect = OSError("spawn failed")

            res = self.client.post("/api/config/restart")

        self.assertEqual(res.status_code, 500)
        self.assertIn("spawn failed", res.json()["detail"])


if __name__ == "__main__":
    unittest.main()
