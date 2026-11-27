import copy
import json
import tempfile
import unittest
from pathlib import Path

from catalog_mirror.sources import SourceBlocked, SourceMissing, SourceUnavailable
from catalog_mirror.store import Store
from catalog_mirror.sync import SyncEngine


def band(i, **fields):
    return {"entity_type": "band", "entity_id": str(i), "name": f"Synthetic {i}",
            "source_url": f"https://www.metal-archives.com/bands/{i}", "genres_raw": "Doom/Black", **fields}


class FakeSource:
    def __init__(self, records=None, pages=None, status="available", interrupt=None):
        self.records = records or {}
        self.pages = pages or []
        self.status = status
        self.fetched, self.discovered = [], []
        self.interrupt = interrupt
    def probe(self): return {"status": self.status}
    def discover(self, checkpoint=None, limit=25):
        checkpoint = checkpoint or {}
        index = int(checkpoint.get("page", 0))
        self.discovered.append(copy.deepcopy(checkpoint))
        page = self.pages[index] if index < len(self.pages) else []
        if isinstance(page, BaseException): raise page
        return page, {"page": index + 1, "done": index + 1 >= len(self.pages)}, None
    def fetch(self, entity_type, entity_id, url=None):
        self.fetched.append((entity_type, str(entity_id)))
        if self.interrupt: raise self.interrupt
        value = self.records.get((entity_type, str(entity_id)), [band(entity_id)] if entity_type == "band" else [])
        if isinstance(value, BaseException): raise value
        return copy.deepcopy(value)


