import csv
import gzip
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from catalog_mirror.export import csv_cell, export_catalog
from catalog_mirror.store import Store


def decoded_bytes(path):
    data = path.read_bytes()
    return gzip.decompress(data) if path.suffix == ".gz" else data


def json_records(path):
    return json.loads(decoded_bytes(path).decode("utf-8"))


def csv_records(path):
    return list(csv.DictReader(io.StringIO(decoded_bytes(path).decode("utf-8-sig"), newline="")))


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "source.sqlite")
        self.run = self.store.start_run("test")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_empty_catalog_is_truthful(self):
        target = self.root / "public"
        manifest = export_catalog(self.store, target)
        self.assertEqual(manifest["total_records"], 0)
        self.assertEqual(manifest["partitions"], [])
        summary = json.loads((target / "summary.json").read_text())
        self.assertIsNone(summary["estimated_coverage"])
        self.assertIsNone(summary["estimated_confidence"])

    def test_partitioned_export_roundtrip(self):
        for index in range(7):
            self.store.upsert({"entity_type": "band", "entity_id": str(index), "name": f"Synthetic {index}", "genres_raw": "Doom/Black/Doom"}, self.run, verified=True)
        target = self.root / "public"
        manifest = export_catalog(self.store, target, partition_size=3, include_sqlite=True)
        self.assertEqual([part["count"] for part in manifest["partitions"]], [3, 3, 1])
        recovered = []
        for partition in manifest["partitions"]:
            path = target / partition["path"]
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), partition["sha256"])
            recovered += json_records(path)
            self.assertEqual(len(csv_records(target / partition["csv_path"])), partition["count"])
            for prefix, asset_path in (("", path), ("csv_", target / partition["csv_path"])):
                self.assertEqual(partition[prefix + "encoding"], "gzip")
                self.assertEqual(partition[prefix + "bytes"], asset_path.stat().st_size)
                self.assertEqual(partition[prefix + "uncompressed_bytes"], len(decoded_bytes(asset_path)))
                self.assertEqual(partition[prefix + "sha256"], hashlib.sha256(asset_path.read_bytes()).hexdigest())
                self.assertEqual(partition[prefix + "uncompressed_sha256"], hashlib.sha256(decoded_bytes(asset_path)).hexdigest())
        self.assertEqual(len(recovered), 7)
        self.assertEqual(recovered[0]["genres_json"], ["Doom", "Black", "Doom"])
        with Store(target / "catalog.sqlite") as copy:
            self.assertEqual(copy.summary()["total_records"], 7)

    def test_export_failure_preserves_previous_good_export(self):
        target = self.root / "public"
        export_catalog(self.store, target)
        previous = (target / "manifest.json").read_bytes()
        self.store.upsert({"entity_type": "band", "entity_id": "1", "name": "Synthetic"}, self.run, verified=True)
        with self.assertRaises(ValueError):
            export_catalog(self.store, target, max_bytes=10)
        self.assertEqual((target / "manifest.json").read_bytes(), previous)
        self.assertFalse(list(self.root.glob(".public-export-*")))

    def test_reexport_removes_obsolete_partitions(self):
        target = self.root / "public"
        self.store.upsert({"entity_type": "band", "entity_id": "1", "name": "Synthetic"}, self.run, verified=True)
        export_catalog(self.store, target)
        (target / "orphan.json").write_text("[]")
        export_catalog(self.store, target)
        self.assertFalse((target / "orphan.json").exists())

    def test_csv_formula_safety_without_json_mutation(self):
        self.assertEqual(csv_cell("=SUM(1,2)"), "'=SUM(1,2)")
        self.store.upsert({"entity_type": "band", "entity_id": "1", "name": "=Synthetic"}, self.run, verified=True)
        target = self.root / "public"
        manifest = export_catalog(self.store, target)
        data = json_records(target / manifest["partitions"][0]["path"])
        self.assertEqual(data[0]["name"], "=Synthetic")
        self.assertEqual(csv_records(target / manifest["partitions"][0]["csv_path"])[0]["name"], "'=Synthetic")

    def test_cross_entity_name_enrichment(self):
        self.store.upsert({"entity_type": "band", "entity_id": "1", "name": "Synthetic Band"}, self.run, verified=True)
        self.store.upsert({"entity_type": "release", "entity_id": "2", "name": "Synthetic Release", "band_id": "1"}, self.run, verified=True)
        self.assertEqual(self.store.get("release", "2")["band_name"], "Synthetic Band")

    def exported_records(self):
        target = self.root / "public"
        manifest = export_catalog(self.store, target)
        return {(row["entity_type"], row["entity_id"]): row
                for part in manifest["partitions"]
                for row in json_records(target / part["path"])}

    def test_gzip_exports_keep_all_change_history_and_plain_status_files(self):
        self.store.upsert({"entity_type": "band", "entity_id": "1", "name": "Synthetic", "genres_raw": "Doom/Black"}, self.run, verified=True)
        self.store.upsert({"entity_type": "band", "entity_id": "1", "genres_raw": "Black/Doom"}, self.run, verified=True)
        self.store.upsert({"entity_type": "release", "entity_id": "2", "name": "Synthetic release", "band_id": "1"}, self.run, verified=True)
        target = self.root / "public"
        manifest = export_catalog(self.store, target, partition_size=2)
        self.assertEqual(manifest["version"], 2)
        recovered = []
        for part in manifest["change_partitions"]:
            path = target / part["path"]
            self.assertTrue(path.name.endswith(".csv.gz"))
            self.assertEqual(part["encoding"], "gzip")
            self.assertEqual(part["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            recovered.extend(csv_records(path))
        source_history = list(self.store.iter_changes())
        self.assertEqual(len(recovered), len(source_history))
        self.assertEqual({int(row["change_id"]) for row in recovered}, {row["change_id"] for row in source_history})
        self.assertEqual(sum(part["count"] for part in manifest["change_partitions"]), manifest["changes_total"])
        for name in ("manifest.json", "summary.json", "runs.json", "changes.json", "reviews.json"):
            json.loads((target / name).read_text(encoding="utf-8"))
        self.assertFalse(list(target.glob("**/part-*.json")))
        self.assertFalse(list(target.glob("**/part-*.csv")))

    def test_gzip_partition_bytes_are_deterministic(self):
        self.store.upsert({"entity_type": "band", "entity_id": "1", "name": "Synthetic", "genres_raw": "Doom/Doom"}, self.run, verified=True)
        target = self.root / "public"
        first = export_catalog(self.store, target)
        first_files = {str(path.relative_to(target)): path.read_bytes() for path in target.glob("**/*.gz")}
        second = export_catalog(self.store, target)
        self.assertEqual(first["partitions"], second["partitions"])
        self.assertEqual(first["change_partitions"], second["change_partitions"])
        self.assertEqual(first_files, {str(path.relative_to(target)): path.read_bytes() for path in target.glob("**/*.gz")})

    def test_plain_export_remains_available_and_preserves_same_values(self):
        self.store.upsert({"entity_type": "band", "entity_id": "1", "name": "Synthetic", "genres_raw": "Doom/Black/Doom"}, self.run, verified=True)
        zipped_root, plain_root = self.root / "zipped", self.root / "plain"
        zipped = export_catalog(self.store, zipped_root)
        plain = export_catalog(self.store, plain_root, compressed=False)
        self.assertEqual(plain["version"], 1)
        self.assertEqual(plain["export_compression"], "identity")
        zipped_part, plain_part = zipped["partitions"][0], plain["partitions"][0]
        self.assertTrue(plain_part["path"].endswith(".json"))
        self.assertTrue(plain_part["csv_path"].endswith(".csv"))
        self.assertEqual(plain_part["encoding"], "identity")
        self.assertEqual(plain_part["sha256"], plain_part["uncompressed_sha256"])
        self.assertEqual(decoded_bytes(zipped_root / zipped_part["path"]), decoded_bytes(plain_root / plain_part["path"]))
        self.assertEqual(decoded_bytes(zipped_root / zipped_part["csv_path"]), decoded_bytes(plain_root / plain_part["csv_path"]))

    def test_storage_budget_counts_compressed_files_not_expanded_json(self):
        self.store.upsert({"entity_type": "band", "entity_id": "1", "name": "Synthetic", "notes": "Synthetic repeated metadata. " * 3000}, self.run, verified=True)
        target = self.root / "public"
        compressed = export_catalog(self.store, target)
        compressed_size = sum(path.stat().st_size for path in target.rglob("*") if path.is_file())
        budget = compressed_size + 2000
        export_catalog(self.store, target, max_bytes=budget)
        before = (target / "manifest.json").read_bytes()
        with self.assertRaises(ValueError):
            export_catalog(self.store, target, max_bytes=budget, compressed=False)
        self.assertEqual((target / "manifest.json").read_bytes(), before)
        self.assertLess(compressed["partitions"][0]["bytes"], compressed["partitions"][0]["uncompressed_bytes"])

    def seed_genre_parents(self):
        self.store.upsert({"entity_type":"band", "entity_id":"1", "name":"Synthetic Band", "genres_raw":"Death/Black/Death", "country":"Finland", "status":"Active", "formed_in":1992, "source_url":"https://www.metal-archives.com/bands/1"}, self.run, verified=True)
        self.store.upsert({"entity_type":"release", "entity_id":"2", "name":"Synthetic Release", "band_id":"1", "label_name":"Synthetic Label", "label_id":"4", "source_url":"https://www.metal-archives.com/albums/2"}, self.run, verified=True)
        self.store.upsert({"entity_type":"song", "entity_id":"3", "name":"Synthetic Song", "release_id":"2"}, self.run, verified=True)
        self.store.set_genre_mapping("Death", "Extreme metal", confidence=0.9, review_status="approved")

    def test_release_and_song_inherit_explicit_facets_without_mutating_source(self):
        self.seed_genre_parents()
        before = self.store.get("release", "2")
        before_song = self.store.get("song", "3")
        records = self.exported_records()
        for key in (("release","2"), ("song","3")):
            row = records[key]
            self.assertEqual(row["genres_raw"], "")
            self.assertEqual(row["genres_json"], [])
            self.assertEqual(row["primary_genre"], "")
            self.assertEqual(row["facet_genres_json"], ["Death","Black","Death"])
            self.assertEqual(row["facet_primary_genre"], "Death")
            self.assertEqual(row["genre_origin"], "band")
            self.assertEqual(row["genre_origin_entity_id"], "1")
            self.assertEqual(row["facet_ecommerce_primary_class"], "Extreme metal")
            self.assertEqual(row["facet_classification_status"], "approved")
            self.assertEqual(row["facet_band_id"], "1")
            self.assertEqual(row["facet_country"], "Finland")
        self.assertEqual(records[("song","3")]["facet_label_name"], "Synthetic Label")
        self.assertEqual(self.store.get("release","2"), before)
        self.assertEqual(self.store.get("song","3"), before_song)
        self.assertEqual(records[("release","2")]["content_hash"], before["content_hash"])

    def test_own_genres_precede_parent_and_song_prefers_its_release(self):
        self.seed_genre_parents()
        self.store.upsert({"entity_type":"release", "entity_id":"2", "genres_raw":"Doom; Folk"}, self.run, verified=True)
        records = self.exported_records()
        self.assertEqual(records[("release","2")]["genre_origin"], "source")
        self.assertEqual(records[("release","2")]["genres_raw"], "Doom; Folk")
        self.assertEqual(records[("release","2")]["facet_genres_json"], ["Doom","Folk"])
        self.assertEqual(records[("song","3")]["genre_origin"], "release")
        self.assertEqual(records[("song","3")]["facet_genres_json"], ["Doom","Folk"])

    def test_entity_overrides_win_including_deliberate_empty_class(self):
        self.seed_genre_parents()
        self.store.set_override("release","2","ecommerce_primary_class","Hand picked")
        self.store.set_override("song","3","ecommerce_primary_class","")
        records = self.exported_records()
        self.assertEqual(records[("release","2")]["facet_ecommerce_primary_class"], "Hand picked")
        self.assertEqual(records[("release","2")]["facet_classification_status"], "overridden")
        self.assertEqual(records[("song","3")]["facet_ecommerce_primary_class"], "")
        self.assertEqual(records[("song","3")]["classification_origin"], "entity_override")

    def test_missing_or_ambiguous_parent_does_not_invent_a_genre(self):
        self.seed_genre_parents()
        self.store.upsert({"entity_type":"release", "entity_id":"9", "name":"Split", "band_id":"1", "band_ids":["1","8"]}, self.run, verified=True)
        self.store.upsert({"entity_type":"song", "entity_id":"10", "name":"Unknown", "release_id":"99"}, self.run, verified=True)
        records = self.exported_records()
        for key in (("release","9"),("song","10")):
            self.assertEqual(records[key]["genre_origin"], "unknown")
            self.assertEqual(records[key]["facet_genres_json"], [])

    def test_relationship_endpoints_get_names_for_filters(self):
        self.seed_genre_parents()
        self.store.upsert({"entity_type":"artist", "entity_id":"7", "name":"Synthetic Artist"}, self.run, verified=True)
        self.store.upsert({"entity_type":"relationship", "entity_id":"rel-1", "name":"Membership", "source_entity_type":"artist", "source_entity_id":"7", "target_entity_type":"band", "target_entity_id":"1"}, self.run, verified=True)
        row = self.exported_records()[("relationship","rel-1")]
        self.assertEqual(row["source_entity_name"], "Synthetic Artist")
        self.assertEqual(row["target_entity_name"], "Synthetic Band")

    def test_historical_snapshot_notice_survives_subsequent_sync_runs(self):
        provenance = {"provider":"Kaggle", "dataset_url":"https://www.kaggle.com/datasets/example/test", "version":3, "published_at":"2024-11-09", "snapshot_label":"November 2024", "complete":True, "unverified":True, "live":False, "observation_date":None}
        self.store.set_checkpoint("historical_snapshot", provenance)
        self.store.upsert({"entity_type":"band", "entity_id":"70", "name":"Synthetic historical band", "snapshot_published_at":"2024-11-09"}, self.run, source="historical_snapshot", verified=False)
        target = self.root / "public"
        first = export_catalog(self.store, target)
        live_run = self.store.start_run("batch")
        self.store.finish_run(live_run, status="partial", api_status="unavailable", website_status="blocked")
        second = export_catalog(self.store, target)
        self.assertEqual(first["historical_snapshot"], second["historical_snapshot"])
        self.assertEqual(second["historical_snapshot"]["published_at"], "2024-11-09")
        self.assertIsNone(second["historical_snapshot"]["observation_date"])
        self.assertFalse(second["historical_snapshot"]["publication_is_observation_date"])
        summary = json.loads((target / "summary.json").read_text())
        self.assertIsNone(summary["estimated_coverage"])
        self.assertEqual(summary["verification_ratio"], 0)
        self.assertEqual(summary["historical_snapshot"], second["historical_snapshot"])

    def test_band_label_name_facets_resolve_stable_label_ids(self):
        self.store.upsert({"entity_type":"label", "entity_id":"44", "name":"Synthetic Catalog Label"}, self.run, source="historical_snapshot", verified=False)
        self.store.upsert({"entity_type":"band", "entity_id":"70", "name":"Synthetic historical band", "label_id":"44"}, self.run, source="historical_snapshot", verified=False)
        record = self.exported_records()[("band","70")]
        self.assertEqual(record["label_id"], "44")
        self.assertEqual(record["label_name"], "Synthetic Catalog Label")
        self.assertEqual(record["facet_label_name"], "Synthetic Catalog Label")
        self.assertEqual(record["facet_label_id"], "44")

    def test_album_and_band_snapshot_notices_are_separate_compact_and_persistent(self):
        bands = {"provider": "Kaggle", "dataset_url": "https://www.kaggle.com/datasets/example/test", "version": 3, "published_at": "2024-11-09", "snapshot_label": "November 2024", "complete": True, "unverified": True, "live": False}
        albums = {"provider": "Hugging Face", "dataset": "fairygaze/music", "dataset_url": "https://huggingface.co/datasets/fairygaze/music", "revision": "06856d4d785dce8b029551bcca4839ee1e4d0ccc", "published_at": "2026-08-24", "observation_date": None, "license": "unknown", "complete": False, "unverified": True, "live": False, "files": [{"internal_progress": 123}], "unrelated": "Do not publish"}
        self.store.set_checkpoint("historical_snapshot", bands)
        self.store.set_checkpoint("album_snapshot", albums)
        target = self.root / "public"
        first = export_catalog(self.store, target)
        self.assertEqual(first["historical_snapshot"]["published_at"], "2024-11-09")
        self.assertEqual(first["album_snapshot"]["published_at"], "2026-08-24")
        self.assertEqual(first["album_snapshot"]["revision"], albums["revision"])
        self.assertIsNone(first["album_snapshot"]["observation_date"])
        self.assertFalse(first["album_snapshot"]["publication_is_observation_date"])
        self.assertNotIn("files", first["album_snapshot"])
        self.assertNotIn("unrelated", first["album_snapshot"])
        later = self.store.start_run("batch")
        self.store.finish_run(later, status="partial", website_status="blocked")
        second = export_catalog(self.store, target)
        self.assertEqual(first["album_snapshot"], second["album_snapshot"])
        summary = json.loads((target / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["album_snapshot"], second["album_snapshot"])
        self.assertEqual(summary["historical_snapshot"], second["historical_snapshot"])


if __name__ == "__main__":
    unittest.main()
