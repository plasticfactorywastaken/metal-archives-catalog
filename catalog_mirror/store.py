"""Durable catalog, per-field history, and user-owned classification controls.

Every mutation commits independently. Workers can commit a checkpoint after an
upsert: a crash between those operations safely repeats an idempotent upsert.
Missing fields never erase existing source values. A failed request is never
evidence of removal; mark_absent accepts only confirmed 404/410 observations.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .genres import PROTECTED_FIELDS, apply_overrides, classify, genre_fields


ENTITY_TYPES = ("band", "release", "song", "label", "artist", "relationship", "upcoming", "review")
METADATA = frozenset({
    "entity_type", "entity_id", "first_seen", "last_seen", "last_checked", "source_updated_at",
    "content_hash", "active_status", "verification_status", "raw_source_ref", "source_system",
    "last_changed_at", "last_changed_run", "missing_count", "last_missing_run", "source",
    "ecommerce_primary_class", "ecommerce_secondary_tags", "classification_status",
    "classification_confidence", "taxonomy_version", "override_fields", "observed_at",
})
DERIVED_GENRES = frozenset({"primary_genre", "secondary_genres", "genre_count", *(f"genre_{n}" for n in range(2, 9))})
COUNTERS = frozenset({
    "records_discovered", "records_checked", "records_inserted", "records_updated", "records_unchanged",
    "records_removed", "records_reappeared", "conflicts", "review_items",
})


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def content_hash(record: dict) -> str:
    """Hash source content only, with ordered arrays intentionally left intact."""
    stable = {key: value for key, value in record.items() if key not in METADATA and key not in DERIVED_GENRES}
    return hashlib.sha256(canonical_json(stable).encode("utf-8")).hexdigest()


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))
        self.conn.commit()

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def start_run(self, mode: str = "test", **metrics) -> str:
        run_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:12]}"
        with self.conn:
            self.conn.execute("INSERT INTO sync_runs(run_id,mode,started_at) VALUES(?,?,?)", (run_id, mode, utcnow()))
        if metrics:
            self.update_run(run_id, **metrics)
        return run_id

    def _increment(self, run_id: str, counter: str, amount: int = 1):
        if counter not in COUNTERS:
            raise ValueError(f"Unsupported counter: {counter}")
        self.conn.execute(f"UPDATE sync_runs SET {counter}={counter}+? WHERE run_id=?", (amount, run_id))

    def increment(self, run_id: str, counter: str, amount: int = 1):
        with self.conn:
            self._increment(run_id, counter, amount)

    def update_run(self, run_id: str, **metrics):
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(sync_runs)")}
        row = self.conn.execute("SELECT metrics_json FROM sync_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError(f"Unknown run: {run_id}")
        extras = json.loads(row[0])
        updates = {}
        for key, value in metrics.items():
            if key in columns and key not in {"run_id", "metrics_json"}:
                updates[key] = canonical_json(value) if isinstance(value, (list, dict)) else value
            else:
                extras[key] = value
        updates["metrics_json"] = canonical_json(extras)
        with self.conn:
            self.conn.execute(
                "UPDATE sync_runs SET " + ",".join(f"{key}=?" for key in updates) + " WHERE run_id=?",
                (*updates.values(), run_id),
            )

    def finish_run(self, run_id: str, **metrics):
        metrics.setdefault("status", "success")
        metrics.setdefault("finished_at", utcnow())
        self.update_run(run_id, **metrics)

    def _change(self, run_id, entity_type, entity_id, name, kind, field, previous, current, source_url, timestamp):
        self.conn.execute(
            "INSERT INTO changes(run_id,detected_at,entity_type,entity_id,entity_name,change_type,changed_field,previous_value,new_value,source_url) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (run_id, timestamp, entity_type, entity_id, name, kind, field,
             canonical_json(previous), canonical_json(current), source_url),
        )

    def add_review(self, entity_type: str, entity_id: str, reason: str, run_id: str | None = None,
                   details: dict | str | None = None, field: str = "", source_url: str = "", review_key: str | None = None) -> int:
        """Deduplicate identical exceptions; changed evidence creates a new item."""
        details = details or {}
        review_key = review_key or hashlib.sha256(canonical_json([entity_type, str(entity_id), reason, field, details]).encode()).hexdigest()
        timestamp = utcnow()
        with self.conn:
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO review_queue(review_key,created_at,updated_at,run_id,entity_type,entity_id,reason,field,details_json,source_url) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (review_key, timestamp, timestamp, run_id, entity_type, str(entity_id), reason, field, canonical_json(details), source_url),
            )
            if cursor.rowcount and run_id:
                self._increment(run_id, "review_items")
                if reason == "source_conflict":
                    self._increment(run_id, "conflicts")
            self.conn.execute("UPDATE review_queue SET updated_at=? WHERE review_key=?", (timestamp, review_key))
            return self.conn.execute("SELECT review_id FROM review_queue WHERE review_key=?", (review_key,)).fetchone()[0]

    def _review_no_commit(self, entity_type, entity_id, reason, run_id, details, field, source_url, timestamp):
        key = hashlib.sha256(canonical_json([entity_type, entity_id, reason, field, details]).encode()).hexdigest()
        cursor = self.conn.execute(
            "INSERT OR IGNORE INTO review_queue(review_key,created_at,updated_at,run_id,entity_type,entity_id,reason,field,details_json,source_url) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (key, timestamp, timestamp, run_id, entity_type, entity_id, reason, field, canonical_json(details), source_url),
        )
        if cursor.rowcount:
            self._increment(run_id, "review_items")
            if reason == "source_conflict":
                self._increment(run_id, "conflicts")
        return bool(cursor.rowcount)

    def upsert(self, record: dict, run_id: str, source: str = "website", verified: bool = False) -> str:
        entity_type = record.get("entity_type")
        entity_id = str(record.get("entity_id", ""))
        if entity_type not in ENTITY_TYPES or not entity_id:
            raise ValueError("An allowed entity_type and a nonempty stable entity_id are required")
        if not self.conn.execute("SELECT 1 FROM sync_runs WHERE run_id=?", (run_id,)).fetchone():
            raise ValueError(f"Unknown run: {run_id}")
        timestamp = utcnow()
        existing = self.conn.execute("SELECT * FROM entities WHERE entity_type=? AND entity_id=?", (entity_type, entity_id)).fetchone()
        old_data = json.loads(existing["data_json"]) if existing else {}
        # None means unavailable, not an assertion that the old value is gone.
        incoming = {key: value for key, value in record.items() if key not in METADATA and key not in DERIVED_GENRES and value is not None}
        if "title" in incoming and "name" not in incoming:
            incoming["name"] = incoming["title"]
        if "ma_url" in incoming and "source_url" not in incoming:
            incoming["source_url"] = incoming.pop("ma_url")
        data = dict(old_data)
        competing = self.conn.execute(
            "SELECT source,data_json,verified FROM source_observations WHERE entity_type=? AND entity_id=? AND source!=? AND run_id=?",
            (entity_type, entity_id, source, run_id),
        ).fetchall()
        conflicts = []
        for observation in competing:
            previous = json.loads(observation["data_json"])
            for field in sorted(incoming.keys() & previous.keys()):
                if field in {"source_url", "ma_url"} or incoming[field] == previous[field]:
                    continue
                conflicts.append((field, {source: incoming[field], observation["source"]: previous[field]}))
        for key, value in incoming.items():
            if existing and existing["verification_status"] == "verified" and not verified and key in old_data and old_data[key] != value:
                continue
            data[key] = value
        if "genres_raw" in data or "genres_json" in data:
            # A newly received raw string invalidates previously derived tokens.
            ordered = incoming.get("genres_json") if "genres_raw" in incoming else data.get("genres_json")
            data.update(genre_fields(data.get("genres_raw"), ordered))
        digest = content_hash(data)
        name = str(data.get("name", data.get("title", "")))
        source_url = str(data.get("source_url", ""))
        status = "inserted" if existing is None else "updated" if digest != existing["content_hash"] else "unchanged"
        reappeared = bool(existing and existing["active_status"] != "active" and verified)
        active_status = "active" if verified or not existing else existing["active_status"]
        missing_count = 0 if verified or not existing else existing["missing_count"]
        last_missing_run = None if verified or not existing else existing["last_missing_run"]
        checked = timestamp if verified else (existing["last_checked"] if existing else None)
        verification = "verified" if verified else (existing["verification_status"] if existing else "pending")
        changed_at = timestamp if status != "unchanged" or reappeared else existing["last_changed_at"]
        changed_run = run_id if status != "unchanged" or reappeared else existing["last_changed_run"]
        with self.conn:
            self.conn.execute(
                "INSERT INTO source_observations(entity_type,entity_id,source,data_json,content_hash,checked_at,run_id,verified) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(entity_type,entity_id,source) DO UPDATE SET data_json=excluded.data_json,content_hash=excluded.content_hash,checked_at=excluded.checked_at,run_id=excluded.run_id,verified=excluded.verified",
                (entity_type, entity_id, source, canonical_json(incoming), content_hash(incoming), timestamp, run_id, int(verified)),
            )
            self.conn.execute(
                "INSERT INTO entities(entity_type,entity_id,name,source_url,source_system,data_json,first_seen,last_seen,last_checked,source_updated_at,content_hash,active_status,verification_status,raw_source_ref,missing_count,last_missing_run,last_changed_at,last_changed_run) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(entity_type,entity_id) DO UPDATE SET name=excluded.name,source_url=excluded.source_url,source_system=excluded.source_system,data_json=excluded.data_json,last_seen=excluded.last_seen,last_checked=excluded.last_checked,source_updated_at=excluded.source_updated_at,content_hash=excluded.content_hash,active_status=excluded.active_status,verification_status=excluded.verification_status,raw_source_ref=excluded.raw_source_ref,missing_count=excluded.missing_count,last_missing_run=excluded.last_missing_run,last_changed_at=excluded.last_changed_at,last_changed_run=excluded.last_changed_run",
                (entity_type, entity_id, name, source_url, record.get("source_system", "Metal-Archives"), canonical_json(data),
                 existing["first_seen"] if existing else timestamp, timestamp, checked,
                 record.get("source_updated_at") or (existing["source_updated_at"] if existing else None), digest, active_status,
                 verification, record.get("raw_source_ref") or (existing["raw_source_ref"] if existing else None), missing_count, last_missing_run, changed_at, changed_run),
            )
            self._increment(run_id, "records_checked")
            self._increment(run_id, f"records_{status}")
            if status == "inserted":
                self._change(run_id, entity_type, entity_id, name, "new", "*", None, data, source_url, timestamp)
            elif status == "updated":
                for field in sorted(set(old_data) | set(data)):
                    if old_data.get(field) == data.get(field):
                        continue
                    kind = self._field_change_kind(field, old_data.get(field), data.get(field))
                    self._change(run_id, entity_type, entity_id, name, kind, field, old_data.get(field), data.get(field), source_url, timestamp)
            if reappeared:
                self._increment(run_id, "records_reappeared")
                self._change(run_id, entity_type, entity_id, name, "reappeared", "active_status", existing["active_status"], "active", source_url, timestamp)
            for field, details in conflicts:
                if self._review_no_commit(entity_type, entity_id, "source_conflict", run_id, details, field, source_url, timestamp):
                    self._change(run_id, entity_type, entity_id, name, "source_conflict", field, {key: value for key, value in details.items() if key != source}, incoming[field], source_url, timestamp)
            if not verified and existing is None:
                self._review_no_commit(entity_type, entity_id, "verification_pending", run_id,
                                       {"source": source}, "", source_url, timestamp)
            if verified:
                self.conn.execute("UPDATE review_queue SET review_status='resolved',updated_at=? WHERE entity_type=? AND entity_id=? AND reason='verification_pending'", (timestamp, entity_type, entity_id))
        # An upcoming entry already imported from this release must not remain
        # 'announced' after a verified release-page observation says released.
        if entity_type == "release" and verified and incoming.get("is_upcoming") is False:
            upcoming = self.get("upcoming", entity_id)
            if upcoming and (upcoming.get("status") != "released" or upcoming.get("is_upcoming") is not False):
                self.upsert({"entity_type": "upcoming", "entity_id": entity_id, "status": "released", "is_upcoming": False},
                            run_id, source=source, verified=True)
        return "reappeared" if reappeared else status

    @staticmethod
    def _field_change_kind(field, previous, current):
        if field in {"genres_raw", "genres_json"}:
            return "genre_changed"
        if field == "primary_genre":
            return "primary_genre_changed"
        if field in {"label_id", "label_name"}:
            return "label_changed"
        if field in {"release_date", "release_year", "expected_release_date"}:
            return "release_date_changed"
        if field == "status":
            return "status_changed"
        if field == "is_upcoming" and previous and not current:
            return "upcoming_released"
        return "updated"

    def mark_absent(self, entity_type: str, entity_id: str, run_id: str, strong: bool = False, status_code: int = 404) -> str:
        """Soft-removal after three separate failed observations, or an HTTP 410.

        Never call on timeout, parse failure, 403, 429, or source-list absence.
        Explicit status validation makes accidental mass removal harder.
        """
        if status_code not in {404, 410}:
            return "unavailable"
        row = self.conn.execute("SELECT * FROM entities WHERE entity_type=? AND entity_id=?", (entity_type, str(entity_id))).fetchone()
        if row is None:
            return "unknown"
        if row["last_missing_run"] == run_id:
            return row["active_status"]
        timestamp = utcnow()
        count = row["missing_count"] + 1
        status = "removed" if count >= 3 or (strong and status_code == 410) else "missing"
        with self.conn:
            self.conn.execute(
                "UPDATE entities SET active_status=?,missing_count=?,last_missing_run=?,last_checked=?,last_changed_at=CASE WHEN active_status!=? THEN ? ELSE last_changed_at END,last_changed_run=CASE WHEN active_status!=? THEN ? ELSE last_changed_run END WHERE entity_type=? AND entity_id=?",
                (status, count, run_id, timestamp, status, timestamp, status, run_id, entity_type, str(entity_id)),
            )
            self._increment(run_id, "records_checked")
            if row["active_status"] != status:
                self._change(run_id, entity_type, str(entity_id), row["name"], "removed" if status == "removed" else "soft_missing", "active_status", row["active_status"], status, row["source_url"], timestamp)
                if status == "removed":
                    self._increment(run_id, "records_removed")
        return status

    def set_checkpoint(self, key: str, value):
        with self.conn:
            self.conn.execute("INSERT INTO checkpoints VALUES(?,?,?) ON CONFLICT(checkpoint_key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at", (key, canonical_json(value), utcnow()))

    def get_checkpoint(self, key: str, default=None):
        row = self.conn.execute("SELECT value_json FROM checkpoints WHERE checkpoint_key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_genre_mapping(self, original_genre: str, ecommerce_classification: str = "", secondary_tags=None,
                          confidence: float = 0.0, review_status: str = "needs_review", notes: str = "", taxonomy_version: str = "1", last_updated: str | None = None):
        if not 0 <= float(confidence) <= 1:
            raise ValueError("Genre confidence must be between 0 and 1")
        tags = secondary_tags or []
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except ValueError:
                tags = [tag.strip() for tag in tags.split(";") if tag.strip()]
        with self.conn:
            self.conn.execute("INSERT INTO genre_map VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(original_genre) DO UPDATE SET ecommerce_classification=excluded.ecommerce_classification,secondary_tags=excluded.secondary_tags,confidence=excluded.confidence,review_status=excluded.review_status,notes=excluded.notes,taxonomy_version=excluded.taxonomy_version,last_updated=excluded.last_updated",
                              (original_genre, ecommerce_classification, canonical_json(tags), confidence, review_status, notes, str(taxonomy_version), last_updated or utcnow()))

    def set_override(self, entity_type: str, entity_id: str, field: str, override_value, reason: str = "", reviewer: str = "", updated_at: str | None = None):
        if entity_type not in ENTITY_TYPES:
            raise ValueError("Unsupported entity type")
        if field in PROTECTED_FIELDS:
            raise ValueError(f"Cannot overwrite source provenance or ordered genres: {field}")
        if field == "classification_confidence" and not 0 <= float(override_value) <= 1:
            raise ValueError("Classification confidence must be between 0 and 1")
        with self.conn:
            self.conn.execute("INSERT INTO classification_overrides VALUES(?,?,?,?,?,?,?) ON CONFLICT(entity_type,entity_id,field) DO UPDATE SET override_value=excluded.override_value,reason=excluded.reason,reviewer=excluded.reviewer,updated_at=excluded.updated_at",
                              (entity_type, str(entity_id), field, canonical_json(override_value), reason, reviewer, updated_at or utcnow()))

    def delete_override(self, entity_type: str, entity_id: str, field: str):
        with self.conn:
            self.conn.execute("DELETE FROM classification_overrides WHERE entity_type=? AND entity_id=? AND field=?", (entity_type, str(entity_id), field))

    def genre_mappings(self) -> dict:
        result = {}
        for row in self.conn.execute("SELECT * FROM genre_map ORDER BY original_genre"):
            value = dict(row)
            value["secondary_tags"] = json.loads(value["secondary_tags"])
            result[value["original_genre"]] = value
        return result

    def overrides(self, entity_type=None, entity_id=None) -> list[dict]:
        sql, params = "SELECT * FROM classification_overrides", []
        if entity_type is not None:
            sql += " WHERE entity_type=?"
            params.append(entity_type)
        if entity_id is not None:
            sql += " AND entity_id=?" if params else " WHERE entity_id=?"
            params.append(str(entity_id))
        result = []
        for row in self.conn.execute(sql, params):
            value = dict(row)
            value["override_value"] = json.loads(value["override_value"])
            result.append(value)
        return result

    def _flatten(self, row, mappings=None) -> dict:
        result = json.loads(row["data_json"])
        result.update({key: row[key] for key in row.keys() if key != "data_json"})
        result.update(genre_fields(result.get("genres_raw"), result.get("genres_json")))
        result.update(classify(result, self.genre_mappings() if mappings is None else mappings))
        # Stable names make cross-entity filters useful without loading a second
        # full table into the browser or the Python process.
        for kind, id_field, name_field in (("band", "band_id", "band_name"), ("release", "release_id", "release_title"), ("label", "label_id", "label_name"), ("artist", "artist_id", "artist_name")):
            if result.get(id_field) and not result.get(name_field):
                target = self.conn.execute("SELECT name FROM entities WHERE entity_type=? AND entity_id=?", (kind, str(result[id_field]))).fetchone()
                if target:
                    result[name_field] = target[0]
        result["ma_url"] = result["source_url"]
        return apply_overrides(result, self.overrides(row["entity_type"], row["entity_id"]))

    def get(self, entity_type: str, entity_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM entities WHERE entity_type=? AND entity_id=?", (entity_type, str(entity_id))).fetchone()
        return self._flatten(row) if row else None

    def iter_entities(self, entity_type: str | None = None):
        sql, params = "SELECT * FROM entities", []
        if entity_type:
            sql += " WHERE entity_type=?"
            params.append(entity_type)
        sql += " ORDER BY entity_type,entity_id"
        mappings = self.genre_mappings()
        for row in self.conn.execute(sql, params):
            yield self._flatten(row, mappings)

    def iter_changes(self, run_id: str | None = None, limit: int | None = None):
        sql, params = "SELECT * FROM changes", []
        if run_id:
            sql += " WHERE run_id=?"
            params.append(run_id)
        sql += " ORDER BY change_id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        for row in self.conn.execute(sql, params):
            value = dict(row)
            for field in ("previous_value", "new_value"):
                value[field] = json.loads(value[field]) if value[field] is not None else None
            value["changed_fields"] = [value["changed_field"]]
            yield value

    def runs(self, limit: int | None = None) -> list[dict]:
        sql = "SELECT * FROM sync_runs ORDER BY started_at DESC"
        rows = self.conn.execute(sql + (" LIMIT ?" if limit is not None else ""), (limit,) if limit is not None else ())
        result = []
        for row in rows:
            value = dict(row)
            value.update(json.loads(value.pop("metrics_json")))
            result.append(value)
        return result

    def summary(self, run_id: str | None = None) -> dict:
        runs = self.runs(limit=None if run_id else 1)
        run = next((value for value in runs if value["run_id"] == run_id), {}) if run_id else (runs[0] if runs else {})
        counts = {row[0]: row[1] for row in self.conn.execute("SELECT entity_type,COUNT(*) FROM entities GROUP BY entity_type")}
        total = sum(counts.values())
        verified = self.conn.execute("SELECT COUNT(*) FROM entities WHERE verification_status='verified'").fetchone()[0]
        unresolved = self.conn.execute("SELECT COUNT(*) FROM review_queue WHERE review_status='unresolved'").fetchone()[0]
        changes = {}
        if run:
            changes = {row[0]: row[1] for row in self.conn.execute("SELECT change_type,COUNT(DISTINCT entity_type || ':' || entity_id) FROM changes WHERE run_id=? GROUP BY change_type", (run["run_id"],))}
        summary = {
            "total_records": total,
            "entity_counts": counts,
            "verified_records": verified,
            "records_processed": total,
            "records_remaining": run.get("records_remaining"),
            "checkpoint": run.get("checkpoint"),
            "errors": run.get("error_summary", ""),
            "estimated_coverage": run.get("estimated_coverage"),
            "estimated_confidence": run.get("estimated_confidence"),
            "verification_ratio": verified / total if total else None,
            "review_items": unresolved,
            "latest_run": run,
            "weekly": {
                "records_checked": run.get("records_checked", 0),
                "new_records": run.get("records_inserted", 0),
                "updated_records": run.get("records_updated", 0),
                "removed_records": run.get("records_removed", 0),
                "reappeared_records": run.get("records_reappeared", 0),
                "genre_changes": changes.get("genre_changed", 0),
                "primary_genre_changes": changes.get("primary_genre_changed", 0),
                "label_changes": changes.get("label_changed", 0),
                "new_upcoming_releases": 0,
                "upcoming_now_released": changes.get("upcoming_released", 0),
                "api_failures": run.get("api_failures", 0),
                "website_failures": run.get("website_failures", 0),
                "conflicts": run.get("conflicts", 0),
                "review_items": run.get("review_items", 0),
            },
        }
        if run:
            summary["weekly"]["upcoming_now_released"] = self.conn.execute(
                "SELECT COUNT(DISTINCT entity_id) FROM changes WHERE run_id=? AND change_type='upcoming_released'", (run["run_id"],)
            ).fetchone()[0]
            summary["weekly"]["new_upcoming_releases"] = self.conn.execute(
                "SELECT COUNT(DISTINCT entity_id) FROM changes WHERE run_id=? AND change_type='new' AND (entity_type='upcoming' OR (entity_type='release' AND json_extract(new_value,'$.is_upcoming')=1))", (run["run_id"],)
            ).fetchone()[0]
        return summary

    def backup(self, target: str | Path):
        """Consistent SQLite snapshot, including data committed into the WAL."""
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        destination = sqlite3.connect(target)
        try:
            self.conn.backup(destination)
        finally:
            destination.close()
