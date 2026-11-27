"""Read-only, aggregate-only validation of a cloud-restored catalog.

Run after album import with ``python scripts/validate_album_import.py``. This
does not download source files, decode Parquet, or print any catalog row values.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3

from catalog_mirror.albums import DATASET, artist_key_shape, ma_url_identity, positive_id


ENTITY_TYPES = ("band", "release", "song", "label", "artist", "relationship", "upcoming", "review")
FIELDS = ("band_id", "band_name", "release_year", "release_date", "release_type",
          "label_id", "label_name", "genres_raw", "genres_json", "snapshot_artist_key")
SNAPSHOT_SOURCE = "Hugging Face / Metal-Archives"


def available(value):
    if isinstance(value, str):
        return bool(value.strip())
    return value is not None and value != [] and value != {}


def entity_counts(conn):
    counts = dict.fromkeys(ENTITY_TYPES, 0)
    for kind, count in conn.execute("SELECT entity_type,COUNT(*) FROM entities GROUP BY entity_type"):
        if kind in counts:
            counts[kind] = count
    return counts


def validate_database(path):
    """One release at a time; SQLite temporary aggregates spill to disk.

    The read transaction fixes a consistent snapshot, including when a WAL is
    present. Counts are checked again before ending that snapshot. No Store is
    constructed, so schema initialization/migration cannot mutate the database.
    """
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA temp_store=FILE")
        conn.execute("PRAGMA cache_size=-4096")
        conn.execute("BEGIN")
        before = entity_counts(conn)
        report = {
            "read_only": True, "entity_counts": before, "total_records": sum(before.values()),
            "release_count": before["release"], "song_count": before["song"],
            "snapshot_release_count": 0, "linked_release_count": 0,
            "missing_parent_release_count": 0, "unlinked_release_count": 0,
            "valid_release_id_count": 0, "valid_source_url_count": 0,
            "matching_id_url_count": 0, "canonical_source_url_count": 0,
            "invalid_data_object_count": 0,
            "metadata_available": {"title": 0, **dict.fromkeys(FIELDS, 0)},
            "snapshot_metadata_available": {"title": 0, **dict.fromkeys(FIELDS, 0)},
            "snapshot_artist_key_shapes": dict.fromkeys(("missing", "ma_band_url", "numeric", "other_url", "opaque"), 0),
        }
        cursor = conn.execute("""
            SELECT e.entity_id,e.name,e.source_url,e.source_system,e.data_json,
                   b.entity_id IS NOT NULL
            FROM entities e LEFT JOIN entities b
              ON b.entity_type='band' AND b.entity_id=json_extract(e.data_json,'$.band_id')
            WHERE e.entity_type='release'
        """)
        for entity_id, name, source_url, source_system, data_json, parent_exists in cursor:
            data = json.loads(data_json)
            if not isinstance(data, dict):
                report["invalid_data_object_count"] += 1
                data = {}
            identity = ma_url_identity(source_url, "albums")
            report["valid_release_id_count"] += int(bool(positive_id(entity_id)))
            report["valid_source_url_count"] += int(identity is not None)
            report["matching_id_url_count"] += int(identity is not None and identity[0] == entity_id)
            report["canonical_source_url_count"] += int(identity is not None and identity[1] == source_url)
            if available(data.get("band_id")):
                key = "linked_release_count" if parent_exists else "missing_parent_release_count"
            else:
                key = "unlinked_release_count"
            report[key] += 1
            snapshot = source_system == SNAPSHOT_SOURCE or data.get("snapshot_dataset") == DATASET
            targets = [report["metadata_available"]]
            if snapshot:
                report["snapshot_release_count"] += 1
                targets.append(report["snapshot_metadata_available"])
                report["snapshot_artist_key_shapes"][artist_key_shape(data.get("snapshot_artist_key"))] += 1
            for target in targets:
                target["title"] += int(available(name))
                for field in FIELDS:
                    target[field] += int(available(data.get(field)))
        report["unique_release_id_count"], report["unique_source_url_count"] = conn.execute(
            "SELECT COUNT(DISTINCT entity_id),COUNT(DISTINCT NULLIF(source_url,'')) FROM entities WHERE entity_type='release'"
        ).fetchone()
        report["linked_band_count"] = conn.execute("""
            SELECT COUNT(DISTINCT b.entity_id) FROM entities e JOIN entities b
              ON b.entity_type='band' AND b.entity_id=json_extract(e.data_json,'$.band_id')
            WHERE e.entity_type='release'
        """).fetchone()[0]
        report["snapshot_song_count"] = conn.execute("""
            SELECT COUNT(*) FROM entities e WHERE e.entity_type='song' AND (
              e.source_system=? OR json_extract(e.data_json,'$.snapshot_dataset')=?
              OR EXISTS (SELECT 1 FROM source_observations o
                WHERE o.entity_type='song' AND o.entity_id=e.entity_id AND o.source LIKE ?)
              OR EXISTS (SELECT 1 FROM sync_runs r WHERE r.run_id=e.last_changed_run
                AND (r.mode LIKE 'album_snapshot_%' OR json_extract(r.metrics_json,'$.import_kind')='album_snapshot')))
        """, (SNAPSHOT_SOURCE, DATASET, f"snapshot:huggingface:{DATASET}:%")).fetchone()[0]
        report["counts_stable"] = before == entity_counts(conn)
        errors = []
        for key in ("valid_release_id_count", "valid_source_url_count", "matching_id_url_count", "unique_release_id_count", "unique_source_url_count"):
            if report[key] != report["release_count"]:
                errors.append(key + "_mismatch")
        if report["snapshot_song_count"]:
            errors.append("album_snapshot_contains_songs")
        if report["invalid_data_object_count"]:
            errors.append("invalid_release_data_object")
        if not report["counts_stable"]:
            errors.append("entity_counts_changed")
        report["errors"] = errors
        report["valid"] = not errors
        return report
    finally:
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="state/catalog.sqlite")
    args = parser.parse_args(argv)
    try:
        report = validate_database(args.db)
    except (OSError, sqlite3.Error, ValueError, TypeError):
        # Paths, SQL errors and row values are deliberately excluded from logs.
        report = {"valid": False, "read_only": True, "errors": ["database_validation_unavailable"]}
    print(json.dumps(report, sort_keys=True))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
