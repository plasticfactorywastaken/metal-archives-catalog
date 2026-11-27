"""Bounded historical Kaggle bootstrap. No live-site requests or model parsing.

The provider publication date is known; the precise observation date of each
Metal-Archives record is not. Imported entities remain unverified. Existing IDs
are never overwritten by a historical snapshot, even if currently unverified.
"""
from __future__ import annotations

import csv
import hashlib
import http.client
import json
import logging
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlsplit, urlunsplit
import urllib.error
import urllib.request
import zipfile

from .genres import genre_fields
from .store import canonical_json, content_hash, utcnow

log = logging.getLogger(__name__)
DATASET = "guimacrlh/every-metal-archives-band-october-2024"
DATASET_URL = "https://www.kaggle.com/datasets/" + DATASET
VERSION = 3
PUBLISHED_AT = "2024-11-09T13:34:11.417Z"
LABEL = "November 2024"
FILES = {
    "bands": {"filename": "metal_bands_roster.csv", "bytes": 32956677, "headers": ("Band ID", "Name", "URL", "Country", "Genre", "Status", "Photo_URL", "Label ID")},
    "labels": {"filename": "labels_roster.csv", "bytes": 17424004, "headers": ("Label ID", "Band ID", "Name", "Specialization", "Status", "Country", "Website", "Online Shopping")},
}
MAX_DOWNLOAD_BYTES = 128 * 1024 * 1024
ALLOWED_HOSTS = frozenset({"www.kaggle.com", "kaggle.com", "storage.googleapis.com"})


class SnapshotError(RuntimeError):
    pass


def download_url(filename):
    if filename not in {value["filename"] for value in FILES.values()}:
        raise ValueError("Only the two pinned public metadata CSV files are supported")
    return f"https://www.kaggle.com/api/v1/datasets/download/{DATASET}/{filename}?datasetVersionNumber={VERSION}"


def safe_download_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise SnapshotError("Snapshot redirect left the permitted HTTPS provider/storage hosts")
    return url


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        safe_download_url(newurl)
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def sha256(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


class SnapshotDownloader:
    """One sequential, bounded GET per pinned file; no authentication or retries."""
    def __init__(self, opener=None, max_bytes=MAX_DOWNLOAD_BYTES, clock=time.monotonic):
        self.opener = opener or urllib.request.build_opener(SafeRedirect())
        self.max_bytes = max_bytes
        self.clock = clock

    def download(self, filename, destination, deadline):
        url = safe_download_url(download_url(filename))
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise SnapshotError("Snapshot time budget expired before download")
        request = urllib.request.Request(url, headers={"User-Agent": "CatalogMirror/0.1 (historical public metadata snapshot)", "Accept": "text/csv,application/zip,application/octet-stream"})
        destination = Path(destination)
        try:
            with self.opener.open(request, timeout=min(30, remaining)) as response:
                safe_download_url(response.geturl())
                if response.status != 200:
                    raise SnapshotError(f"Snapshot download returned HTTP {response.status}")
                length = response.headers.get("Content-Length")
                if length and int(length) > self.max_bytes:
                    raise SnapshotError("Snapshot response exceeds download byte limit")
                if "text/html" in response.headers.get("Content-Type", "").lower():
                    raise SnapshotError("Snapshot returned HTML, not a metadata file")
                size = 0
                with destination.open("wb") as stream:
                    while True:
                        if self.clock() >= deadline:
                            raise SnapshotError("Snapshot time budget expired during download")
                        block = response.read(min(1024 * 1024, self.max_bytes - size + 1))
                        if not block:
                            break
                        size += len(block)
                        if size > self.max_bytes:
                            raise SnapshotError("Snapshot response exceeds download byte limit")
                        stream.write(block)
                if not size:
                    raise SnapshotError("Snapshot file is empty")
                if length and size != int(length):
                    raise SnapshotError("Snapshot download ended before its declared byte length")
        except urllib.error.HTTPError as exc:
            destination.unlink(missing_ok=True)
            raise SnapshotError(f"Snapshot download returned HTTP {exc.code}; no retry or bypass attempted") from None
        except (urllib.error.URLError, http.client.IncompleteRead, TimeoutError, OSError) as exc:
            destination.unlink(missing_ok=True)
            raise SnapshotError(f"Snapshot download failed: {type(exc).__name__}") from None
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return destination


def prepare_csv(path, expected_filename, max_bytes=MAX_DOWNLOAD_BYTES):
    """Accept plain CSV or a ZIP containing exactly the requested CSV; no extractall."""
    path = Path(path)
    if path.stat().st_size > max_bytes:
        raise SnapshotError("Snapshot input exceeds byte limit")
    with path.open("rb") as stream:
        prefix = stream.read(4)
    if prefix.startswith(b"PK"):
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) != 1 or entries[0].filename != expected_filename or entries[0].is_dir():
                raise SnapshotError("ZIP must contain exactly the requested CSV and no other files")
            entry = entries[0]
            if entry.flag_bits & 1 or entry.file_size > max_bytes:
                raise SnapshotError("Encrypted or oversized snapshot ZIP rejected")
            output = path.with_suffix(".extracted.csv")
            try:
                size = 0
                with archive.open(entry) as source, output.open("wb") as destination:
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        size += len(block)
                        if size > max_bytes:
                            raise SnapshotError("Expanded snapshot exceeds byte limit")
                        destination.write(block)
                if size != entry.file_size:
                    raise SnapshotError("Snapshot ZIP size mismatch")
            except Exception:
                output.unlink(missing_ok=True)
                raise
            return output
    return path


