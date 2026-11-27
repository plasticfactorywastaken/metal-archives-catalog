"""Pinned, resumable third-party album bootstrap; never a live MA observation.

Only five album/artist columns are decoded from the public Parquet files. Track
titles, track IDs, lyrics and other source columns are never read or imported.
The cloud workflow supplies temporary disk; this module never requests MA URLs.
"""
from __future__ import annotations

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

from .store import canonical_json, content_hash, utcnow

log = logging.getLogger(__name__)
DATASET = "fairygaze/music"
DATASET_URL = "https://huggingface.co/datasets/" + DATASET
REVISION = "06856d4d785dce8b029551bcca4839ee1e4d0ccc"
PUBLISHED_AT = "2026-08-24T10:35:27.000Z"
SOURCE_ROWS = 4510074
COLUMNS = ("release_id", "release_url", "album", "artist_key", "artist_name")
FILES = {
    "part-000000000001.parquet": {"bytes": 21757791, "sha256": "18d4e1c7408e4436a149b4e64e45530463ba7f5d1de2fcc03ed1665c1cc69928"},
    "part-000000000002.parquet": {"bytes": 21583269, "sha256": "8744aa853d0ec851be9440016c227339235676d393d371db9853aa86c1fdb10c"},
    "part-000000000003.parquet": {"bytes": 21698891, "sha256": "91735639083f1e35b7458d3d485d9d8b20e97f6d480bbf65c5fbd3cd0b184949"},
    "part-000000000004.parquet": {"bytes": 21700961, "sha256": "1ade5afb3a18d61d22edb2a54c61ac5c60d3551db660e3e0f188da178e415d95"},
    "part-000000000005.parquet": {"bytes": 21678181, "sha256": "424795e38f3ea10f4222dd7a672a34984cf9c9a3d37a3ceea7d3b58854e7d725"},
    "part-000000000006.parquet": {"bytes": 21391849, "sha256": "55afc07b657dad4303a001869592fbc30852b74f8ec801adf0834729adf1067f"},
    "part-000000000007.parquet": {"bytes": 21719810, "sha256": "602e0a98d8590f043a74fbe09f7a8bd33bab8e05fc51739cded9d1d31dffc073"},
    "part-000000000008.parquet": {"bytes": 21722073, "sha256": "a3cae67826229d983750fa78eb60c88f1b58dff862e5b066d764cf52f5b7d690"},
    "part-000000000009.parquet": {"bytes": 22069510, "sha256": "f6d5d76f3f1d5dd11c9871fa9e55229bb8f00194513af301702dff34410163e2"},
    "part-000000000010.parquet": {"bytes": 456444, "sha256": "5296e48cac3e6c47f1ec4b0bab337c5f8740e3583887480f681b925606df5d9e"},
}
ALLOWED_HOSTS = frozenset({"huggingface.co", "us.aws.cdn.hf.co"})
MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024
MAX_ERRORS = 20


class AlbumSnapshotError(RuntimeError):
    pass


def download_url(filename):
    if filename not in FILES:
        raise ValueError("Only the ten pinned Metal-Archives Parquet parts are supported")
    return f"{DATASET_URL}/resolve/{REVISION}/metal-archives/{filename}"


def safe_download_url(url):
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == "https" and parsed.hostname in ALLOWED_HOSTS
                 and not parsed.username and not parsed.password
                 and parsed.port in (None, 443) and not parsed.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise AlbumSnapshotError("Album download left the permitted HTTPS provider/CDN hosts")
    return url


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        safe_download_url(newurl)
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def verify_file(path, specification):
    """Verify immutable bytes before any row is decoded or a cursor is trusted."""
    path = Path(path)
    if path.stat().st_size != specification["bytes"] or path.stat().st_size > MAX_DOWNLOAD_BYTES:
        raise AlbumSnapshotError("Pinned album file byte count changed or download was incomplete")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        if stream.read(4) != b"PAR1":
            raise AlbumSnapshotError("Album file is not Parquet")
        stream.seek(0)
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
        stream.seek(-4, 2)
        if stream.read(4) != b"PAR1":
            raise AlbumSnapshotError("Album Parquet footer is missing")
    if digest.hexdigest() != specification["sha256"]:
        raise AlbumSnapshotError("Pinned album checksum changed; existing cursor retained")
    return digest.hexdigest()


