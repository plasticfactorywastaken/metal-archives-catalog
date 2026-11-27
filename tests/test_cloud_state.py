import json
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from catalog_mirror.store import Store
from scripts import cloud_state


class CloudStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "source.sqlite"
        self.snapshot = self.root / "snapshot"
        self.restored = self.root / "restored.sqlite"
        with Store(self.db) as store:
            run = store.start_run("test")
            store.upsert({"entity_type": "band", "entity_id": "1", "name": "Synthetic", "genres_raw": "Doom/Black"}, run, verified=True)
            store.set_override("band", "1", "ecommerce_primary_class", "Manual")
            store.set_checkpoint("discovery", {"page": 42})

    def tearDown(self):
        self.temp.cleanup()

    def test_pack_restore_preserves_data_history_overrides_checkpoint(self):
        with patch.object(cloud_state, "CHUNK", 512):
            manifest = cloud_state.pack(self.db, self.snapshot)
            self.assertGreater(len(manifest["parts"]), 1)
            cloud_state.unpack(self.snapshot, self.restored)
        with Store(self.restored) as store:
            self.assertEqual(store.get("band", "1")["ecommerce_primary_class"], "Manual")
            self.assertEqual(store.get_checkpoint("discovery"), {"page": 42})
            self.assertEqual(len(list(store.iter_changes())), 1)
        # Rename/delete on Windows also proves all SQLite handles were closed.
        moved = self.root / "moved.sqlite"
        self.restored.rename(moved)
        moved.unlink()

    def test_missing_database_cannot_be_snapshotted_as_empty(self):
        with self.assertRaises(RuntimeError):
            cloud_state.pack(self.root / "absent.sqlite", self.snapshot)
        self.assertFalse((self.root / "absent.sqlite").exists())

    def test_corrupt_part_cannot_replace_existing_database(self):
        manifest = cloud_state.pack(self.db, self.snapshot)
        self.restored.write_bytes(self.db.read_bytes())
        before = self.restored.read_bytes()
        (self.snapshot / manifest["parts"][0]["name"]).write_bytes(b"corrupt")
        with self.assertRaises(RuntimeError):
            cloud_state.unpack(self.snapshot, self.restored)
        self.assertEqual(self.restored.read_bytes(), before)

    def test_manifest_checksum_mismatch_preserves_old_database(self):
        manifest = cloud_state.pack(self.db, self.snapshot)
        self.restored.write_bytes(self.db.read_bytes())
        before = self.restored.read_bytes()
        manifest["sqlite_sha256"] = "0" * 64
        (self.snapshot / "snapshot-manifest.json").write_text(json.dumps(manifest))
        with self.assertRaises(RuntimeError):
            cloud_state.unpack(self.snapshot, self.restored)
        self.assertEqual(self.restored.read_bytes(), before)
        self.assertFalse(self.restored.with_suffix(".restoring.sqlite").exists())

    def test_out_of_order_or_unsafe_part_paths_refused(self):
        manifest = cloud_state.pack(self.db, self.snapshot)
        for name in ("../source.sqlite", "catalog.sqlite.gz.part0010", "catalog.sqlite.gz.part0000/../source.sqlite"):
            manifest["parts"][0]["name"] = name
            (self.snapshot / "snapshot-manifest.json").write_text(json.dumps(manifest))
            with self.assertRaises(RuntimeError):
                cloud_state.unpack(self.snapshot, self.restored)
        self.assertFalse(self.restored.exists())

    def test_declared_size_limits_decompression(self):
        manifest = cloud_state.pack(self.db, self.snapshot)
        manifest["sqlite_size"] = 10
        (self.snapshot / "snapshot-manifest.json").write_text(json.dumps(manifest))
        with self.assertRaises(RuntimeError):
            cloud_state.unpack(self.snapshot, self.restored)
        self.assertFalse(self.restored.exists())

    def test_wal_prevents_unsafe_restore(self):
        cloud_state.pack(self.db, self.snapshot)
        Path(str(self.restored) + "-wal").write_bytes(b"existing write-ahead log")
        with self.assertRaises(RuntimeError):
            cloud_state.unpack(self.snapshot, self.restored)

    def test_retry_after_cooldown_roundtrips_without_raw_cache(self):
        cache = self.root / "cache"
        cache.mkdir()
        name = hashlib.sha256(b"www.metal-archives.com").hexdigest() + ".cooldown.json"
        (cache / name).write_text(json.dumps({"until": 2_000_000_000}))
        (cache / "raw-response.html").write_text("Must not be snapshotted")
        manifest = cloud_state.pack(self.db, self.snapshot)
        self.assertEqual(manifest["cooldown_asset"]["name"], "cooldowns.json")
        self.assertFalse((self.snapshot / "raw-response.html").exists())
        restored_cache = self.root / "restored-cache"
        cloud_state.unpack(self.snapshot, self.restored, cache_dir=restored_cache)
        self.assertEqual(json.loads((restored_cache / name).read_text()), {"until": 2_000_000_000})

    def test_restoring_older_state_never_shortens_cooldown(self):
        cache = self.root / "cache"
        cache.mkdir()
        name = hashlib.sha256(b"metal-api.dev").hexdigest() + ".cooldown.json"
        (cache / name).write_text(json.dumps({"until": 1000}))
        cloud_state.pack(self.db, self.snapshot)
        (cache / name).write_text(json.dumps({"until": 2000}))
        cloud_state.unpack(self.snapshot, self.restored)
        self.assertEqual(json.loads((cache / name).read_text())["until"], 2000)

    def test_corrupt_cooldown_asset_does_not_replace_db(self):
        cloud_state.pack(self.db, self.snapshot)
        (self.snapshot / "cooldowns.json").write_text('{"corrupt":true}')
        self.restored.write_bytes(self.db.read_bytes())
        before = self.restored.read_bytes()
        with self.assertRaises(RuntimeError):
            cloud_state.unpack(self.snapshot, self.restored)
        self.assertEqual(self.restored.read_bytes(), before)

    def test_no_release_requires_explicit_initialization(self):
        with patch.object(cloud_state, "gh", return_value="[[]]"):
            with self.assertRaises(RuntimeError):
                cloud_state.restore(self.restored)
        self.assertFalse(self.restored.exists())

    def test_failed_upload_never_publishes_draft(self):
        calls = []

        def failing_gh(*args):
            calls.append(args)
            if args[:2] == ("release", "upload"):
                raise RuntimeError("Synthetic upload failure")
            return ""

        with patch.object(cloud_state, "gh", side_effect=failing_gh):
            with self.assertRaises(RuntimeError):
                cloud_state.publish(self.db)
        self.assertTrue(any(args[:2] == ("release", "create") and "--draft" in args for args in calls))
        self.assertFalse(any(args[:2] == ("release", "edit") for args in calls))
        with Store(self.db) as store:
            self.assertEqual(store.summary()["total_records"], 1)


if __name__ == "__main__":
    unittest.main()