def stable_id(value):
    value = str(value or "").strip()
    if not re.fullmatch(r"[1-9][0-9]*(?:\.0+)?", value):
        return None
    return value.split(".", 1)[0]


def normalize_row(kind, row, checksum):
    key = "Band ID" if kind == "bands" else "Label ID"
    entity_id = stable_id(row.get(key))
    if not entity_id:
        raise ValueError("missing_or_invalid_stable_id")
    if not isinstance(row.get("Name"), str) or not row["Name"].strip():
        raise ValueError("missing_name")
    record = {
        "entity_type": "band" if kind == "bands" else "label",
        "entity_id": entity_id,
        "name": row["Name"],
        "country": row.get("Country") or "",
        "status": row.get("Status") or "",
        "source_system": "Kaggle / Metal-Archives",
        "source_updated_at": None,
        "raw_source_ref": download_url(FILES[kind]["filename"]) + "#sha256=" + checksum,
        "snapshot_dataset": DATASET,
        "snapshot_dataset_url": DATASET_URL,
        "snapshot_version": VERSION,
        "snapshot_published_at": PUBLISHED_AT,
        "snapshot_label": LABEL,
        "snapshot_observation_date": None,
        "snapshot_checksum": checksum,
    }
    if kind == "bands":
        parsed = urlsplit(row.get("URL") or "")
        parts = [part for part in parsed.path.split("/") if part]
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"www.metal-archives.com", "metal-archives.com"} or parsed.username or parsed.password or parsed.port not in (None, 80, 443) or len(parts) < 2 or parts[0] != "bands" or parts[-1] != entity_id:
            raise ValueError("missing_or_mismatched_band_url")
        record["source_url"] = urlunsplit(("https", "www.metal-archives.com", parsed.path, "", ""))
        record["genres_raw"] = row.get("Genre") if isinstance(row.get("Genre"), str) else ""
        label_id = stable_id(row.get("Label ID"))
        if label_id:
            record["label_id"] = label_id
    else:
        record["source_url"] = "https://www.metal-archives.com/labels/" + entity_id
        record["specialization"] = row.get("Specialization") or ""
        record["official_url"] = row.get("Website") or ""
    # Explicit allowlisting deliberately discards Photo_URL and joined Band ID
    # from labels. No images, releases, lyrics, or forum records are imported.
    return record


def _checkpoint_sql(store, key, value, timestamp):
    store.conn.execute("INSERT INTO checkpoints VALUES(?,?,?) ON CONFLICT(checkpoint_key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at", (key, canonical_json(value), timestamp))


