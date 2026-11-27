"""Finite jobs, persistent queue, and deterministic parsing. No model APIs."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone

from .sources import SourceBlocked, SourceMissing, SourceUnavailable

log = logging.getLogger(__name__)


class SyncEngine:
    def __init__(self, store, api, website, settings):
        self.store, self.api, self.website, self.settings = store, api, website, settings
        store.conn.execute("""CREATE TABLE IF NOT EXISTS work_queue (
            entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, record_json TEXT NOT NULL,
            reason TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 10,
            attempts INTEGER NOT NULL DEFAULT 0, last_attempt_run TEXT,
            PRIMARY KEY(entity_type, entity_id))""")
        store.conn.commit()

    def enqueue(self, record, reason="discovery", priority=10):
        if not isinstance(record, dict):
            raise ValueError("A queued record must be an entity object")
        if record.get("_parse_error"):
            raise ValueError(str(record["_parse_error"]))
        if record.get("entity_type") == "song":
            if not self.settings.get("include_songs", True):
                return
            # Songs have no independent verification page; their release does.
            if record.get("release_id"):
                record = {"entity_type": "release", "entity_id": str(record["release_id"])}
            else:
                raise ValueError("A song discovery needs its parent release ID")
        if record.get("entity_type") not in ("band", "release", "label", "artist"):
            return
        if not record.get("entity_id"):
            raise ValueError("A queued record needs a stable entity ID")
        self.store.conn.execute("""INSERT INTO work_queue
            (entity_type,entity_id,record_json,reason,priority) VALUES(?,?,?,?,?)
            ON CONFLICT(entity_type,entity_id) DO UPDATE SET
            priority=MIN(priority,excluded.priority)""",
            (record["entity_type"], str(record["entity_id"]), json.dumps(record), reason, priority))
        self.store.conn.commit()

    def _schedule_checks(self, run_id):
        days = int(self.settings.get("recheck_days", 90))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        limit = max(1, int(self.settings.get("max_records", 250)) // 3)
        rows = self.store.conn.execute("""SELECT entity_type,entity_id FROM entities
            WHERE entity_type IN ('band','release','label','artist')
            AND (last_checked IS NULL OR last_checked < ?) ORDER BY last_checked LIMIT ?""", (cutoff, limit))
        for row in rows.fetchall():
            self.enqueue(self.store.get(row[0], row[1]), "stale", 5)
        # SQLite samples rows without loading the catalog into Python or a model.
        rows = self.store.conn.execute("""SELECT entity_type,entity_id FROM entities
            WHERE entity_type IN ('band','release','label','artist')
            ORDER BY RANDOM() LIMIT ?""", (int(self.settings.get("sample_size", 10)),))
        for row in rows.fetchall():
            self.enqueue(self.store.get(row[0], row[1]), "sample", 2)
        for row in self.store.conn.execute("""SELECT entity_type,entity_id FROM entities
            WHERE entity_type IN ('band','release','label','artist') AND active_status='missing'
            ORDER BY last_checked LIMIT ?""", (limit,)).fetchall():
            self.enqueue(self.store.get(row[0], row[1]), "absence", 1)
        # Upcoming entities are materialized from release metadata by the store.
        for row in self.store.conn.execute("""SELECT entity_type,entity_id FROM entities
            WHERE entity_type='release' AND json_extract(data_json,'$.is_upcoming')=1
            LIMIT ?""", (limit,)).fetchall():
            self.enqueue(self.store.get(row[0], row[1]), "upcoming", 1)

    @staticmethod
    def _available(probe):
        return probe.get("available") is True or probe.get("status") in ("available", "ok", "healthy")

    def run(self, mode="test", limit=None, seconds=None):
        limit = int(limit or (5 if mode == "test" else self.settings.get("max_records", 250)))
        seconds = float(seconds or self.settings.get("max_seconds", 7200))
        if limit < 1 or seconds < 1:
            raise ValueError("Limits must be positive")
        run_id = self.store.start_run(mode)
        deadline = time.monotonic() + seconds
        metrics = {"records_discovered_api": 0, "records_discovered_website": 0,
                   "records_checked": 0, "records_verified_directly": 0,
                   "records_skipped": 0, "api_failures": 0, "website_failures": 0}
        errors, availability = [], {}
        for name, source in (("api", self.api), ("website", self.website)):
            result = {}
            try:
                result = source.probe()
                availability[name] = self._available(result)
                metrics[name + "_status"] = result.get("status", "available" if availability[name] else "unavailable")
            except (SourceUnavailable, OSError, ValueError) as exc:
                availability[name] = False
                metrics[name + "_status"] = "unavailable"
                errors.append(f"{name} probe: {type(exc).__name__}")
            if not availability[name]:
                metrics[name + "_failures"] += 1
                if result.get("error"):
                    errors.append(f"{name} probe: {result['error']}")
        for record in self.settings.get("seed_records", []):
            try:
                self.enqueue(record, "seed", 0)
            except (ValueError, TypeError, KeyError) as exc:
                self._record_error(record, run_id, "invalid_seed", exc)
                errors.append("invalid seed: " + type(exc).__name__)
        if mode != "test":
            self._schedule_checks(run_id)
        processed, pages = 0, 0
        failed_discovery = set()
        discoveries = [("api", "api", self.api.discover), ("website", "website", self.website.discover)]
        if hasattr(self.website, "discover_upcoming"):
            discoveries.append(("upcoming", "website", self.website.discover_upcoming))
        try:
            while processed < limit and time.monotonic() < deadline:
                # One small page per source per pass. Cursor advances only after durable enqueue.
                if pages == 0 or (mode == "full" and self.pending(run_id) == 0):
                    for discovery_name, name, discover in discoveries:
                        if not availability[name] or discovery_name in failed_discovery:
                            continue
                        key = "discovery:" + discovery_name
                        cursor = self.store.get_checkpoint(key, {})
                        if cursor.get("done") or cursor.get("complete"):
                            # A new scheduled pass starts another reconciliation cycle.
                            if pages == 0 and mode != "full":
                                cursor = {}
                            else:
                                continue
                        try:
                            records, next_cursor, total = discover(cursor, min(limit, int(self.settings.get("discovery_page_size",25))))
                            for record in records:
                                try:
                                    self.enqueue(record)
                                except (ValueError, TypeError, KeyError) as exc:
                                    errors.append(f"{discovery_name} invalid discovery: {type(exc).__name__}")
                                    self._record_error(record, run_id, "invalid_discovery", exc)
                            self.store.set_checkpoint(key, next_cursor)
                            metrics["records_discovered_" + name] += len(records)
                            if total is not None:
                                self.store.set_checkpoint("discovered_total:" + discovery_name, total)
                        except (SourceUnavailable, OSError, ValueError) as exc:
                            errors.append(f"{name} discovery: {type(exc).__name__}")
                            metrics[name + "_failures"] += 1
                            failed_discovery.add(discovery_name)
                            if isinstance(exc, SourceBlocked):
                                availability[name] = False
                                metrics[name + "_status"] = "blocked"
                    pages += 1
                # Reserve one third of slots for oldest least-attempted work,
                # so related entities cannot starve behind recurring rechecks
                # or a permanently failing older retry backlog.
                order = "attempts,rowid" if processed % 3 == 2 else "priority,attempts,entity_type,entity_id"
                jobs = self.store.conn.execute("""SELECT * FROM work_queue
                    WHERE last_attempt_run IS NULL OR last_attempt_run<>?
                    ORDER BY """ + order + " LIMIT 1", (run_id,)).fetchall()
                if not jobs:
                    if mode == "full" and any(availability.values()) and pages < limit:
                        cursors = [self.store.get_checkpoint("discovery:"+d,{}) for d,n,_ in discoveries if availability[n] and d not in failed_discovery]
                        if any(not (c.get("done") or c.get("complete")) for c in cursors):
                            continue
                    break
                job = dict(jobs[0])
                record = json.loads(job["record_json"])
                self.store.conn.execute("UPDATE work_queue SET last_attempt_run=?,attempts=attempts+1 WHERE entity_type=? AND entity_id=?",
                    (run_id, record["entity_type"], str(record["entity_id"])))
                self.store.conn.commit()
                processed += 1
                metrics["records_checked"] += 1
                completed = False
                item_errors = []
                # Fetch through API for broad discovery, then verify new/queued records directly.
                # Known website rechecks do not also need the API.
                if availability["api"] and job["reason"] in ("discovery", "seed") and record["entity_type"] in ("band", "release"):
                    try:
                        for item in self.api.fetch(record["entity_type"], record["entity_id"], record.get("source_url")):
                            self._accept_safely(item, run_id, "api", False, item_errors)
                    except SourceMissing:
                        pass  # API absence is never evidence of website removal.
                    except (SourceUnavailable, OSError, ValueError) as exc:
                        metrics["api_failures"] += 1
                        errors.append(f"api detail {record['entity_type']}:{record['entity_id']}: {type(exc).__name__}")
                        if isinstance(exc, SourceBlocked):
                            availability["api"] = False
                            metrics["api_status"] = "blocked"
                if availability["website"]:
                    try:
                        found = self.website.fetch(record["entity_type"], record["entity_id"], record.get("source_url"))
                        for item in found:
                            self._accept_safely(item, run_id, "website", True, item_errors)
                        if any(isinstance(item, dict) and item.get("_source_blocked") for item in found):
                            availability["website"] = False
                            metrics["website_status"] = "blocked"
                            metrics["website_failures"] += 1
                        completed = any(not r.get("_discovery_only") and r.get("entity_type")==record["entity_type"]
                                        and str(r.get("entity_id"))==str(record["entity_id"]) for r in found if isinstance(r,dict))
                        if completed:
                            metrics["records_verified_directly"] += 1
                        else:
                            raise ValueError("Detail parser did not produce requested stable ID")
                    except SourceMissing as exc:
                        self.store.mark_absent(record["entity_type"], record["entity_id"], run_id, status_code=exc.status)
                        completed = True  # Requeued by periodic checks, never three times in one run.
                    except (SourceUnavailable, OSError, ValueError) as exc:
                        errors.append(f"website detail {record['entity_type']}:{record['entity_id']}: {type(exc).__name__}")
                        metrics["website_failures"] += 1
                        if isinstance(exc, SourceBlocked):
                            availability["website"] = False
                            metrics["website_status"] = "blocked"
                if item_errors:
                    errors.extend(item_errors)
                    completed = False  # Parent is retained for child/parser retry next run.
                if completed:
                    self.store.conn.execute("DELETE FROM work_queue WHERE entity_type=? AND entity_id=?", (record["entity_type"], str(record["entity_id"])))
                    self.store.conn.commit()
                else:
                    metrics["records_skipped"] += 1
                log.info(json.dumps({"event":"progress","run_id":run_id,"processed":processed,"pending":self.pending()}))
                if not any(availability.values()):
                    break
        except (KeyboardInterrupt, Exception) as exc:
            errors.append("interrupted: " + type(exc).__name__)
            if not isinstance(exc, KeyboardInterrupt):
                log.exception("Sync stopped; durable queue retained")
        metrics["records_discovered"] = metrics["records_discovered_api"] + metrics["records_discovered_website"]
        metrics["records_remaining"] = self.pending()
        metrics["estimated_coverage"] = None  # No validated global denominator exists.
        metrics["estimated_confidence"] = self.store.summary()["verification_ratio"]
        total_bands = self.store.get_checkpoint("discovered_total:website")
        if total_bands:
            bands = self.store.conn.execute("SELECT COUNT(*) FROM entities WHERE entity_type='band'").fetchone()[0]
            metrics["estimated_band_coverage"] = min(1, bands / total_bands)
            metrics["band_coverage_denominator"] = total_bands
        metrics["checkpoint"] = {n:self.store.get_checkpoint("discovery:"+n,{}) for n,_,_ in discoveries}
        metrics["error_summary"] = errors[:100]
        metrics["status"] = "partial" if errors or not all(availability.values()) or self.pending() else "success"
        self.store.finish_run(run_id, **metrics)
        return {"run_id":run_id, "processed":processed, **metrics}

    def _accept(self, record, run_id, source, verified):
        if record.get("entity_type") == "song" and not self.settings.get("include_songs", True):
            return
        if record.get("_discovery_only"):
            self.enqueue(record, "related", 20)
            return
        clean = {key:value for key,value in record.items() if not key.startswith("_")}
        self.store.upsert(clean, run_id, source=source, verified=verified)
        for warning in record.get("_source_warnings", []):
            self.store.add_review(record["entity_type"], record["entity_id"], "source_incomplete", run_id,
                details={"warning":warning}, source_url=record.get("source_url",""))

    def _record_error(self, record, run_id, reason, exc):
        record = record if isinstance(record, dict) else {}
        self.store.add_review(str(record.get("entity_type", "unknown")), str(record.get("entity_id", "unknown")),
            reason, run_id, details={"error_type": type(exc).__name__, "message": str(exc)},
            source_url=record.get("source_url", ""))

    def _accept_safely(self, record, run_id, source, verified, errors):
        try:
            if not isinstance(record, dict):
                raise ValueError("Source item is not an entity object")
            self._accept(record, run_id, source, verified)
            for warning in record.get("_source_warnings", []):
                errors.append(f"{source} incomplete child discovery: {warning}")
        except (ValueError, TypeError, KeyError) as exc:
            errors.append(f"{source} invalid entity: {type(exc).__name__}")
            self._record_error(record, run_id, "invalid_entity", exc)

    def pending(self, run_id=None):
        if run_id:
            return self.store.conn.execute("SELECT COUNT(*) FROM work_queue WHERE last_attempt_run IS NULL OR last_attempt_run<>?",(run_id,)).fetchone()[0]
        return self.store.conn.execute("SELECT COUNT(*) FROM work_queue").fetchone()[0]
