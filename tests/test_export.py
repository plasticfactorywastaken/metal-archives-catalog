import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from catalog_mirror.export import csv_cell, export_catalog
from catalog_mirror.store import Store


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
            recovered += json.loads(path.read_text(encoding="utf-8"))
            with (target / partition["csv_path"]).open(encoding="utf-8-sig", newline="") as stream:
                self.assertEqual(len(list(csv.DictReader(stream))), partition["count"])
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
        data = json.loads((target / manifest["partitions"][0]["path"]).read_text())
        self.assertEqual(data[0]["name"], "=Synthetic")

    def test_cross_entity_name_enrichment(self):
        self.store.upsert({"entity_type": "band", "entity_id": "1", "name": "Synthetic Band"}, self.run, verified=True)
        self.store.upsert({"entity_type": "release", "entity_id": "2", "name": "Synthetic Release", "band_id": "1"}, self.run, verified=True)
        self.assertEqual(self.store.get("release", "2")["band_name"], "Synthetic Band")

    def exported_records(self):
        target = self.root / "public"
        manifest = export_catalog(self.store, target)
        return {(row["entity_type"], row["entity_id"]): row
                for part in manifest["partitions"]
                for row in json.loads((target / part["path"]).read_text(encoding="utf-8"))}

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


if __name__ == "__main__":
    unittest.main()