def _insert_snapshot(store, record, run_id, timestamp):
    """One new entity inside the caller's batch transaction, never an overwrite."""
    kind, entity_id = record["entity_type"], record["entity_id"]
    if store.conn.execute("SELECT 1 FROM entities WHERE entity_type=? AND entity_id=?", (kind, entity_id)).fetchone():
        return False
    data = {key: value for key, value in record.items() if key not in {"entity_type", "entity_id", "source_system", "source_updated_at", "raw_source_ref"} and value is not None}
    if "genres_raw" in data:
        data.update(genre_fields(data["genres_raw"]))
    digest = content_hash(data)
    store.conn.execute(
        "INSERT INTO entities(entity_type,entity_id,name,source_url,source_system,data_json,first_seen,last_seen,last_checked,source_updated_at,content_hash,active_status,verification_status,raw_source_ref,missing_count,last_missing_run,last_changed_at,last_changed_run) VALUES(?,?,?,?,?,?,?,?,NULL,NULL,?,'active','pending',?,0,NULL,?,?)",
        (kind, entity_id, data["name"], data["source_url"], record["source_system"], canonical_json(data), timestamp, timestamp, digest, record["raw_source_ref"], timestamp, run_id))
    store.conn.execute(
        "INSERT INTO source_observations(entity_type,entity_id,source,data_json,content_hash,checked_at,run_id,verified) VALUES(?,?,?,?,?,?,?,0)",
        (kind, entity_id, "snapshot:kaggle:v3", canonical_json(data), digest, timestamp, run_id))
    store._change(run_id, kind, entity_id, data["name"], "new", "*", None, data, data["source_url"], timestamp)
    return True


