import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

from catalog_mirror.store import Store
from catalog_mirror.albums import DATASET

spec = importlib.util.spec_from_file_location("validate_album_import", Path(__file__).parents[1] / "scripts" / "validate_album_import.py")
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


class AlbumValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "synthetic.sqlite"
        self.store = Store(self.path)
        self.run = self.store.start_run("test")
        self.store.upsert({"entity_type": "band", "entity_id": "7", "name": "Synthetic private band"}, self.run)
        for number, extra in ((10, {"band_id": "7", "snapshot_artist_key": "7", "release_year": 2000}),
                              (11, {"band_id": "8", "snapshot_artist_key": "opaque-secret"}), (12, {})):
            self.store.upsert({"entity_type": "release", "entity_id": str(number), "name": "Synthetic private title",
                               "source_url": f"https://www.metal-archives.com/albums/{number}",
                               "snapshot_dataset": DATASET, **extra}, self.run)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_aggregate_only_links_identity_and_metadata(self):
        before = self.path.read_bytes()
        report = validator.validate_database(self.path)
        self.assertTrue(report["valid"])
        self.assertTrue(report["counts_stable"])
        self.assertEqual(report["release_count"], 3)
        self.assertEqual(report["linked_band_count"], 1)
        self.assertEqual(report["linked_release_count"], 1)
        self.assertEqual(report["missing_parent_release_count"], 1)
        self.assertEqual(report["unlinked_release_count"], 1)
        self.assertEqual(report["metadata_available"]["release_year"], 1)
        self.assertEqual(report["snapshot_artist_key_shapes"]["opaque"], 1)
        self.assertEqual(report["snapshot_artist_key_shapes"]["numeric"], 1)
        text = json.dumps(report)
        for secret in ("Synthetic private", "opaque-secret", "https://"):
            self.assertNotIn(secret, text)
        self.assertEqual(before, self.path.read_bytes())

    def test_mismatched_url_fails(self):
        with self.store.conn:
            self.store.conn.execute("UPDATE entities SET source_url='https://www.metal-archives.com/albums/99' WHERE entity_type='release' AND entity_id='10'")
        self.assertFalse(validator.validate_database(self.path)["valid"])

    def test_existing_song_allowed_but_snapshot_song_rejected(self):
        self.store.upsert({"entity_type": "song", "entity_id": "1", "name": "Synthetic song"}, self.run)
        self.assertTrue(validator.validate_database(self.path)["valid"])
        self.store.upsert({"entity_type": "song", "entity_id": "2", "name": "Synthetic song", "snapshot_dataset": DATASET}, self.run)
        report = validator.validate_database(self.path)
        self.assertEqual(report["song_count"], 2)
        self.assertEqual(report["snapshot_song_count"], 1)
        self.assertFalse(report["valid"])

    def test_missing_database_not_created_and_error_has_no_path(self):
        path = self.path.parent / "private-missing.sqlite"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(validator.main(["--db", str(path)]), 1)
        self.assertFalse(path.exists())
        self.assertNotIn("private-missing", output.getvalue())


if __name__ == "__main__":
    unittest.main()
