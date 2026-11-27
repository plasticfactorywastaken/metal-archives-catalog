import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import zipfile

from catalog_mirror.snapshot import FILES, SnapshotDownloader, SnapshotError, SnapshotImporter, normalize_row, prepare_csv, safe_download_url, stable_id
from catalog_mirror.store import Store


def band(number, **changes):
    return {"Band ID": str(number), "Name": f"Synthetic Band {number}", "URL": f"https://www.metal-archives.com/bands/Synthetic/{number}", "Country": "Synthetic Country", "Genre": " Doom/Black/Doom ", "Status": "Active", "Photo_URL": "https://invalid.test/excluded.jpg", "Label ID": "12.0", **changes}


def label(number=12, **changes):
    return {"Label ID": str(number), "Band ID": "1", "Name": "Synthetic Label", "Specialization": "Doom", "Status": "Active", "Country": "Synthetic Country", "Website": "", "Online Shopping": "", **changes}


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "catalog.sqlite")
        self.files = {kind: self.root / spec["filename"] for kind, spec in FILES.items()}
        self.write("bands", [band(1), band(2), band(3)])
        self.write("labels", [label(), label(), label(13)])

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def write(self, kind, rows):
        with self.files[kind].open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=FILES[kind]["headers"])
            writer.writeheader()
            writer.writerows(rows)

    def importer(self, **kwargs):
        return SnapshotImporter(self.store, batch_size=2, **kwargs)

    def test_historical_pending_genres_provenance_no_images_or_releases(self):
        result = self.importer().run(limit=100, files=self.files)
        record = self.store.get("band", "1")
        self.assertEqual(record["genres_raw"], " Doom/Black/Doom ")
        self.assertEqual(record["genres_json"], ["Doom", "Black", "Doom"])
        self.assertEqual(record["verification_status"], "pending")
        self.assertIsNone(record["last_checked"])
        self.assertIsNone(record["source_updated_at"])
        self.assertEqual(record["snapshot_version"], 3)
        self.assertEqual(len(record["snapshot_checksum"]), 64)
        self.assertNotIn("Photo_URL", record)
        self.assertNotIn("excluded.jpg", json.dumps(record))
        self.assertNotIn("online_shopping", self.store.get("label", "12"))
        self.assertEqual(self.store.summary()["entity_counts"], {"band": 3, "label": 2})
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["snapshot_status"], "complete")
        self.assertEqual(result["estimated_confidence"], 0)
        self.assertIsNone(result["estimated_coverage"])
        notice = self.store.get_checkpoint("historical_snapshot")
        self.assertTrue(notice["complete"])
        self.assertTrue(notice["unverified"])
        self.assertIsNone(notice["observation_date"])

    def test_bounded_resume_and_deduplicated_labels(self):
        first = self.importer().run(mode="batch", limit=2, files=self.files)
        self.assertEqual(first["processed"], 2)
        self.assertEqual(self.store.summary()["total_records"], 2)
        self.assertEqual(self.store.get_checkpoint(SnapshotImporter.checkpoint_key("bands"))["next_row"], 2)
        self.importer().run(mode="full", limit=100, files=self.files)
        self.assertEqual(self.store.summary()["total_records"], 5)
        before = len(list(self.store.iter_changes()))
        repeated = self.importer().run(limit=100, files=self.files)
        self.assertEqual(repeated["processed"], 0)
        self.assertEqual(len(list(self.store.iter_changes())), before)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM review_queue WHERE reason='historical_snapshot_unverified'").fetchone()[0], 1)

    def test_existing_verified_and_unverified_rows_and_overrides_survive(self):
        run = self.store.start_run("test")
        for number, verified in ((1, True), (2, False)):
            self.store.upsert({"entity_type": "band", "entity_id": str(number), "name": "Newer current value", "genres_raw": "Current Genre"}, run, source="website" if verified else "api", verified=verified)
        self.store.set_override("band", "1", "ecommerce_primary_class", "Manual class")
        before = self.store.get("band", "1")["content_hash"]
        self.importer().run(limit=100, files=self.files)
        self.assertEqual(self.store.get("band", "1")["content_hash"], before)
        self.assertEqual(self.store.get("band", "1")["ecommerce_primary_class"], "Manual class")
        self.assertEqual(self.store.get("band", "2")["name"], "Newer current value")

    def test_resume_rejects_changed_pinned_file(self):
        self.importer().run(limit=1, files=self.files)
        self.write("bands", [band(1), band(2, Name="Changed source")])
        result = self.importer().run(limit=100, files=self.files)
        self.assertIn("checksum changed", str(result["error_summary"]))
        self.assertEqual(self.store.summary()["total_records"], 1)
        self.assertEqual(self.store.get_checkpoint(SnapshotImporter.checkpoint_key("bands"))["next_row"], 1)

    def test_invalid_ids_and_urls_are_skipped_without_stopping_valid_rows(self):
        self.write("bands", [band(1, URL="https://www.metal-archives.com/bands/Wrong/9"), band(2), band(3, **{"Band ID": "Unknown"})])
        self.write("labels", [label(0), label(12), label(**{"Label ID": "12.0"})])
        result = self.importer().run(limit=100, files=self.files)
        self.assertEqual(result["records_skipped"], 3)
        self.assertEqual(self.store.summary()["entity_counts"], {"band": 1, "label": 1})

    def test_missing_genres_are_preserved_as_empty(self):
        self.write("bands", [band(1, Genre="")])
        self.importer().run(limit=100, files=self.files)
        self.assertEqual(self.store.get("band", "1")["genres_json"], [])

    def test_committed_checkpoint_survives_connection_reopen(self):
        self.importer().run(limit=2, files=self.files)
        self.store.close()
        self.store = Store(self.root / "catalog.sqlite")
        self.importer().run(limit=100, files=self.files)
        self.assertEqual(self.store.summary()["total_records"], 5)
        self.assertEqual(len(list(self.store.iter_changes())), 5)

    def test_changed_headers_abort_before_records(self):
        self.files["bands"].write_text("unexpected,headers\none,two\n")
        result = self.importer().run(files=self.files)
        self.assertIn("headers changed", str(result["error_summary"]))
        self.assertEqual(self.store.summary()["total_records"], 0)

    def test_positive_budgets_and_time_limit(self):
        with self.assertRaises(ValueError):
            self.importer().run(limit=0, files=self.files)
        times = iter([0, 2])
        result = self.importer(clock=lambda: next(times, 2)).run(seconds=1, files=self.files)
        self.assertEqual(result["processed"], 0)

    def test_stable_ids_do_not_round_through_float(self):
        self.assertEqual(stable_id("12345678901234567890.0"), "12345678901234567890")
        for value in ("0", "nan", "Unknown", "1.2", "-5", "1e9"):
            self.assertIsNone(stable_id(value))

    def test_plain_csv_and_strict_zip(self):
        self.assertEqual(prepare_csv(self.files["bands"], "metal_bands_roster.csv"), self.files["bands"])
        archive = self.root / "synthetic.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("metal_bands_roster.csv", self.files["bands"].read_bytes())
        self.assertEqual(prepare_csv(archive, "metal_bands_roster.csv").read_bytes(), self.files["bands"].read_bytes())
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("../metal_bands_roster.csv", b"data")
        with self.assertRaises(SnapshotError):
            prepare_csv(archive, "metal_bands_roster.csv")
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("metal_bands_roster.csv", b"data")
            z.writestr("image.jpg", b"excluded")
        with self.assertRaises(SnapshotError):
            prepare_csv(archive, "metal_bands_roster.csv")

    def test_download_redirect_allowlist(self):
        for url in ("http://storage.googleapis.com/file", "https://evil.test/file", "https://www.kaggle.com.evil.test/file", "https://user:pass@www.kaggle.com/file"):
            with self.assertRaises(SnapshotError):
                safe_download_url(url)
        self.assertEqual(safe_download_url("https://storage.googleapis.com/public-file"), "https://storage.googleapis.com/public-file")

    def test_download_size_cap_and_html_refusal(self):
        class Response(io.BytesIO):
            status = 200
            headers = {"Content-Type": "text/csv"}
            def geturl(self):
                return "https://storage.googleapis.com/synthetic"
        class Opener:
            def open(self, *args, **kwargs):
                return Response(b"1234567890")
        with self.assertRaises(SnapshotError):
            SnapshotDownloader(Opener(), max_bytes=5, clock=lambda: 0).download("metal_bands_roster.csv", self.root / "download", 10)
        self.assertFalse((self.root / "download").exists())
        Response.headers = {"Content-Type": "text/html"}
        with self.assertRaises(SnapshotError):
            SnapshotDownloader(Opener(), clock=lambda: 0).download("metal_bands_roster.csv", self.root / "download", 10)

    def test_truncated_download_is_not_accepted(self):
        class Response(io.BytesIO):
            status = 200
            headers = {"Content-Type": "text/csv", "Content-Length": "100"}
            def geturl(self):
                return "https://storage.googleapis.com/synthetic"
        class Opener:
            def open(self, *args, **kwargs):
                return Response(b"truncated")
        with self.assertRaises(SnapshotError):
            SnapshotDownloader(Opener(), clock=lambda: 0).download("metal_bands_roster.csv", self.root / "download", 10)
        self.assertFalse((self.root / "download").exists())

    def test_cli_bootstrap_avoids_live_source_construction_and_exports(self):
        from catalog_mirror.cli import main
        config = self.root / "settings.json"
        config.write_text(json.dumps({"database": str(self.root / "cli.sqlite"), "export_directory": str(self.root / "public"), "sheet_id": ""}))
        fake = {"run_id": "synthetic-run", "processed": 0, "records_remaining": None, "estimated_coverage": None, "estimated_confidence": None}
        def fake_run(importer, *args):
            run_id = importer.store.start_run("snapshot_test")
            importer.store.finish_run(run_id, status="partial")
            return {**fake, "run_id": run_id}
        with patch("catalog_mirror.cli.Transport", side_effect=AssertionError("No live transport")), patch("catalog_mirror.cli.SnapshotImporter.run", fake_run), patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(main(["--config", str(config), "bootstrap-snapshot", "--mode", "test", "--limit", "2", "--skip-sheets"]), 0)
        self.assertTrue((self.root / "public" / "manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