class SnapshotImporter:
    def __init__(self, store, downloader=None, batch_size=500, clock=time.monotonic):
        if not 1 <= batch_size <= 5000:
            raise ValueError("Snapshot batch size must be between 1 and 5000")
        self.store = store
        self.downloader = downloader or SnapshotDownloader(clock=clock)
        self.batch_size = batch_size
        self.clock = clock

    @staticmethod
    def checkpoint_key(kind):
        return f"snapshot:kaggle:{DATASET}:v{VERSION}:{kind}"

    def run(self, mode="full", limit=None, seconds=7200, files=None):
        """Import at most `limit` CSV rows. `files` exists for synthetic/offline tests.

        Downloads are limited to two pinned public files, at most 128 MiB each.
        Checkpoints and new entities commit together every 500 rows. On resume,
        the immutable CSV checksum must match before its cursor is honored.
        """
        if mode not in {"test", "batch", "full"}:
            raise ValueError("Invalid snapshot mode")
        limit = limit if limit is not None else {"test": 5, "batch": 2500, "full": 250000}[mode]
        if not isinstance(limit, int) or limit < 1 or seconds <= 0:
            raise ValueError("Positive snapshot row/time budgets are required")
        deadline = self.clock() + seconds
        run_id = self.store.start_run("snapshot_" + mode)
        provenance = {
            "provider": "Kaggle", "dataset": DATASET, "dataset_url": DATASET_URL,
            "version": VERSION, "published_at": PUBLISHED_AT, "snapshot_label": LABEL,
            "observation_date": None, "complete": False, "unverified": True, "live": False,
        }
        self.store.set_checkpoint("historical_snapshot", provenance)
        self.store.add_review("dataset", DATASET + ":v3", "historical_snapshot_unverified", run_id,
                              details={"provider": "Kaggle", "version": VERSION, "published_at": PUBLISHED_AT,
                                       "observation_date": None, "verification": "Pending direct Metal-Archives verification"},
                              source_url=DATASET_URL)
        processed = inserted = existing = invalid = 0
        errors = []
        csv.field_size_limit(1024 * 1024)
        with tempfile.TemporaryDirectory(prefix="catalog-snapshot-") as temporary:
            for kind, specification in FILES.items():
                key = self.checkpoint_key(kind)
                checkpoint = self.store.get_checkpoint(key, {"next_row": 0, "rows_inserted": 0, "rows_existing": 0, "rows_invalid": 0, "done": False})
                if checkpoint.get("done"):
                    continue
                if processed >= limit or self.clock() >= deadline:
                    break
                try:
                    path = Path(files[kind]) if files is not None else self.downloader.download(specification["filename"], Path(temporary) / (kind + ".download"), deadline)
                    path = prepare_csv(path, specification["filename"])
                    if files is None and path.stat().st_size != specification["bytes"]:
                        raise SnapshotError("Pinned provider CSV byte count changed or download was incomplete")
                    checksum = sha256(path)
                    if checkpoint.get("checksum") and checkpoint["checksum"] != checksum:
                        raise SnapshotError("Pinned snapshot checksum changed; existing cursor retained")
                    checkpoint["checksum"] = checksum
                    checkpoint["filename"] = specification["filename"]
                    with path.open("r", encoding="utf-8-sig", newline="") as stream:
                        reader = csv.DictReader(stream)
                        if tuple(reader.fieldnames or ()) != specification["headers"]:
                            raise SnapshotError("Snapshot CSV headers changed; import refused")
                        for _ in range(checkpoint["next_row"]):
                            if next(reader, None) is None:
                                raise SnapshotError("Snapshot cursor lies beyond the file")
                        done = False
                        while processed < limit and self.clock() < deadline and not done:
                            batch = []
                            for _ in range(min(self.batch_size, limit - processed)):
                                if self.clock() >= deadline:
                                    break
                                row = next(reader, None)
                                if row is None:
                                    done = True
                                    break
                                batch.append(row)
                            timestamp = utcnow()
                            with self.store.conn:
                                for row in batch:
                                    processed += 1
                                    checkpoint["next_row"] += 1
                                    try:
                                        record = normalize_row(kind, row, checksum)
                                    except ValueError as exc:
                                        invalid += 1
                                        checkpoint["rows_invalid"] += 1
                                        if len(errors) < 20:
                                            errors.append(f"{kind} CSV row {checkpoint['next_row']}: {exc}")
                                        continue
                                    if _insert_snapshot(self.store, record, run_id, timestamp):
                                        inserted += 1
                                        checkpoint["rows_inserted"] += 1
                                        self.store._increment(run_id, "records_inserted")
                                    else:
                                        existing += 1
                                        checkpoint["rows_existing"] += 1
                                checkpoint["done"] = done
                                checkpoint["rows_seen"] = checkpoint["next_row"]
                                _checkpoint_sql(self.store, key, checkpoint, timestamp)
                            log.info(json.dumps({"event": "snapshot_progress", "run_id": run_id, "file": kind, "rows_processed": processed, "records_inserted": inserted, "next_row": checkpoint["next_row"], "file_complete": done}))
                except (SnapshotError, OSError, UnicodeError, csv.Error, zipfile.BadZipFile, ValueError) as exc:
                    errors.append(f"{kind}: {type(exc).__name__}: {exc}")
                    break
        checkpoints = {kind: self.store.get_checkpoint(self.checkpoint_key(kind), {}) for kind in FILES}
        complete = all(value.get("done", False) for value in checkpoints.values())
        provenance.update({"complete": complete, "files": checkpoints})
        self.store.set_checkpoint("historical_snapshot", provenance)
        verification_ratio = self.store.summary().get("verification_ratio")
        metrics = {
            "status": "partial", "api_status": "not_checked", "website_status": "not_checked",
            "snapshot_status": "complete" if complete else "partial",
            "import_kind": "historical_snapshot", "snapshot_provider": "Kaggle", "snapshot_version": VERSION,
            "snapshot_published_at": PUBLISHED_AT, "snapshot_label": LABEL,
            "records_discovered": inserted, "records_checked": 0, "records_verified_directly": 0,
            "records_inserted": inserted, "records_unchanged": existing, "records_skipped": invalid,
            "snapshot_rows_processed": processed, "records_remaining": 0 if complete else None,
            "estimated_coverage": None, "estimated_confidence": verification_ratio,
            "error_summary": errors, "checkpoint": checkpoints,
            "source_note": "Historical provider files only; precise source observation dates and live completeness are unknown. All imported records require direct verification.",
        }
        self.store.finish_run(run_id, **metrics)
        return {"run_id": run_id, "processed": processed, **metrics}
