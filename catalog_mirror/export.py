"""Streaming, bounded partitions and atomic publication of an entire export.

SQLite remains authoritative. JSON preserves original values; CSV cells are
formula-neutralized for safe opening in spreadsheets. Export is built in a
sibling staging directory and replaces the published tree only after success.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import itertools
import json
import os
import shutil
import uuid
from functools import lru_cache
from pathlib import Path

from .store import ENTITY_TYPES, Store, canonical_json, utcnow


def csv_cell(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        return canonical_json(value)
    if isinstance(value, str) and value[:1] in {"=", "+", "-", "@", "\t", "\r", "\n"}:
        return "'" + value
    return value


def chunks(iterator, size):
    iterator = iter(iterator)
    while True:
        batch = list(itertools.islice(iterator, size))
        if not batch:
            return
        yield batch


def _payload(path: Path, data: bytes, compressed: bool = False) -> dict:
    """Write one bounded partition and describe both stored and decoded bytes.

    An empty gzip filename and fixed mtime make repeated exports byte-identical
    across platforms. Hashes refer to exact file bytes, including CSV's UTF-8 BOM.
    """
    if compressed:
        with path.open("wb") as stream:
            with gzip.GzipFile(filename="", mode="wb", fileobj=stream, compresslevel=6, mtime=0) as zipped:
                zipped.write(data)
    else:
        path.write_bytes(data)
    stored = path.read_bytes()
    return {
        "encoding": "gzip" if compressed else "identity",
        "bytes": len(stored),
        "uncompressed_bytes": len(data),
        "sha256": hashlib.sha256(stored).hexdigest(),
        "uncompressed_sha256": hashlib.sha256(data).hexdigest(),
    }


def write_csv(path: Path, rows: list[dict], fieldnames=None, compressed: bool = False):
    fieldnames = fieldnames or sorted({key for row in rows for key in row})
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows({key: csv_cell(row.get(key)) for key in fieldnames} for row in rows)
    return _payload(path, stream.getvalue().encode("utf-8-sig"), compressed)


def _json(path, value):
    data = (canonical_json(value) + "\n").encode("utf-8")
    return _payload(path, data)["sha256"]


def _cleanup_generated(path: Path, expected_parent: Path, prefix: str):
    """Never recursively clean an arbitrary caller-supplied output directory."""
    resolved = path.resolve()
    if resolved.parent != expected_parent.resolve() or not resolved.name.startswith(prefix):
        raise ValueError("Refusing cleanup outside the generated export directory")
    if resolved.exists():
        shutil.rmtree(resolved)


def with_search_facets(record: dict, lookup) -> dict:
    """Add explicit joined search fields without rewriting source observations.

    Releases often have no genre field; songs may have only a release ID.
    Prefer the entity's own ordered genres, then its release's, then one known
    parent band's. Never choose one band from a multi-band relationship.
    Per-entity manual classifications take precedence over inherited overlays.
    """
    result = dict(record)
    kind = record["entity_type"]
    release = None
    if kind in {"song", "upcoming"} and record.get("release_id"):
        release = lookup("release", str(record["release_id"]))
    band_id = record.get("band_id") or (release or {}).get("band_id")
    bands = record.get("band_ids") or (release or {}).get("band_ids") or []
    ambiguous_band = isinstance(bands, list) and len(set(map(str, bands))) > 1
    band = lookup("band", str(band_id)) if band_id and not ambiguous_band and kind in {"release", "song", "upcoming"} else None
    if release:
        result["facet_release_id"] = release["entity_id"]
        result["facet_release_title"] = release.get("name", "")
        for field in ("label_id", "label_name"):
            if not record.get(field) and release.get(field):
                result["facet_" + field] = release[field]
    label_id = record.get("label_id") or result.get("facet_label_id")
    if label_id:
        label = lookup("label", str(label_id))
        if label:
            result["facet_label_id"] = label["entity_id"]
            result["facet_label_name"] = label.get("name", "")
    if band:
        result["facet_band_id"] = band["entity_id"]
        result["facet_band_name"] = band.get("name", "")
        for field, source_field in (("country", "country"), ("location", "location"), ("band_status", "status"), ("formed_in", "formed_in")):
            if band.get(source_field) is not None:
                result["facet_" + field] = band[source_field]
    if kind == "relationship":
        for side in ("source", "target"):
            target_kind, target_id = record.get(side + "_entity_type"), record.get(side + "_entity_id")
            if target_kind in ENTITY_TYPES and target_id:
                target = lookup(target_kind, str(target_id))
                if target:
                    result[side + "_entity_name"] = target.get("name", "")
    own_genres = record.get("genres_json") or []
    origin = record if own_genres else next((parent for parent in (release, band) if parent and parent.get("genres_json")), None)
    ordered = list(origin.get("genres_json", [])) if origin else []
    result["facet_genres_json"] = ordered
    result["facet_primary_genre"] = ordered[0] if ordered else ""
    result["genre_origin"] = "source" if own_genres else origin["entity_type"] if origin else "unknown"
    result["genre_origin_entity_id"] = origin["entity_id"] if origin else None
    result["genre_source_url"] = origin.get("source_url", "") if origin else ""
    result["genre_origin_name"] = origin.get("name", "") if origin else ""
    if origin and origin is not record:
        overrides = set(record.get("override_fields", []))
        for field in ("ecommerce_primary_class", "ecommerce_secondary_tags", "classification_status", "classification_confidence", "taxonomy_version"):
            own = field in overrides or (field == "classification_status" and bool(overrides))
            result["facet_" + field] = record.get(field) if own else origin.get(field)
        result["classification_origin"] = "entity_override" if overrides else result["genre_origin"]
        # A parent genre edit changes the visible facet without inventing an
        # update to this entity's raw fields or content hash.
        changed = max((item for item in (record, origin) if item.get("last_changed_at")), key=lambda item: item["last_changed_at"], default=record)
        result["facet_last_changed_at"] = changed.get("last_changed_at")
        result["facet_last_changed_run"] = changed.get("last_changed_run")
    return result


def export_catalog(store: Store, output_dir: str | Path, partition_size: int = 1000,
                   include_sqlite: bool = False, max_bytes: int = 900_000_000,
                   compressed: bool = True) -> dict:
    if not 1 <= partition_size <= 1000:
        raise ValueError("partition_size must be between 1 and 1000")
    target = Path(output_dir).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    prefix = f".{target.name}-export-"
    staging = target.parent / f"{prefix}{uuid.uuid4().hex}"
    backup = target.parent / f"{prefix}backup-{uuid.uuid4().hex}"
    staging.mkdir()
    bytes_written = 0

    def budget(*paths):
        nonlocal bytes_written
        bytes_written += sum(path.stat().st_size for path in paths)
        if bytes_written > max_bytes:
            raise ValueError(f"Export exceeds the configured free-hosting budget ({max_bytes} bytes); the previous export is retained")

    manifest = {
        "version": 2 if compressed else 1,
        "export_compression": "gzip" if compressed else "identity",
        "generated_at": utcnow(),
        "total_records": 0,
        "partitions": [],
        "summary": "summary.json",
        "runs": "runs.json",
        "changes": "changes.json",
        "reviews": "reviews.json",
        "change_partitions": [],
        "downloads": {},
        "genre_facets": "facet_* fields may inherit a release or single parent band's metadata. genres_raw, genres_json, primary_genre and content_hash remain the entity's own source values. genre_origin and genre_source_url identify inheritance.",
        "csv_formula_escaping": "Leading spreadsheet formula characters are prefixed with an apostrophe; JSON and SQLite preserve exact values.",
        "download_formats": {
            "entity_csv": "Complete entity CSV partitions (.csv.gz); decompress gzip before opening in a spreadsheet." if compressed else "Complete entity CSV partitions (.csv).",
            "history_csv": "Complete per-field change history CSV partitions (.csv.gz); no historical rows are omitted." if compressed else "Complete per-field change history CSV partitions (.csv); no historical rows are omitted.",
        },
    }
    # Snapshot origin belongs to persisted catalog state, not the latest run.
    # A subsequent live-sync attempt must not erase this historical disclosure.
    historical = store.get_checkpoint("historical_snapshot")
    if isinstance(historical, dict) and historical:
        fields = ("provider", "dataset_url", "version", "published_at", "snapshot_label", "complete", "unverified", "live", "observation_date")
        manifest["historical_snapshot"] = {field: historical.get(field) for field in fields}
        manifest["historical_snapshot"]["publication_is_observation_date"] = False
        manifest["historical_snapshot"]["complete_means"] = "Snapshot files imported; not complete or current Metal-Archives coverage."
    albums = store.get_checkpoint("album_snapshot")
    if isinstance(albums, dict) and albums:
        fields = ("provider", "dataset", "dataset_url", "revision", "published_at", "snapshot_label", "observation_date", "license", "complete", "unverified", "live")
        manifest["album_snapshot"] = {field: albums.get(field) for field in fields}
        manifest["album_snapshot"]["publication_is_observation_date"] = False
        manifest["album_snapshot"]["complete_means"] = "Album source files processed; not complete or current Metal-Archives coverage."
    try:
        # A bounded lookup cache avoids reloading the same parents for nearby
        # tracks without retaining the complete catalog during export.
        lookup = lru_cache(maxsize=512)(store.get)
        for entity_type in ENTITY_TYPES:
            directory = staging / entity_type
            faceted = (with_search_facets(record, lookup) for record in store.iter_entities(entity_type))
            for index, records in enumerate(chunks(faceted, partition_size)):
                directory.mkdir(exist_ok=True)
                suffix = ".gz" if compressed else ""
                json_path = directory / f"part-{index:06d}.json{suffix}"
                csv_path = directory / f"part-{index:06d}.csv{suffix}"
                json_description = _payload(json_path, (canonical_json(records) + "\n").encode("utf-8"), compressed)
                csv_description = write_csv(csv_path, records, compressed=compressed)
                manifest["partitions"].append({
                    "path": json_path.relative_to(staging).as_posix(),
                    "csv_path": csv_path.relative_to(staging).as_posix(),
                    "entity_type": entity_type,
                    "count": len(records),
                    **json_description,
                    **{"csv_" + key: value for key, value in csv_description.items()},
                    "first_id": records[0]["entity_id"],
                    "last_id": records[-1]["entity_id"],
                })
                manifest["total_records"] += len(records)
                budget(json_path, csv_path)
        history = staging / "history"
        for index, changes in enumerate(chunks(store.iter_changes(), partition_size)):
            history.mkdir(exist_ok=True)
            csv_path = history / f"changes-{index:06d}.csv{'.gz' if compressed else ''}"
            csv_description = write_csv(csv_path, changes, compressed=compressed)
            manifest["change_partitions"].append({"path": csv_path.relative_to(staging).as_posix(), "count": len(changes), **csv_description})
            budget(csv_path)
        summary = store.summary()
        if "historical_snapshot" in manifest:
            summary["historical_snapshot"] = manifest["historical_snapshot"]
        if "album_snapshot" in manifest:
            summary["album_snapshot"] = manifest["album_snapshot"]
        recent_runs = store.runs(limit=104)
        recent_changes = list(store.iter_changes(limit=1000))
        reviews = []
        for row in store.conn.execute("SELECT * FROM review_queue WHERE review_status='unresolved' ORDER BY updated_at DESC LIMIT 1000"):
            value = dict(row)
            value["details"] = json.loads(value.pop("details_json"))
            reviews.append(value)
        manifest["changes_total"] = store.conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0]
        manifest["changes_preview_limit"] = 1000
        manifest["runs_preview_limit"] = 104
        manifest["reviews_preview_limit"] = 1000
        for name, value in (("summary.json", summary), ("runs.json", recent_runs), ("changes.json", recent_changes), ("reviews.json", reviews)):
            path = staging / name
            _json(path, value)
            budget(path)
        if include_sqlite:
            snapshot = staging / "catalog.sqlite"
            store.backup(snapshot)
            manifest["downloads"]["sqlite"] = "catalog.sqlite"
            budget(snapshot)
        manifest["data_bytes"] = bytes_written
        _json(staging / "manifest.json", manifest)
        budget(staging / "manifest.json")
        # Same-volume rename avoids publishing a half-written partition set.
        if target.exists():
            os.replace(target, backup)
        try:
            os.replace(staging, target)
        except BaseException:
            if backup.exists() and not target.exists():
                os.replace(backup, target)
            raise
        if backup.exists():
            _cleanup_generated(backup, target.parent, prefix)
        return manifest
    finally:
        if staging.exists():
            _cleanup_generated(staging, target.parent, prefix)