class UpcomingSource(FakeSource):
    def __init__(self, upcoming, **kwargs): super().__init__(**kwargs); self.upcoming = upcoming; self.upcoming_calls = []
    def discover_upcoming(self, checkpoint=None, limit=25):
        self.upcoming_calls.append(checkpoint)
        return self.upcoming, {"offset": len(self.upcoming), "done": True}, len(self.upcoming)


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "catalog.sqlite"
        self.store = Store(self.path)
    def tearDown(self): self.store.close(); self.tmp.cleanup()
    def engine(self, api=None, website=None, **settings):
        return SyncEngine(self.store, api or FakeSource(), website or FakeSource(), {"sample_size": 0, "max_records": 5, **settings})
    def test_bounded_test_import_and_same_state_idempotent(self):
        source = FakeSource(pages=[[{**band(1), "_discovery_only": True}, {**band(2), "_discovery_only": True}]])
        e = self.engine(website=source)
        first = e.run("test", limit=1)
        self.assertEqual(first["processed"], 1)
        self.assertEqual(e.pending(), 1)
        self.assertEqual(self.store.get("band", "1")["verification_status"], "verified")
        e.run("test", limit=2)
        count = self.store.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        changes = self.store.conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0]
        e.enqueue(band(1), "sample")
        e.run("test", limit=1)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0], count)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0], changes)
    def test_partial_source_failure_retains_good_record(self):
        run = self.store.start_run()
        self.store.upsert(band(1, country="Keep"), run, verified=True)
        self.store.finish_run(run)
        e = self.engine(api=FakeSource(status="unavailable"), website=FakeSource(status="blocked"), seed_records=[band(1)])
        result = e.run("test")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(self.store.get("band", "1")["country"], "Keep")
        self.assertEqual(self.store.get("band", "1")["active_status"], "active")
        self.assertEqual(e.pending(), 1)
    def test_resume_after_interruption_keeps_queue_and_checkpoint(self):
        source = FakeSource(pages=[[{**band(1), "_discovery_only": True}]], interrupt=KeyboardInterrupt())
        e = self.engine(website=source)
        result = e.run("full", limit=5)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(e.pending(), 1)
        saved = self.store.get_checkpoint("discovery:website")
        self.store.close(); self.store = Store(self.path)
        restored = self.engine()
        result = restored.run("full", limit=5)
        self.assertEqual(restored.pending(), 0)
        self.assertEqual(self.store.get_checkpoint("discovery:website"), saved)
        self.assertEqual(self.store.get("band", "1")["verification_status"], "verified")
    def test_api_missing_is_not_website_removal(self):
        api = FakeSource(records={("band", "1"): SourceMissing("api")})
        e = self.engine(api=api, seed_records=[band(1)])
        e.run("test")
        self.assertEqual(self.store.get("band", "1")["active_status"], "active")
        self.assertEqual(self.store.get("band", "1")["missing_count"], 0)
    def test_isolated_invalid_detail_preserves_good_children_and_retries_parent(self):
        site = FakeSource(records={("band", "1"): [band(1), {"entity_type": "bad", "entity_id": "2"}, {"entity_type": "label", "entity_id": "3", "name": "Synthetic label"}]})
        e = self.engine(website=site, seed_records=[band(1)])
        result = e.run("test")
        self.assertEqual(result["status"], "partial")
        self.assertIsNotNone(self.store.get("label", "3"))
        self.assertEqual(e.pending(), 1)
        self.assertEqual(len(site.fetched), 1)
        self.assertGreater(self.store.conn.execute("SELECT COUNT(*) FROM review_queue WHERE reason='invalid_entity'").fetchone()[0], 0)
    def test_invalid_discovery_row_reviewed_while_good_row_imports(self):
        site = FakeSource(pages=[[{"entity_type": "band", "entity_id": "", "_parse_error": "missing id"}, {**band(2), "_discovery_only": True}]])
        e = self.engine(website=site)
        result = e.run("test")
        self.assertEqual(result["status"], "partial")
        self.assertIsNotNone(self.store.get("band", "2"))
        self.assertTrue(self.store.get_checkpoint("discovery:website")["done"])
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM review_queue WHERE reason='invalid_discovery'").fetchone()[0], 1)
    def test_source_warning_retains_band_and_retry(self):
        site = FakeSource(records={("band", "1"): [band(1, _source_warnings=["discography unavailable"])]})
        e = self.engine(website=site, seed_records=[band(1)])
        result = e.run("test")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(e.pending(), 1)
        self.assertEqual(self.store.get("band", "1")["verification_status"], "verified")
    def test_block_stops_further_website_fetches(self):
        site = FakeSource(records={("band", "1"): SourceBlocked("403")})
        e = self.engine(website=site, seed_records=[band(1), band(2)])
        result = e.run("test")
        self.assertEqual(site.fetched, [("band", "1")])
        self.assertEqual(result["website_status"], "blocked")
        self.assertEqual(e.pending(), 2)
    def test_full_mode_finishes_cursor_without_reset_or_spin(self):
        site = FakeSource(pages=[[{**band(1), "_discovery_only": True}], [{**band(2), "_discovery_only": True}]])
        e = self.engine(website=site)
        result = e.run("full", limit=5)
        self.assertEqual(result["processed"], 2)
        self.assertEqual(len(site.discovered), 2)
        self.assertTrue(self.store.get_checkpoint("discovery:website")["done"])
        e.run("full", limit=5)
        self.assertEqual(len(site.discovered), 2)
    def test_stale_and_random_sample_scheduled(self):
        run = self.store.start_run()
        self.store.upsert(band(1), run, verified=True)
        self.store.upsert(band(2), run, verified=True)
        self.store.finish_run(run)
        self.store.conn.execute("UPDATE entities SET last_checked='2000-01-01T00:00:00Z' WHERE entity_id='1'")
        self.store.conn.commit()
        site = FakeSource()
        e = self.engine(website=site, sample_size=2)
        e.run("batch", limit=5)
        self.assertEqual(set(site.fetched), {("band", "1"), ("band", "2")})
    def test_old_related_jobs_get_reserved_slots_amid_priority_rechecks(self):
        site = FakeSource()
        e = self.engine(website=site)
        e.enqueue(band(99), "related", 20)
        for i in range(1, 6):
            e.enqueue(band(i), "sample", 2)
        e.run("test", limit=3)
        self.assertEqual(site.fetched, [("band", "1"), ("band", "2"), ("band", "99")])
    def test_upcoming_feed_and_released_transition(self):
        run = self.store.start_run()
        release = {"entity_type": "release", "entity_id": "5", "name": "Synthetic release", "source_url": "https://www.metal-archives.com/albums/5", "is_upcoming": True, "release_date": "2099-01-01"}
        upcoming = {**release, "entity_type": "upcoming", "status": "announced"}
        self.store.upsert(release, run, verified=True); self.store.upsert(upcoming, run, verified=True)
        self.store.finish_run(run)
        site = UpcomingSource([{**release, "_discovery_only": True}], records={("release", "5"): [{**release, "release_date": "2000-01-01", "is_upcoming": False}]})
        e = self.engine(website=site)
        result = e.run("batch")
        self.assertEqual(self.store.get("upcoming", "5")["status"], "released")
        self.assertTrue(self.store.get_checkpoint("discovery:upcoming")["done"])
        self.assertEqual(len(site.upcoming_calls), 1)
        self.assertGreater(self.store.conn.execute("SELECT COUNT(*) FROM changes WHERE change_type='upcoming_released'").fetchone()[0], 0)
    def test_three_absences_require_three_runs(self):
        run = self.store.start_run(); self.store.upsert(band(1), run, verified=True); self.store.finish_run(run)
        site = FakeSource(records={("band", "1"): SourceMissing("website")})
        e = self.engine(website=site, sample_size=1)
        for expected in ("missing", "missing", "removed"):
            e.run("batch")
            self.assertEqual(self.store.get("band", "1")["active_status"], expected)
    def test_unchanged_discovery_stubs_do_not_overwrite_verified_detail(self):
        run = self.store.start_run(); self.store.upsert(band(1, country="Retain"), run, verified=True); self.store.finish_run(run)
        site = FakeSource(records={("band", "1"): SourceUnavailable("temporary")}, pages=[[{**band(1, country="wrong"), "_discovery_only": True}]])
        e = self.engine(website=site)
        e.run("test")
        self.assertEqual(self.store.get("band", "1")["country"], "Retain")


if __name__ == "__main__": unittest.main()
