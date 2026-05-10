"""Tests for MQTT auth mode credential selection."""

from __future__ import annotations

import hashlib
import unittest

from src.config import MqttConfig
from src.relay.mqtt_publisher import MqttPublisher


class TestMqttPublisherAuth(unittest.TestCase):
    def _publisher(self, **overrides) -> MqttPublisher:
        cfg = MqttConfig(**overrides)
        return MqttPublisher(config=cfg, device_name="TestPoint")

    def test_username_password_mode_uses_username_credentials(self):
        pub = self._publisher(
            auth_mode="username_password",
            username="alice",
            password="secret",
            api_key="apikey-should-not-win",
        )

        self.assertEqual(pub._mqtt_credentials(), ("alice", "secret"))

    def test_api_key_mode_ignores_stale_username_password(self):
        pub = self._publisher(
            auth_mode="api_key",
            username="meshdev",
            password="large4cats",
            api_key="abc123",
        )

        expected_user = "mrk_" + hashlib.sha256("abc123".encode()).hexdigest()[:20]
        self.assertEqual(pub._mqtt_credentials(), (expected_user, "abc123"))

    def test_none_mode_disables_credentials(self):
        pub = self._publisher(
            auth_mode="none",
            username="meshdev",
            password="large4cats",
            api_key="abc123",
        )

        self.assertIsNone(pub._mqtt_credentials())


if __name__ == "__main__":
    unittest.main()
