"""Tests for the cross-protocol name contamination cleanup migration.

Versions <0.6.7 had an unscoped MeshCore name fallback in
``src/api/server.py::_save_and_notify`` that would write a MeshCore contact's
``long_name`` into a Meshtastic node's row whenever an inbound Meshtastic
message lacked a name. The fallback is now scoped to MeshCore packets only,
and a startup migration cleans up rows poisoned by the prior bug.
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone

from src.storage.database import DatabaseManager


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestCrossProtocolNameCleanup(unittest.TestCase):
    """In-memory tests of the contamination repair migration."""

    def _connect_with_seed(self, seed_rows):
        """Connect a fresh in-memory DB and insert seed rows AFTER the schema is
        created but BEFORE the migration runs. Done by calling connect() once
        to materialize the schema, inserting the seeds, then re-running the
        cleanup directly.
        """
        db = DatabaseManager(":memory:")
        _run(db.connect())
        now = datetime.now(timezone.utc).isoformat()
        for row in seed_rows:
            _run(db.execute(
                "INSERT INTO nodes (node_id, long_name, short_name, protocol, "
                "last_heard, first_seen) VALUES (?, ?, ?, ?, ?, ?)",
                (row["node_id"], row.get("long_name"), row.get("short_name"),
                 row["protocol"], now, now),
            ))
        _run(db.commit())
        return db

    def _select(self, db, node_id):
        cursor = _run(db.execute(
            "SELECT node_id, long_name, short_name, protocol FROM nodes "
            "WHERE node_id = ?",
            (node_id,),
        ))
        row = _run(cursor.fetchone())
        return dict(row) if row else None

    def test_cleans_meshtastic_row_poisoned_by_meshcore_name(self):
        """Classic bug case: MT node had its long_name overwritten with a
        MeshCore contact name. Migration should NULL the MT row."""
        db = self._connect_with_seed([
            {"node_id": "7d8b98a9", "long_name": "Guzii_RedV4",
             "short_name": "K", "protocol": "meshtastic"},
            {"node_id": "mc:Guzii_RedV4", "long_name": "Guzii_RedV4",
             "protocol": "meshcore"},
        ])

        _run(db._cleanup_cross_protocol_name_contamination())
        _run(db.commit())

        row = self._select(db, "7d8b98a9")
        self.assertIsNone(row["long_name"])
        self.assertEqual(row["short_name"], "K")

        mc_row = self._select(db, "mc:Guzii_RedV4")
        self.assertEqual(mc_row["long_name"], "Guzii_RedV4")

        _run(db.disconnect())

    def test_idempotent_when_data_clean(self):
        """No MT rows match any mc:% long_name. Migration is a no-op."""
        db = self._connect_with_seed([
            {"node_id": "deadbeef", "long_name": "Meshpoint",
             "short_name": "MPNT", "protocol": "meshtastic"},
            {"node_id": "mc:Guzii_RedV4", "long_name": "Guzii_RedV4",
             "protocol": "meshcore"},
        ])

        _run(db._cleanup_cross_protocol_name_contamination())
        _run(db.commit())

        row = self._select(db, "deadbeef")
        self.assertEqual(row["long_name"], "Meshpoint")
        self.assertEqual(row["short_name"], "MPNT")

        _run(db.disconnect())

    def test_only_touches_meshtastic_rows(self):
        """A MeshCore row that happens to share a long_name with another
        mc:% row must NOT be cleared. The migration's protocol filter
        keeps it scoped strictly to Meshtastic."""
        db = self._connect_with_seed([
            {"node_id": "mc:abc123", "long_name": "Shared Name",
             "protocol": "meshcore"},
            {"node_id": "mc:Shared Name", "long_name": "Shared Name",
             "protocol": "meshcore"},
        ])

        _run(db._cleanup_cross_protocol_name_contamination())
        _run(db.commit())

        for nid in ("mc:abc123", "mc:Shared Name"):
            row = self._select(db, nid)
            self.assertEqual(row["long_name"], "Shared Name")

        _run(db.disconnect())

    def test_skips_when_no_mc_contacts_exist(self):
        """A fresh-install DB without any mc:% rows runs cleanly."""
        db = self._connect_with_seed([
            {"node_id": "deadbeef", "long_name": "Meshpoint",
             "short_name": "MPNT", "protocol": "meshtastic"},
            {"node_id": "557673f2", "long_name": "Some MT Node",
             "protocol": "meshtastic"},
        ])

        _run(db._cleanup_cross_protocol_name_contamination())
        _run(db.commit())

        row = self._select(db, "deadbeef")
        self.assertEqual(row["long_name"], "Meshpoint")

        _run(db.disconnect())

    def test_runs_during_connect(self):
        """The cleanup must be wired into ``_run_migrations`` so a normal
        ``connect()`` call repairs poisoned rows without explicit invocation.
        """
        import tempfile
        import os

        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            db = DatabaseManager(db_path)
            _run(db.connect())
            now = datetime.now(timezone.utc).isoformat()
            _run(db.execute(
                "INSERT INTO nodes (node_id, long_name, short_name, protocol, "
                "last_heard, first_seen) VALUES "
                "('7d8b98a9', 'Guzii_RedV4', 'K', 'meshtastic', ?, ?), "
                "('mc:Guzii_RedV4', 'Guzii_RedV4', NULL, 'meshcore', ?, ?)",
                (now, now, now, now),
            ))
            _run(db.commit())
            _run(db.disconnect())

            db2 = DatabaseManager(db_path)
            _run(db2.connect())

            row = self._select(db2, "7d8b98a9")
            self.assertIsNotNone(row)
            self.assertIsNone(row["long_name"])
            self.assertEqual(row["short_name"], "K")
            self.assertEqual(row["protocol"], "meshtastic")

            _run(db2.disconnect())
        finally:
            os.unlink(db_path)


if __name__ == "__main__":
    unittest.main()
