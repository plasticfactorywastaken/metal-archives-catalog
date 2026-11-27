import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from catalog_mirror import albums
from catalog_mirror.store import Store


def album(number=10, **changes):
    return {"release_id": str(number), "release_url": f"https://www.metal-archives.com/albums/Synthetic_Band/Synthetic_Album/{number}",
            "album": "Synthetic Album", "artist_key": "7", "artist_name": "Synthetic Band", **changes}


class FakeProjectedParquet:
    rows = {}
    opened = []

    def __init__(self, path):
        self.values = self.rows[Path(path).name]
        self.num_rows = len(self.values)
        self.opened.append(Path(path).name)

    def batches(self, start_row, batch_size):
        for index in range(start_row, len(self.values), batch_size):
            yield self.values[index:index + batch_size]

    def close(self):
        pass


class AlbumTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "catalog.sqlite")
        self.specifications = {}
        self.paths = {}
        for number in (1, 2):
            name = f"part-{number:012}.parquet"
            data = b"PAR1synthetic fixture " + str(number).encode() + b"PAR1"
            self.paths[name] = self.root / name
            self.paths[name].write_bytes(data)
            self.specifications[name] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        self.names = list(self.paths)
        FakeProjectedParquet.rows = {self.names[0]: [album(10), album(10), album(11)], self.names[1]: [album(12)]}
        FakeProjectedParquet.opened = []
        self.patch_files = patch.object(albums, "FILES", self.specifications)
        self.patch_parquet = patch.object(albums, "ProjectedParquet", FakeProjectedParquet)
        self.patch_files.start()
        self.patch_parquet.start()

    def tearDown(self):
        self.patch_parquet.stop()
        self.patch_files.stop()
        self.store.close()
        self.temp.cleanup()

    def importer(self, **kwargs):
        return albums.AlbumSnapshotImporter(self.store, batch_size=2, **kwargs)

    def add_band(self, number="7", name="Synthetic Band"):
        run_id = self.store.start_run("test")
        self.store.upsert({"entity_type": "band", "entity_id": number, "name": name}, run_id, verified=False)

    def test_only_unique_releases_pending_with_honest_provenance(self):
        self.add_band()
        result = self.importer().run(limit=100, files=self.paths)
        self.assertEqual(result["processed"], 4)
        self.assertEqual(result["records_inserted"], 3)
        self.assertEqual(result["records_unchanged"], 1)
        self.assertEqual(result["albums_linked_inserted"], 3)
        self.assertEqual(result["artist_key_shapes"], {"numeric": 4})
        self.assertEqual(result["band_links"], {"numeric_existing_name_match": 4})
        self.assertEqual(self.store.summary()["entity_counts"], {"band": 1, "release": 3})
        record = self.store.get("release", "10")
        self.assertEqual(record["band_id"], "7")
        self.assertEqual(record["verification_status"], "pending")
        self.assertIsNone(record["last_checked"])
        self.assertIsNone(record["source_updated_at"])
        self.assertNotIn("release_year", record)
        self.assertNotIn("release_type", record)
        self.assertNotIn("track_id", record)
        self.assertNotIn("title", record)
        self.assertEqual(record["snapshot_license"], "unknown")
        observation = self.store.conn.execute("SELECT source,verified FROM source_observations WHERE entity_type='release' LIMIT 1").fetchone()
        self.assertIn(albums.REVISION, observation["source"])
        self.assertEqual(observation["verified"], 0)
        provenance = self.store.get_checkpoint("album_snapshot")
        self.assertTrue(provenance["complete"])
        self.assertTrue(provenance["unverified"])
        self.assertFalse(provenance["live"])
        self.assertFalse(provenance["publication_is_observation_date"])
        self.assertIsNone(provenance["observation_date"])
        self.assertEqual(provenance["license"], "unknown")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["snapshot_status"], "complete")
        self.assertIsNone(result["estimated_coverage"])

    def test_bounded_resume_cross_file_and_completed_files_not_reopened(self):
        first = self.importer().run(limit=2, files=self.paths)
        self.assertEqual(first["processed"], 2)
        key = albums.AlbumSnapshotImporter.checkpoint_key(self.names[0])
        self.assertEqual(self.store.get_checkpoint(key)["next_row"], 2)
        self.store.close()
        self.store = Store(self.root / "catalog.sqlite")
        second = self.importer().run(limit=1, files=self.paths)
        self.assertTrue(self.store.get_checkpoint(key)["done"])
        self.assertEqual(second["records_inserted"], 1)
        FakeProjectedParquet.opened = []
        third = self.importer().run(limit=100, files=self.paths)
        self.assertEqual(FakeProjectedParquet.opened, [self.names[1]])
        self.assertEqual(third["records_inserted"], 1)
        before = len(list(self.store.iter_changes()))
        fourth = self.importer().run(limit=100, files=self.paths)
        self.assertEqual(fourth["processed"], 0)
        self.assertEqual(len(list(self.store.iter_changes())), before)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM review_queue WHERE reason='album_snapshot_unverified'").fetchone()[0], 1)

    def test_existing_records_and_overrides_never_overwritten(self):
        run_id = self.store.start_run("test")
        for number, verified in ((10, True), (11, False)):
            self.store.upsert({"entity_type": "release", "entity_id": str(number), "name": "Existing title", "release_year": 2030}, run_id, verified=verified)
        self.store.set_override("release", "10", "ecommerce_primary_class", "Manual class")
        original = self.store.get("release", "10")
        self.importer().run(limit=100, files=self.paths)
        self.assertEqual(self.store.get("release", "10"), original)
        self.assertEqual(self.store.get("release", "11")["name"], "Existing title")

    def test_invalid_album_isolated_and_ambiguous_artist_not_mislabeled(self):
        self.add_band(name="Different band with same numeric ID")
        FakeProjectedParquet.rows[self.names[0]] = [
            album(10, release_id="99"), album(11, release_url="https://evil.test/albums/B/A/11"),
            album(12, album=""), album(13), album(14, artist_key="opaque:7"),
            album(15, artist_key="https://www.metal-archives.com/bands/Synthetic_Band/7"),
        ]
        FakeProjectedParquet.rows[self.names[1]] = []
        result = self.importer().run(limit=100, files=self.paths)
        self.assertEqual(result["records_skipped"], 3)
        self.assertNotIn("band_id", self.store.get("release", "13"))
        self.assertNotIn("band_id", self.store.get("release", "14"))
        self.assertEqual(self.store.get("release", "14")["snapshot_artist_key"], "opaque:7")
        self.assertEqual(self.store.get("release", "15")["band_id"], "7")
        self.assertEqual(result["albums_linked_inserted"], 1)
        self.assertEqual(result["snapshot_status"], "complete")

    def test_opaque_release_key_never_becomes_entity_id(self):
        record, link = albums.normalize_row(album(release_id="upstream:opaque"), self.names[0], self.store)
        self.assertEqual(record["entity_id"], "10")
        self.assertEqual(link, "unlinked")
        for url in ("https://www.metal-archives.com.evil.test/albums/B/A/10", "https://user:pass@www.metal-archives.com/albums/B/A/10", "https://www.metal-archives.com/albums/B/A/0", "https://www.metal-archives.com/bands/B/10", "https://www.metal-archives.com:444/albums/B/A/10"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                albums.normalize_row(album(release_url=url), self.names[0])

    def test_changed_bytes_and_stale_checkpoint_fail_before_decoding(self):
        self.importer().run(limit=1, files=self.paths)
        key = albums.AlbumSnapshotImporter.checkpoint_key(self.names[0])
        self.paths[self.names[0]].write_bytes(b"PAR1changed payload!!!PAR1")
        result = self.importer().run(limit=100, files=self.paths)
        self.assertEqual(result["processed"], 0)
        self.assertEqual(self.store.get_checkpoint(key)["next_row"], 1)
        checkpoint = self.store.get_checkpoint(key)
        checkpoint["checksum"] = "0" * 64
        self.store.set_checkpoint(key, checkpoint)
        result = self.importer().run(limit=100, files=self.paths)
        self.assertIn("checkpoint checksum", str(result["error_summary"]))

    def test_batch_rollback_retains_only_prior_committed_cursor(self):
        FakeProjectedParquet.rows[self.names[0]] = [album(10), album(11), album(12), album(13)]
        original = albums._insert_album
        def fail_at_thirteen(store, record, run_id, timestamp):
            if record["entity_id"] == "13":
                raise ValueError("synthetic write failure")
            return original(store, record, run_id, timestamp)
        with patch.object(albums, "_insert_album", fail_at_thirteen):
            result = self.importer().run(limit=100, files=self.paths)
        key = albums.AlbumSnapshotImporter.checkpoint_key(self.names[0])
        self.assertEqual(self.store.get_checkpoint(key)["next_row"], 2)
        self.assertEqual(result["processed"], 2)
        self.assertIsNone(self.store.get("release", "12"))
        self.importer().run(limit=100, files=self.paths)
        self.assertEqual(self.store.summary()["entity_counts"], {"release": 4})

    def test_time_expiry_and_budget_validation(self):
        times = iter([0, 2])
        result = self.importer(clock=lambda: next(times, 2)).run(limit=100, seconds=1, files=self.paths)
        self.assertEqual(result["processed"], 0)
        for value in (0, -1, True):
            with self.assertRaises(ValueError):
                self.importer().run(limit=value, files=self.paths)
        with self.assertRaises(ValueError):
            albums.AlbumSnapshotImporter(self.store, batch_size=501)

    def test_downloader_integrity_and_host_allowlist(self):
        filename = self.names[0]
        payload = self.paths[filename].read_bytes()
        class Response(io.BytesIO):
            status = 200
            headers = {"Content-Type": "application/octet-stream", "Content-Length": str(len(payload))}
            def geturl(self):
                return "https://us.aws.cdn.hf.co/synthetic?signature=fixture"
        class Opener:
            calls = 0
            def open(self, *args, **kwargs):
                self.calls += 1
                return Response(payload)
        opener = Opener()
        target = self.root / "download.parquet"
        result = albums.AlbumDownloader(opener, clock=lambda: 0).download(filename, target, 10)
        self.assertEqual(result.read_bytes(), payload)
        self.assertEqual(opener.calls, 1)
        for url in ("http://huggingface.co/file", "https://huggingface.co.evil.test/file", "https://evil.test/file", "https://user:pass@huggingface.co/file", "https://huggingface.co:bad/file", "https://us.aws.cdn.hf.co:443/file#fragment"):
            with self.subTest(url=url), self.assertRaises(albums.AlbumSnapshotError):
                albums.safe_download_url(url)
        with self.assertRaises(albums.AlbumSnapshotError):
            albums.SafeRedirect().redirect_request(urllib.request.Request("https://huggingface.co/file"), None, 302, "Found", {}, "https://evil.test/file")
        Response.headers = {"Content-Length": "10000"}
        with self.assertRaises(albums.AlbumSnapshotError):
            albums.AlbumDownloader(opener, clock=lambda: 0).download(filename, target, 10)
        self.assertFalse(target.exists())
        Response.headers = {"Content-Type": "text/html"}
        with self.assertRaises(albums.AlbumSnapshotError):
            albums.AlbumDownloader(opener, clock=lambda: 0).download(filename, target, 10)

    def test_truncated_corrupted_and_403_downloads_leave_no_file(self):
        filename = self.names[0]
        original = self.paths[filename].read_bytes()
        class Response(io.BytesIO):
            status = 200
            headers = {}
            def geturl(self):
                return "https://us.aws.cdn.hf.co/synthetic"
        class Opener:
            def __init__(self, payload):
                self.payload = payload
            def open(self, *args, **kwargs):
                if self.payload is None:
                    raise urllib.error.HTTPError("https://huggingface.co/synthetic", 403, "Forbidden", {}, None)
                return Response(self.payload)
        target = self.root / "download.parquet"
        for payload in (original[:-1], b"PAR1" + b"x" * (len(original) - 8) + b"PAR1", original + b"x", None):
            with self.subTest(length=None if payload is None else len(payload)), self.assertRaises(albums.AlbumSnapshotError):
                albums.AlbumDownloader(Opener(payload), clock=lambda: 0).download(filename, target, 10)
            self.assertFalse(target.exists())


@unittest.skipUnless(importlib.util.find_spec("pyarrow"), "optional pyarrow dependency unavailable")
class RealParquetTests(unittest.TestCase):
    def test_projection_and_row_group_resume_with_real_synthetic_parquet(self):
        import pyarrow as pa
        import pyarrow.parquet as parquet
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "synthetic.parquet"
            rows = [{**album(number), "title": "Excluded synthetic track", "track_id": "excluded"} for number in range(10, 17)]
            parquet.write_table(pa.Table.from_pylist(rows), path, row_group_size=3)
            reader = albums.ProjectedParquet(path)
            try:
                batches = list(reader.batches(4, 2))
                self.assertTrue(all(len(batch) <= 2 for batch in batches))
                values = [row for batch in batches for row in batch]
                self.assertEqual([value["release_id"] for value in values], ["14", "15", "16"])
                self.assertTrue(all(set(value) == set(albums.COLUMNS) for value in values))
                self.assertEqual(list(reader.batches(7, 2)), [])
                with self.assertRaises(albums.AlbumSnapshotError):
                    list(reader.batches(8, 2))
            finally:
                reader.close()

    def test_schema_changes_refused(self):
        import pyarrow as pa
        import pyarrow.parquet as parquet
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "synthetic.parquet"
            parquet.write_table(pa.table({"unexpected": ["value"]}), path)
            with self.assertRaises(albums.AlbumSnapshotError):
                albums.ProjectedParquet(path)
            row = album(release_id=10)
            parquet.write_table(pa.Table.from_pylist([row]), path)
            with self.assertRaises(albums.AlbumSnapshotError):
                albums.ProjectedParquet(path)


if __name__ == "__main__":
    unittest.main()