class AlbumDownloader:
    """A sequential GET per pinned part, with no authentication or bypass retries."""
    def __init__(self, opener=None, clock=time.monotonic):
        self.opener = opener or urllib.request.build_opener(SafeRedirect())
        self.clock = clock

    def download(self, filename, destination, deadline):
        url = safe_download_url(download_url(filename))
        specification = FILES[filename]
        destination = Path(destination)
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise AlbumSnapshotError("Album time budget expired before download")
        request = urllib.request.Request(url, headers={
            "User-Agent": "CatalogMirror/0.1 (public historical album metadata)",
            "Accept": "application/octet-stream", "Accept-Encoding": "identity",
        })
        try:
            with self.opener.open(request, timeout=min(30, remaining)) as response:
                safe_download_url(response.geturl())
                if response.status != 200:
                    raise AlbumSnapshotError(f"Album download returned HTTP {response.status}")
                if "text/html" in response.headers.get("Content-Type", "").lower():
                    raise AlbumSnapshotError("Album download returned HTML")
                if response.headers.get("Content-Encoding", "identity").lower() not in ("", "identity"):
                    raise AlbumSnapshotError("Unexpected album download content encoding")
                length = response.headers.get("Content-Length")
                if length is not None and int(length) != specification["bytes"]:
                    raise AlbumSnapshotError("Album response byte count differs from pinned metadata")
                size = 0
                with destination.open("wb") as stream:
                    while True:
                        if self.clock() >= deadline:
                            raise AlbumSnapshotError("Album time budget expired during download")
                        block = response.read(min(1024 * 1024, specification["bytes"] - size + 1))
                        if not block:
                            break
                        size += len(block)
                        if size > specification["bytes"] or size > MAX_DOWNLOAD_BYTES:
                            raise AlbumSnapshotError("Album download exceeds its pinned byte limit")
                        stream.write(block)
            verify_file(destination, specification)
        except urllib.error.HTTPError as exc:
            destination.unlink(missing_ok=True)
            raise AlbumSnapshotError(f"Album download returned HTTP {exc.code}; no retry or bypass attempted") from None
        except (urllib.error.URLError, http.client.IncompleteRead, TimeoutError, OSError) as exc:
            destination.unlink(missing_ok=True)
            raise AlbumSnapshotError(f"Album download failed: {type(exc).__name__}") from None
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return destination


def positive_id(value):
    return value if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,29}", value) else None


def ma_url_identity(value, kind):
    """A literal MA entity URL is evidence of its ID; arbitrary keys are not."""
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        parts = parsed.path.strip("/").split("/")
        lengths = {2, 4} if kind == "albums" else {2, 3}
        valid = (parsed.scheme in {"https", "http"}
                 and parsed.hostname in {"www.metal-archives.com", "metal-archives.com"}
                 and not parsed.username and not parsed.password
                 and parsed.port in (None, 443 if parsed.scheme == "https" else 80)
                 and len(parts) in lengths and parts[0] == kind
                 and all(part and part not in {".", ".."} for part in parts)
                 and positive_id(parts[-1]))
    except ValueError:
        return None
    if not valid:
        return None
    return parts[-1], urlunsplit(("https", "www.metal-archives.com", "/" + "/".join(parts), "", ""))


def artist_key_shape(value):
    if not isinstance(value, str) or not value:
        return "missing"
    if ma_url_identity(value, "bands"):
        return "ma_band_url"
    if positive_id(value):
        return "numeric"
    if value.startswith(("http://", "https://")):
        return "other_url"
    return "opaque"


def normalize_row(row, filename, store=None):
    identity = ma_url_identity(row.get("release_url"), "albums")
    if not identity:
        raise ValueError("missing_or_invalid_ma_release_url")
    entity_id, source_url = identity
    supplied_id = row.get("release_id")
    if positive_id(supplied_id) and supplied_id != entity_id:
        raise ValueError("release_id_url_mismatch")
    # Opaque upstream identifiers are never claimed as MA IDs. The validated
    # /albums/.../<id> URL remains the authoritative numeric identifier.
    name = row.get("album")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("missing_album_name")
    artist_name = row.get("artist_name")
    artist_key = row.get("artist_key")
    artist_name = artist_name if isinstance(artist_name, str) else ""
    artist_key = artist_key if isinstance(artist_key, str) else ""
    record = {
        "entity_type": "release", "entity_id": entity_id, "name": name,
        "source_url": source_url, "source_system": "Hugging Face / Metal-Archives",
        "source_updated_at": None,
        "raw_source_ref": download_url(filename) + "#sha256=" + FILES[filename]["sha256"],
        "band_name": artist_name, "snapshot_artist_key": artist_key,
        "snapshot_dataset": DATASET, "snapshot_revision": REVISION,
        "snapshot_published_at": PUBLISHED_AT, "snapshot_observation_date": None,
        "snapshot_license": "unknown",
    }
    band_identity = ma_url_identity(artist_key, "bands")
    link_kind = "unlinked"
    if band_identity:
        record["band_id"] = band_identity[0]
        link_kind = "ma_band_url"
    elif positive_id(artist_key) and artist_name and store is not None:
        band = store.conn.execute("SELECT name FROM entities WHERE entity_type='band' AND entity_id=?", (artist_key,)).fetchone()
        if band is not None and band[0] == artist_name:
            record["band_id"] = artist_key
            link_kind = "numeric_existing_name_match"
    return record, link_kind


