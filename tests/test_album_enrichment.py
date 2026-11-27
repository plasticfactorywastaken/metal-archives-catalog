import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from catalog_mirror import album_enrichment as enrichment
from catalog_mirror.albums import DATASET, REVISION
from catalog_mirror.store import Store


class AlbumEnrichmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "catalog.sqlite")
        self.path = self.root / "discography.csv"
        self.run_id = self.store.start_run("fixture")
        self.add_band("1", "Unique Band")
        self.add_band("2", "Duplicate Band")
        self.add_band("3", "Duplicate Band")
        self.write_csv([
            {"Band ID": "1", "Album Name": "Synthetic Album", "Type": "Full-length", "Year": "2020"},
            {"Band ID": "1", "Album Name": "Ambiguous Album", "Type": "EP", "Year": "2021"},
            {"Band ID": "1", "Album Name": "Ambiguous Album", "Type": "Full-length", "Year": "2022"},
        ])
        self.byte_patch = patch.object(enrichment, "DISCOGRAPHY_BYTES", self.path.stat().st_size)
        self.byte_patch.start()

    def tearDown(self):
        self.byte_patch.stop()
        self.store.close()
        self.temp.cleanup()

    def add_band(self, entity_id, name):
        self.store.upsert({"entity_type": "band", "entity_id": entity_id, "name": name}, self.run_id, verified=False)

    def add_album(self, entity_id="10", verified=False, **fields):
        record = {"entity_type": "release", "entity_id": entity_id, "name": "Synthetic Album", "band_name": "Unique Band",
                  "source_url": f"https://www.metal-archives.com/albums/B/A/{entity_id}",
                  "snapshot_dataset": DATASET, "snapshot_revision": REVISION,
                  "snapshot_artist_key": "opaque:key", **fields}
        self.store.upsert(record, self.run_id, verified=verified)

    def write_csv(self, rows):
        with self.path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=enrichment.DISCOGRAPHY_HEADERS)
            writer.writeheader()
            writer.writerows(rows)

    def run_enrichment(self, **kwargs):
        return enrichment.AlbumEnricher(self.store, batch_size=2).run(discography_file=self.path, **kwargs)

    def test_unique_exact_link_and_discography_are_explicitly_unverified(self):
        self.add_album()
        result = self.run_enrichment()
        value = self.store.get("release", "10")
        self.assertEqual(value["band_id"], "1")
        self.assertEqual(value["band_link_method"], "unique_exact_name")
        self.assertFalse(value["band_link_verified"])
        self.assertEqual(value["release_year"], "2020")
        self.assertEqual(value["release_type"], "Full-length")
        self.assertEqual(value["discography_match_method"], "unique_exact_band_id_and_title")
        self.assertFalse(value["discography_match_verified"])
        self.assertIsNone(value["last_checked"])
        self.assertIsNone(value["source_updated_at"])
        self.assertEqual(value["verification_status"], "pending")
        self.assertEqual(result["bands_linked_by_inference"], 1)
        self.assertEqual(result["release_years_added"], 1)
        self.assertEqual(result["records_verified_directly"], 0)
        self.assertEqual(result["enrichment_status"], "complete")
        observation = self.store.conn.execute("SELECT verified FROM source_observations WHERE source='enrichment:exact_band_name:kaggle_v3'").fetchone()
        self.assertEqual(observation[0], 0)

    def test_ambiguous_band_names_and_title_pairs_remain_unknown(self):
        self.add_album("10", band_name="Duplicate Band")
        self.add_album("11", name="Ambiguous Album")
        self.add_album("12", band_name="unique band")
        result = self.run_enrichment()
        self.assertNotIn("band_id", self.store.get("release", "10"))
        self.assertNotIn("band_id", self.store.get("release", "12"))
        self.assertEqual(self.store.get("release", "11")["band_id"], "1")
        self.assertNotIn("release_year", self.store.get("release", "11"))
        self.assertNotIn("release_type", self.store.get("release", "11"))
        self.assertEqual(result["band_link_outcomes"]["ambiguous_name"], 1)
        self.assertEqual(result["discography_match_outcomes"]["ambiguous_band_title"], 1)

    def test_verified_other_sources_existing_fields_and_overrides_preserved(self):
        self.add_album("10", verified=True)
        self.add_album("11", snapshot_dataset="different/source")
        self.add_album("12", release_year="2030", release_type="Existing type")
        self.store.set_override("release", "12", "ecommerce_primary_class", "Manual class")
        before10 = self.store.get("release", "10")
        before11 = self.store.get("release", "11")
        self.run_enrichment()
        self.assertEqual(self.store.get("release", "10"), before10)
        self.assertEqual(self.store.get("release", "11"), before11)
        value = self.store.get("release", "12")
        self.assertEqual(value["band_id"], "1")
        self.assertEqual(value["release_year"], "2030")
        self.assertEqual(value["release_type"], "Existing type")
        self.assertEqual(value["ecommerce_primary_class"], "Manual class")

    def test_bounded_resume_idempotence_and_new_albums_after_complete_pass(self):
        for entity_id in ("10", "11", "12"):
            self.add_album(entity_id)
        first = self.run_enrichment(limit=1)
        self.assertEqual(first["processed"], 1)
        self.assertEqual(first["records_updated"], 1)
        self.assertNotIn("band_id", self.store.get("release", "11"))
        self.store.close()
        self.store = Store(self.root / "catalog.sqlite")
        second = self.run_enrichment(limit=100)
        self.assertEqual(second["records_updated"], 2)
        self.assertEqual(second["enrichment_status"], "complete")
        third = self.run_enrichment(limit=100)
        self.assertEqual(third["records_updated"], 0)
        self.add_album("09")
        fourth = self.run_enrichment(limit=100)
        self.assertEqual(fourth["records_updated"], 1)
        self.assertEqual(self.store.get("release", "09")["band_id"], "1")

    def test_band_only_mode_never_downloads_or_requires_csv(self):
        self.add_album()
        class ForbiddenDownloader:
            def download(self, *args):
                raise AssertionError("No metadata download in band-only mode")
        result = enrichment.AlbumEnricher(self.store, downloader=ForbiddenDownloader()).run(include_discography=False)
        self.assertEqual(result["bands_linked_by_inference"], 1)
        self.assertNotIn("release_year", self.store.get("release", "10"))

    def test_duplicate_identical_discography_rows_still_ambiguous(self):
        self.add_album()
        row = {"Band ID": "1", "Album Name": "Synthetic Album", "Type": "EP", "Year": "2020"}
        self.write_csv([row, row])
        with patch.object(enrichment, "DISCOGRAPHY_BYTES", self.path.stat().st_size):
            self.run_enrichment()
        self.assertNotIn("release_type", self.store.get("release", "10"))

    def test_changed_checksum_rejected_before_record_changes(self):
        self.add_album()
        self.store.set_checkpoint("album_discography_source", {"checksum": "0" * 64})
        result = self.run_enrichment()
        self.assertIn("checksum changed", str(result["error_summary"]))
        self.assertNotIn("band_id", self.store.get("release", "10"))

    def test_batch_rollback_does_not_advance_cursor(self):
        for entity_id in ("10", "11", "12", "13"):
            self.add_album(entity_id)
        original = self.store._change
        def fail(run_id, kind, entity_id, *args):
            if entity_id == "13":
                raise ValueError("synthetic failure")
            return original(run_id, kind, entity_id, *args)
        with patch.object(self.store, "_change", fail):
            result = self.run_enrichment()
        checkpoint = self.store.get_checkpoint(enrichment.CHECKPOINT + ":discography")
        self.assertEqual(checkpoint["next_entity_id"], "11")
        self.assertEqual(result["records_updated"], 2)
        self.assertNotIn("band_id", self.store.get("release", "12"))
        self.run_enrichment()
        self.assertEqual(self.store.get("release", "13")["band_id"], "1")

    def test_downloader_plain_csv_length_and_truncation(self):
        payload = self.path.read_bytes()
        class Response(io.BytesIO):
            status = 200
            headers = {"Content-Type": "text/csv", "Content-Length": str(len(payload))}
            def geturl(self):
                return "https://storage.googleapis.com/synthetic"
        class Opener:
            def __init__(self, data):
                self.data = data
            def open(self, *args, **kwargs):
                return Response(self.data)
        target = self.root / "download.csv"
        enrichment.DiscographyDownloader(Opener(payload), clock=lambda: 0).download(target, 10)
        self.assertEqual(target.read_bytes(), payload)
        with self.assertRaises(enrichment.EnrichmentError):
            enrichment.DiscographyDownloader(Opener(payload[:-1]), clock=lambda: 0).download(target, 10)
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
