import tempfile
import unittest
from pathlib import Path

from catalog_mirror.store import Store, content_hash


def band(**fields):
    return {"entity_type": "band", "entity_id": "test-1", "name": "Synthetic Band", "source_url": "https://www.metal-archives.com/bands/Synthetic/1", "genres_raw": "Doom Metal/Black Metal", **fields}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "catalog.sqlite"
        self.store = Store(self.path)
        self.run = self.store.start_run("test")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_band_end_to_end(self):
        self.assertEqual(self.store.upsert(band(), self.run, verified=True), "inserted")
        record = self.store.get("band", "test-1")
        self.assertEqual(record["primary_genre"], "Doom Metal")
        self.assertEqual(record["verification_status"], "verified")
        self.assertTrue(record["first_seen"])
        self.assertTrue(record["last_checked"])
        self.assertEqual(self.store.conn.execute("SELECT primary_genre FROM bands").fetchone()[0], "Doom Metal")
        self.assertEqual(list(self.store.iter_changes())[0]["change_type"], "new")

    def test_dedup_and_idempotent_rerun(self):
        self.store.upsert(band(), self.run, verified=True)
        run2 = self.store.start_run("batch")
        self.assertEqual(self.store.upsert(band(), run2, verified=True), "unchanged")
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0], 1)
        self.assertEqual(list(self.store.iter_changes(run2)), [])

    def test_hash_excludes_volatile_metadata(self):
        self.assertEqual(content_hash(band(last_checked="today", verification_status="verified")), content_hash(band(last_checked="tomorrow")))
        self.assertNotEqual(content_hash(band()), content_hash(band(genres_raw="Black Metal/Doom Metal")))

    def test_changed_genre_and_primary_have_old_new(self):
        self.store.upsert(band(), self.run, verified=True)
        run2 = self.store.start_run("batch")
        self.store.upsert(band(genres_raw="Black Metal/Doom Metal"), run2, verified=True)
        changes = list(self.store.iter_changes(run2))
        change = next(c for c in changes if c["changed_field"] == "primary_genre")
        self.assertEqual(change["previous_value"], "Doom Metal")
        self.assertEqual(change["new_value"], "Black Metal")
        self.assertEqual(change["change_type"], "primary_genre_changed")
        self.assertEqual(self.store.summary(run2)["weekly"]["genre_changes"], 1)

    def test_missing_fields_do_not_erase(self):
        self.store.upsert(band(country="Testland"), self.run, verified=True)
        self.store.upsert({"entity_type": "band", "entity_id": "test-1", "country": None}, self.run, verified=True)
        record = self.store.get("band", "test-1")
        self.assertEqual(record["country"], "Testland")
        self.assertEqual(record["primary_genre"], "Doom Metal")

    def test_soft_removal_needs_distinct_runs(self):
        self.store.upsert(band(), self.run, verified=True)
        for _ in range(5):
            self.assertEqual(self.store.mark_absent("band", "test-1", self.run), "missing")
        self.assertEqual(self.store.get("band", "test-1")["missing_count"], 1)
        self.assertEqual(self.store.mark_absent("band", "test-1", self.store.start_run("batch")), "missing")
        self.assertEqual(self.store.mark_absent("band", "test-1", self.store.start_run("batch")), "removed")
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0], 1)

    def test_partial_failure_cannot_remove(self):
        self.store.upsert(band(), self.run, verified=True)
        for code in [403, 429, 500, 503]:
            self.assertEqual(self.store.mark_absent("band", "test-1", self.run, status_code=code), "unavailable")
        self.store.finish_run(self.run, status="partial", website_status="unavailable")
        self.assertEqual(self.store.get("band", "test-1")["active_status"], "active")

    def test_reappearance(self):
        self.store.upsert(band(), self.run, verified=True)
        self.store.mark_absent("band", "test-1", self.run, strong=True, status_code=410)
        run2 = self.store.start_run("batch")
        self.assertEqual(self.store.upsert(band(), run2, verified=True), "reappeared")
        self.assertEqual(self.store.get("band", "test-1")["active_status"], "active")
        self.assertEqual(self.store.get("band", "test-1")["missing_count"], 0)

    def test_unverified_api_cannot_resurrect_missing_record(self):
        self.store.upsert(band(), self.run, verified=True)
        self.store.mark_absent("band", "test-1", self.run, strong=True, status_code=410)
        self.assertEqual(self.store.upsert(band(), self.store.start_run("batch"), source="api"), "unchanged")
        self.assertEqual(self.store.get("band", "test-1")["active_status"], "removed")

    def test_source_conflict_website_wins(self):
        self.store.upsert(band(genres_raw="Heavy Metal"), self.run, source="api")
        self.store.upsert(band(), self.run, source="website", verified=True)
        self.assertEqual(self.store.get("band", "test-1")["genres_raw"], "Doom Metal/Black Metal")
        self.assertEqual(self.store.summary()["latest_run"]["conflicts"], 1)
        self.store.upsert(band(genres_raw="Heavy Metal"), self.run, source="api")
        self.assertEqual(self.store.get("band", "test-1")["genres_raw"], "Doom Metal/Black Metal")
        self.assertEqual(self.store.summary()["latest_run"]["conflicts"], 1)

    def test_stale_api_observation_not_conflict(self):
        self.store.upsert(band(), self.run, source="api")
        self.store.upsert(band(), self.run, source="website", verified=True)
        run2 = self.store.start_run("batch")
        self.store.upsert(band(genres_raw="Heavy Metal"), run2, source="website", verified=True)
        self.assertEqual(self.store.summary(run2)["latest_run"]["conflicts"], 0)

    def test_override_survives_source_updates(self):
        self.store.upsert(band(), self.run, verified=True)
        self.store.set_override("band", "test-1", "ecommerce_primary_class", "Collector special", reviewer="Tester")
        self.store.upsert(band(genres_raw="Heavy Metal"), self.run, verified=True)
        record = self.store.get("band", "test-1")
        self.assertEqual(record["ecommerce_primary_class"], "Collector special")
        self.assertEqual(record["primary_genre"], "Heavy Metal")
        self.assertEqual(record["classification_status"], "overridden")
        with self.assertRaises(ValueError):
            self.store.set_override("band", "test-1", "genres_raw", "Other")

    def test_checkpoint_resume(self):
        self.store.upsert(band(), self.run, verified=True)
        self.store.set_checkpoint("discovery:bands:A", {"offset": 100})
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.get_checkpoint("discovery:bands:A"), {"offset": 100})
        self.assertEqual(self.store.upsert(band(), self.store.start_run("batch"), verified=True), "unchanged")

    def test_upcoming_to_released(self):
        record = {"entity_type": "release", "entity_id": "r1", "name": "Synthetic release", "is_upcoming": True, "release_date": "2030-01-01"}
        self.store.upsert(record, self.run, verified=True)
        self.assertEqual(self.store.summary()["weekly"]["new_upcoming_releases"], 1)
        run2 = self.store.start_run("batch")
        self.store.upsert({**record, "is_upcoming": False}, run2, verified=True)
        self.assertEqual(self.store.summary(run2)["weekly"]["upcoming_now_released"], 1)

    def test_upcoming_entity_closed_when_release_confirmed(self):
        self.store.upsert({"entity_type": "release", "entity_id": "r1", "name": "Synthetic future release", "is_upcoming": True}, self.run, verified=True)
        self.store.upsert({"entity_type": "upcoming", "entity_id": "r1", "name": "Synthetic future release", "status": "announced", "is_upcoming": True}, self.run, verified=True)
        self.store.upsert({"entity_type": "release", "entity_id": "r1", "name": "Synthetic future release", "is_upcoming": False}, self.run, verified=True)
        self.assertEqual(self.store.get("upcoming", "r1")["status"], "released")
        self.assertFalse(self.store.get("upcoming", "r1")["is_upcoming"])
        self.assertEqual(self.store.summary()["weekly"]["upcoming_now_released"], 1)

    def test_all_entity_types_and_namespace_ids(self):
        for kind in ("band", "release", "song", "label", "artist", "relationship", "upcoming", "review"):
            self.store.upsert({"entity_type": kind, "entity_id": "same", "name": f"Synthetic {kind}"}, self.run, verified=True)
        self.assertEqual(self.store.summary()["total_records"], 8)

    def test_summary_and_run_metrics(self):
        self.store.upsert(band(), self.run, verified=True)
        self.store.finish_run(self.run, status="partial", estimated_coverage=None, estimated_confidence=1.0, records_remaining=None, api_failures=1)
        summary = self.store.summary()
        self.assertIsNone(summary["estimated_coverage"])
        self.assertEqual(summary["estimated_confidence"], 1.0)
        self.assertEqual(summary["weekly"]["new_records"], 1)
        self.assertEqual(summary["weekly"]["api_failures"], 1)


if __name__ == "__main__":
    unittest.main()