class ProjectedParquet:
    """Lazy Arrow dependency, fixed projection, and bounded decoded batches."""
    def __init__(self, path):
        try:
            import pyarrow.parquet as parquet
        except ImportError:
            raise AlbumSnapshotError("Album import requires the optional albums dependency (pyarrow)") from None
        try:
            self.file = parquet.ParquetFile(path, memory_map=False, pre_buffer=False)
            if not set(COLUMNS).issubset(self.file.schema_arrow.names):
                self.file.close()
                raise AlbumSnapshotError("Album Parquet schema is missing required projected columns")
            # A changed schema must not silently turn numeric keys into floats.
            if any(str(self.file.schema_arrow.field(column).type) not in {"string", "large_string"} for column in COLUMNS):
                self.file.close()
                raise AlbumSnapshotError("Album projected columns must all be strings")
            self.num_rows = self.file.metadata.num_rows
        except AlbumSnapshotError:
            raise
        except Exception as exc:
            raise AlbumSnapshotError(f"Album Parquet metadata cannot be read: {type(exc).__name__}") from None

    def batches(self, start_row, batch_size):
        if not 0 <= start_row <= self.num_rows:
            raise AlbumSnapshotError("Album cursor lies beyond the file")
        offset = 0
        try:
            for group in range(self.file.num_row_groups):
                rows = self.file.metadata.row_group(group).num_rows
                if offset + rows <= start_row:
                    offset += rows
                    continue
                skip = max(0, start_row - offset)
                for batch in self.file.iter_batches(batch_size=batch_size, row_groups=[group], columns=list(COLUMNS), use_threads=False):
                    if skip >= batch.num_rows:
                        skip -= batch.num_rows
                        continue
                    if skip:
                        batch = batch.slice(skip)
                        skip = 0
                    yield batch.to_pylist()
                offset += rows
        except AlbumSnapshotError:
            raise
        except Exception as exc:
            raise AlbumSnapshotError(f"Album projected batch cannot be read: {type(exc).__name__}") from None

    def close(self):
        self.file.close()


def _checkpoint_sql(store, key, value, timestamp):
    store.conn.execute("INSERT INTO checkpoints VALUES(?,?,?) ON CONFLICT(checkpoint_key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at", (key, canonical_json(value), timestamp))


def _insert_album(store, record, run_id, timestamp):
    entity_id = record["entity_id"]
    if store.conn.execute("SELECT 1 FROM entities WHERE entity_type='release' AND entity_id=?", (entity_id,)).fetchone():
        return False
    data = {key: value for key, value in record.items() if key not in {"entity_type", "entity_id", "source_system", "source_updated_at", "raw_source_ref"} and value is not None}
    digest = content_hash(data)
    store.conn.execute(
        "INSERT INTO entities(entity_type,entity_id,name,source_url,source_system,data_json,first_seen,last_seen,last_checked,source_updated_at,content_hash,active_status,verification_status,raw_source_ref,missing_count,last_missing_run,last_changed_at,last_changed_run) VALUES('release',?,?,?,?,?,?,?,NULL,NULL,?,'active','pending',?,0,NULL,?,?)",
        (entity_id, data["name"], data["source_url"], record["source_system"], canonical_json(data), timestamp, timestamp, digest, record["raw_source_ref"], timestamp, run_id))
    store.conn.execute(
        "INSERT INTO source_observations(entity_type,entity_id,source,data_json,content_hash,checked_at,run_id,verified) VALUES('release',?,?,?,?,?,?,0)",
        (entity_id, f"snapshot:huggingface:{DATASET}:{REVISION}", canonical_json(data), digest, timestamp, run_id))
    store._change(run_id, "release", entity_id, data["name"], "new", "*", None, data, data["source_url"], timestamp)
    return True


class AlbumSnapshotImporter:
    def __init__(self, store, downloader=None, batch_size=500, clock=time.monotonic):
        if not isinstance(batch_size, int) or not 1 <= batch_size <= 500:
            raise ValueError("Album batch size must be between 1 and 500")
        self.store = store
        self.downloader = downloader or AlbumDownloader(clock=clock)
        self.batch_size = batch_size
        self.clock = clock

    @staticmethod
    def checkpoint_key(filename):
        return f"album_snapshot:huggingface:{DATASET}:{REVISION}:{filename}"

    def run(self, mode="full", limit=None, seconds=7200, files=None):
        """Process at most `limit` source rows, deduplicating by real MA release ID.

        `files` is a test-only mapping of pinned filenames to synthetic Parquet
        paths; integrity validation still applies. The cloud workflow calls this
        without files. Checkpoints commit atomically with at most 500 source rows.
        A completed part is never downloaded again; an unfinished immutable part
        resumes at its committed row and skips completed Arrow row groups.
        """
        if mode not in {"test", "batch", "full"}:
            raise ValueError("Invalid album snapshot mode")
        limit = limit if limit is not None else {"test": 25, "batch": 25000, "full": SOURCE_ROWS}[mode]
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or seconds <= 0:
            raise ValueError("Positive album row/time budgets are required")
        deadline = self.clock() + seconds
        run_id = self.store.start_run("album_snapshot_" + mode)
        provenance = {
            "provider": "Hugging Face", "dataset": DATASET, "dataset_url": DATASET_URL,
            "revision": REVISION, "published_at": PUBLISHED_AT, "observation_date": None,
            "license": "unknown", "complete": False, "unverified": True, "live": False,
            "publication_is_observation_date": False,
            "complete_means": "Source files processed; not complete or current Metal-Archives coverage.",
        }
        self.store.set_checkpoint("album_snapshot", provenance)
        self.store.add_review("dataset", DATASET + ":" + REVISION, "album_snapshot_unverified", run_id,
                              details={"provider": "Hugging Face", "revision": REVISION,
                                       "published_at": PUBLISHED_AT, "observation_date": None, "license": "unknown"},
                              source_url=DATASET_URL)
        processed = inserted = existing = invalid = linked_inserted = 0
        errors = []
        shapes, links = {}, {}
        with tempfile.TemporaryDirectory(prefix="catalog-albums-") as temporary:
            for filename, specification in FILES.items():
                key = self.checkpoint_key(filename)
                checkpoint = self.store.get_checkpoint(key, {"next_row": 0, "rows_inserted": 0, "rows_existing": 0, "rows_invalid": 0, "done": False})
                if checkpoint.get("checksum") not in (None, specification["sha256"]):
                    errors.append("Album checkpoint checksum differs from the pinned revision")
                    break
                if checkpoint.get("done"):
                    continue
                if processed >= limit or self.clock() >= deadline:
                    break
                source = None
                downloaded = files is None
                path = Path(temporary) / filename
                try:
                    path = Path(files[filename]) if files is not None else self.downloader.download(filename, path, deadline)
                    checksum = verify_file(path, specification)
                    source = ProjectedParquet(path)
                    cursor = checkpoint.get("next_row", 0)
                    if not isinstance(cursor, int) or isinstance(cursor, bool) or not 0 <= cursor <= source.num_rows:
                        raise AlbumSnapshotError("Album cursor lies beyond the file")
                    checkpoint.update({"checksum": checksum, "filename": filename, "total_rows": source.num_rows})
                    checkpoint.setdefault("artist_key_shapes", {})
                    checkpoint.setdefault("band_links", {})
                    checkpoint.setdefault("albums_linked_inserted", 0)
                    for rows in source.batches(cursor, self.batch_size):
                        if processed >= limit or self.clock() >= deadline:
                            break
                        rows = rows[:min(self.batch_size, limit - processed)]
                        if not rows:
                            continue
                        # Work on copied counters: a rolled-back transaction
                        # must never advance the durable resume cursor.
                        next_checkpoint = json.loads(canonical_json(checkpoint))
                        delta_inserted = delta_existing = delta_invalid = delta_linked = 0
                        batch_shapes, batch_links, batch_errors = {}, {}, []
                        timestamp = utcnow()
                        with self.store.conn:
                            for row in rows:
                                next_checkpoint["next_row"] += 1
                                shape = artist_key_shape(row.get("artist_key"))
                                batch_shapes[shape] = batch_shapes.get(shape, 0) + 1
                                try:
                                    record, link_kind = normalize_row(row, filename, self.store)
                                except ValueError as exc:
                                    delta_invalid += 1
                                    if len(errors) + len(batch_errors) < MAX_ERRORS:
                                        batch_errors.append(f"{filename} row {next_checkpoint['next_row']}: {exc}")
                                    continue
                                batch_links[link_kind] = batch_links.get(link_kind, 0) + 1
                                if _insert_album(self.store, record, run_id, timestamp):
                                    delta_inserted += 1
                                    delta_linked += int("band_id" in record)
                                else:
                                    delta_existing += 1
                            next_checkpoint["rows_inserted"] += delta_inserted
                            next_checkpoint["rows_existing"] += delta_existing
                            next_checkpoint["rows_invalid"] += delta_invalid
                            next_checkpoint["albums_linked_inserted"] += delta_linked
                            next_checkpoint["rows_seen"] = next_checkpoint["next_row"]
                            next_checkpoint["done"] = next_checkpoint["next_row"] == source.num_rows
                            for category, values in (("artist_key_shapes", batch_shapes), ("band_links", batch_links)):
                                for name, count in values.items():
                                    next_checkpoint[category][name] = next_checkpoint[category].get(name, 0) + count
                            _checkpoint_sql(self.store, key, next_checkpoint, timestamp)
                            self.store._increment(run_id, "records_inserted", delta_inserted)
                        checkpoint = next_checkpoint
                        processed += len(rows)
                        inserted += delta_inserted
                        existing += delta_existing
                        invalid += delta_invalid
                        linked_inserted += delta_linked
                        errors.extend(batch_errors)
                        for target, values in ((shapes, batch_shapes), (links, batch_links)):
                            for name, count in values.items():
                                target[name] = target.get(name, 0) + count
                        log.info(json.dumps({"event": "album_snapshot_progress", "run_id": run_id,
                                             "file": filename, "rows_processed": processed, "albums_inserted": inserted,
                                             "next_row": checkpoint["next_row"], "file_complete": checkpoint["done"]}))
                        if checkpoint["done"]:
                            break
                    if checkpoint["next_row"] == source.num_rows and not checkpoint.get("done"):
                        checkpoint["done"] = True
                        self.store.set_checkpoint(key, checkpoint)
                except (AlbumSnapshotError, OSError, ValueError, KeyError) as exc:
                    # No row contents, signed CDN URLs, or arbitrary parser
                    # messages enter logs. Normalization errors are fixed codes.
                    message = str(exc) if isinstance(exc, AlbumSnapshotError) else type(exc).__name__
                    errors.append(f"{filename}: {message}")
                    break
                finally:
                    if source is not None:
                        source.close()
                    if downloaded:
                        path.unlink(missing_ok=True)
        checkpoints = {filename: self.store.get_checkpoint(self.checkpoint_key(filename), {}) for filename in FILES}
        complete = all(value.get("done", False) for value in checkpoints.values())
        provenance.update({"complete": complete, "files": checkpoints,
                           "albums_inserted": sum(value.get("rows_inserted", 0) for value in checkpoints.values()),
                           "albums_linked_inserted": sum(value.get("albums_linked_inserted", 0) for value in checkpoints.values())})
        self.store.set_checkpoint("album_snapshot", provenance)
        metrics = {
            "status": "partial", "api_status": "not_checked", "website_status": "not_checked",
            "snapshot_status": "complete" if complete else "partial", "import_kind": "album_snapshot",
            "snapshot_provider": "Hugging Face", "snapshot_revision": REVISION,
            "snapshot_published_at": PUBLISHED_AT, "snapshot_observation_date": None,
            "snapshot_license": "unknown", "records_discovered": inserted, "records_checked": 0,
            "records_verified_directly": 0, "records_inserted": inserted, "records_unchanged": existing,
            "records_skipped": invalid, "snapshot_rows_processed": processed,
            "albums_linked_inserted": linked_inserted, "artist_key_shapes": shapes, "band_links": links,
            "records_remaining": 0 if complete else None, "estimated_coverage": None,
            "estimated_confidence": self.store.summary().get("verification_ratio"),
            "error_summary": errors, "checkpoint": checkpoints,
            "source_note": "Third-party album snapshot only; observation date, license and current MA coverage are unknown. All imported albums require direct verification. No songs are imported.",
        }
        self.store.finish_run(run_id, **metrics)
        return {"run_id": run_id, "processed": processed, **metrics}


AlbumImporter = AlbumSnapshotImporter
