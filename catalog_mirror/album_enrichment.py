"""Conservative local band linkage and pinned historical discography enrichment.

Only this importer's unverified HF releases are eligible. Existing values and
user overrides are never replaced. Exact-name links remain explicitly inferred;
Kaggle matches must be unique on the exact (band ID, album title) pair.
"""
from __future__ import annotations

import csv
import hashlib
import http.client
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import time
import urllib.error
import urllib.request

from .albums import DATASET, REVISION
from .snapshot import SafeRedirect, SnapshotError, safe_download_url, stable_id
from .store import canonical_json, content_hash, utcnow

DISCOGRAPHY_URL = "https://www.kaggle.com/api/v1/datasets/download/guimacrlh/every-metal-archives-band-october-2024/all_bands_discography.csv?datasetVersionNumber=3"
DISCOGRAPHY_BYTES = 33116482
DISCOGRAPHY_HEADERS = ("Album Name", "Type", "Year", "Reviews", "Band ID")
DISCOGRAPHY_PUBLISHED_AT = "2024-11-09T13:34:11.417Z"
MAX_BYTES = 40 * 1024 * 1024
CHECKPOINT = "album_enrichment:" + REVISION


class EnrichmentError(RuntimeError):
    pass


class DiscographyDownloader:
    def __init__(self, opener=None, clock=time.monotonic):
        self.opener = opener or urllib.request.build_opener(SafeRedirect())
        self.clock = clock

    def download(self, destination, deadline):
        destination = Path(destination)
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise EnrichmentError("Enrichment time budget expired")
        request = urllib.request.Request(DISCOGRAPHY_URL, headers={
            "User-Agent": "CatalogMirror/0.1 (public historical discography metadata)",
            "Accept": "text/csv", "Accept-Encoding": "identity",
        })
        try:
            with self.opener.open(request, timeout=min(30, remaining)) as response:
                safe_download_url(response.geturl())
                if response.status != 200:
                    raise EnrichmentError(f"Discography HTTP {response.status}; no retry or bypass attempted")
                if "text/html" in response.headers.get("Content-Type", "").lower():
                    raise EnrichmentError("Discography response was HTML")
                length = response.headers.get("Content-Length")
                if length is not None and int(length) != DISCOGRAPHY_BYTES:
                    raise EnrichmentError("Discography response differs from pinned byte length")
                size = 0
                with destination.open("wb") as stream:
                    while True:
                        if self.clock() >= deadline:
                            raise EnrichmentError("Enrichment time budget expired during download")
                        block = response.read(min(1024 * 1024, DISCOGRAPHY_BYTES - size + 1))
                        if not block:
                            break
                        size += len(block)
                        if size > min(DISCOGRAPHY_BYTES, MAX_BYTES):
                            raise EnrichmentError("Discography exceeds pinned byte limit")
                        stream.write(block)
            if size != DISCOGRAPHY_BYTES:
                raise EnrichmentError("Discography download was incomplete")
        except urllib.error.HTTPError as exc:
            destination.unlink(missing_ok=True)
            raise EnrichmentError(f"Discography HTTP {exc.code}; no retry or bypass attempted") from None
        except (urllib.error.URLError, http.client.IncompleteRead, TimeoutError, OSError) as exc:
            destination.unlink(missing_ok=True)
            raise EnrichmentError(f"Discography download failed: {type(exc).__name__}") from None
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return destination


def _check_time(clock, deadline):
    if clock() >= deadline:
        raise EnrichmentError("Enrichment time budget expired while building the temporary index")


def _band_index(store, index, clock, deadline):
    index.execute("CREATE TABLE band_names(name TEXT PRIMARY KEY,band_id TEXT NOT NULL,matches INTEGER NOT NULL)")
    cursor = store.conn.execute("SELECT name,entity_id FROM entities WHERE entity_type='band' AND name!=''")
    while rows := cursor.fetchmany(500):
        _check_time(clock, deadline)
        with index:
            index.executemany("INSERT INTO band_names VALUES(?,?,1) ON CONFLICT(name) DO UPDATE SET matches=matches+1", [(row[0], row[1]) for row in rows])


def _discography_index(path, index, clock, deadline):
    path = Path(path)
    if path.stat().st_size != DISCOGRAPHY_BYTES or path.stat().st_size > MAX_BYTES:
        raise EnrichmentError("Discography file differs from pinned byte length")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            _check_time(clock, deadline)
            digest.update(block)
    index.execute("CREATE TABLE discography(band_id TEXT NOT NULL,title TEXT NOT NULL,matches INTEGER NOT NULL,release_year TEXT,release_type TEXT,PRIMARY KEY(band_id,title))")
    csv.field_size_limit(1024 * 1024)
    valid = invalid = 0
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != DISCOGRAPHY_HEADERS:
            raise EnrichmentError("Discography CSV headers changed")
        batch = []
        for row in reader:
            band_id, title = stable_id(row.get("Band ID")), row.get("Album Name")
            if band_id is None or not isinstance(title, str) or not title.strip():
                invalid += 1
            else:
                year = row.get("Year") or ""
                year = year if re.fullmatch(r"[12][0-9]{3}", year) else None
                release_type = row.get("Type") or None
                batch.append((band_id, title, year, release_type))
                valid += 1
            if (valid + invalid) % 500 == 0:
                _check_time(clock, deadline)
                with index:
                    index.executemany("INSERT INTO discography VALUES(?,?,1,?,?) ON CONFLICT(band_id,title) DO UPDATE SET matches=matches+1", batch)
                batch.clear()
        with index:
            index.executemany("INSERT INTO discography VALUES(?,?,1,?,?) ON CONFLICT(band_id,title) DO UPDATE SET matches=matches+1", batch)
    return digest.hexdigest(), valid, invalid


def _updates(data, index, include_discography, checksum):
    """Return only additive fields plus aggregate outcome labels."""
    updates = {}
    band_id = data.get("band_id")
    link = "already_linked" if band_id else "unmatched"
    if not band_id and data.get("band_name"):
        match = index.execute("SELECT band_id,matches FROM band_names WHERE name=?", (data["band_name"],)).fetchone()
        if match is not None and match[1] == 1:
            band_id = match[0]
            updates.update({"band_id": band_id, "band_link_method": "unique_exact_name", "band_link_verified": False,
                            "band_link_provenance": "Inferred from exactly one existing catalog band with an identical name"})
            link = "unique_exact_name"
        elif match is not None:
            link = "ambiguous_name"
    match_status = "not_requested"
    if include_discography:
        match_status = "unmatched"
        if band_id:
            match = index.execute("SELECT matches,release_year,release_type FROM discography WHERE band_id=? AND title=?", (str(band_id), data.get("name", ""))).fetchone()
            if match is not None and match[0] == 1:
                match_status = "unique_exact_band_title"
                if not data.get("release_year") and match[1]:
                    updates["release_year"] = match[1]
                if not data.get("release_type") and match[2]:
                    updates["release_type"] = match[2]
                if "release_year" in updates or "release_type" in updates:
                    updates.update({"discography_match_method": "unique_exact_band_id_and_title",
                                    "discography_match_verified": False, "discography_snapshot_version": 3,
                                    "discography_published_at": DISCOGRAPHY_PUBLISHED_AT,
                                    "discography_observation_date": None,
                                    "discography_source_ref": DISCOGRAPHY_URL + "#sha256=" + checksum})
            elif match is not None:
                match_status = "ambiguous_band_title"
    return updates, link, match_status


class AlbumEnricher:
    def __init__(self, store, downloader=None, batch_size=500, clock=time.monotonic):
        if not isinstance(batch_size, int) or not 1 <= batch_size <= 500:
            raise ValueError("Enrichment batch size must be between 1 and 500")
        self.store = store
        self.downloader = downloader or DiscographyDownloader(clock=clock)
        self.batch_size = batch_size
        self.clock = clock

    def run(self, mode="full", limit=None, seconds=1800, include_discography=True, discography_file=None):
        """Cloud caller omits discography_file; local tests use synthetic CSV only.

        Limits count eligible albums scanned, not source CSV rows. Temporary
        indexes are rebuilt from immutable data per run; the main album cursor
        and all additive changes commit together in batches of at most 500.
        Completed passes restart so later imports can be enriched on a new run.
        """
        if mode not in {"test", "batch", "full"}:
            raise ValueError("Invalid enrichment mode")
        limit = limit if limit is not None else {"test": 25, "batch": 25000, "full": 1000000}[mode]
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or seconds <= 0:
            raise ValueError("Positive enrichment budgets are required")
        deadline = self.clock() + seconds
        run_id = self.store.start_run("album_enrichment_" + mode)
        key = CHECKPOINT + (":discography" if include_discography else ":bands")
        checkpoint = self.store.get_checkpoint(key, {})
        cursor = "" if checkpoint.get("done") else checkpoint.get("next_entity_id", "")
        processed = updated = band_linked = year_added = type_added = 0
        errors, links, matches = [], {}, {}
        done = False
        checksum = None
        source_rows = source_invalid = 0
        try:
            with tempfile.TemporaryDirectory(prefix="catalog-album-enrichment-") as temporary:
                index = sqlite3.connect(str(Path(temporary) / "index.sqlite"))
                try:
                    _band_index(self.store, index, self.clock, deadline)
                    if include_discography:
                        path = Path(discography_file) if discography_file is not None else self.downloader.download(Path(temporary) / "discography.csv", deadline)
                        checksum, source_rows, source_invalid = _discography_index(path, index, self.clock, deadline)
                        previous = self.store.get_checkpoint("album_discography_source", {})
                        if previous.get("checksum") not in (None, checksum):
                            raise EnrichmentError("Pinned discography checksum changed; album cursor retained")
                        self.store.set_checkpoint("album_discography_source", {
                            "source_url": DISCOGRAPHY_URL, "version": 3, "checksum": checksum,
                            "published_at": DISCOGRAPHY_PUBLISHED_AT, "observation_date": None,
                            "license": "Apache 2.0 (provider declaration)", "bytes": DISCOGRAPHY_BYTES,
                        })
                    condition = "(json_extract(data_json,'$.band_id') IS NULL OR json_extract(data_json,'$.band_id')='')"
                    if include_discography:
                        condition = "(" + condition + " OR json_extract(data_json,'$.release_year') IS NULL OR json_extract(data_json,'$.release_year')='' OR json_extract(data_json,'$.release_type') IS NULL OR json_extract(data_json,'$.release_type')='')"
                    sql = ("SELECT entity_id,data_json,source_url FROM entities WHERE entity_type='release' AND verification_status!='verified' "
                           "AND json_extract(data_json,'$.snapshot_dataset')=? AND json_extract(data_json,'$.snapshot_revision')=? "
                           "AND entity_id>? AND " + condition + " ORDER BY entity_id LIMIT ?")
                    while processed < limit and self.clock() < deadline:
                        requested = min(self.batch_size, limit - processed)
                        rows = self.store.conn.execute(sql, (DATASET, REVISION, cursor, requested)).fetchall()
                        if not rows:
                            done = True
                            break
                        delta_updated = delta_linked = delta_year = delta_type = 0
                        batch_links, batch_matches = {}, {}
                        timestamp = utcnow()
                        with self.store.conn:
                            for row in rows:
                                old_data = json.loads(row["data_json"])
                                fields, link, match = _updates(old_data, index, include_discography, checksum)
                                batch_links[link] = batch_links.get(link, 0) + 1
                                batch_matches[match] = batch_matches.get(match, 0) + 1
                                if not fields:
                                    continue
                                data = {**old_data, **fields}
                                digest = content_hash(data)
                                self.store.conn.execute("UPDATE entities SET data_json=?,content_hash=?,last_changed_at=?,last_changed_run=? WHERE entity_type='release' AND entity_id=?", (canonical_json(data), digest, timestamp, run_id, row["entity_id"]))
                                self.store.conn.execute(
                                    "INSERT INTO source_observations(entity_type,entity_id,source,data_json,content_hash,checked_at,run_id,verified) VALUES('release',?,?,?,?,?,?,0) ON CONFLICT(entity_type,entity_id,source) DO UPDATE SET data_json=excluded.data_json,content_hash=excluded.content_hash,checked_at=excluded.checked_at,run_id=excluded.run_id",
                                    (row["entity_id"], "enrichment:exact_band_name:kaggle_v3", canonical_json(fields), content_hash(fields), timestamp, run_id))
                                for field in ("band_id", "release_year", "release_type"):
                                    if field in fields:
                                        self.store._change(run_id, "release", row["entity_id"], data.get("name", ""), "release_date_changed" if field == "release_year" else "updated", field, old_data.get(field), fields[field], row["source_url"], timestamp)
                                delta_updated += 1
                                delta_linked += int("band_id" in fields)
                                delta_year += int("release_year" in fields)
                                delta_type += int("release_type" in fields)
                            next_cursor = rows[-1]["entity_id"]
                            done = len(rows) < requested
                            checkpoint = {"next_entity_id": next_cursor, "done": done, "revision": REVISION,
                                          "discography_checksum": checksum, "include_discography": include_discography}
                            self.store.conn.execute("INSERT INTO checkpoints VALUES(?,?,?) ON CONFLICT(checkpoint_key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at", (key, canonical_json(checkpoint), timestamp))
                            self.store._increment(run_id, "records_updated", delta_updated)
                        cursor = next_cursor
                        processed += len(rows)
                        updated += delta_updated
                        band_linked += delta_linked
                        year_added += delta_year
                        type_added += delta_type
                        for target, values in ((links, batch_links), (matches, batch_matches)):
                            for name, count in values.items():
                                target[name] = target.get(name, 0) + count
                        if done:
                            break
                    if done:
                        checkpoint = {**checkpoint, "next_entity_id": cursor, "done": True, "revision": REVISION}
                        self.store.set_checkpoint(key, checkpoint)
                finally:
                    index.close()
        except (EnrichmentError, SnapshotError, OSError, csv.Error, UnicodeError, ValueError) as exc:
            errors.append(str(exc) if isinstance(exc, (EnrichmentError, SnapshotError)) else type(exc).__name__)
        metrics = {
            "status": "partial", "import_kind": "album_enrichment", "enrichment_status": "complete" if done else "partial",
            "api_status": "not_checked", "website_status": "not_checked",
            "records_checked": 0, "records_verified_directly": 0, "records_updated": updated,
            "albums_scanned": processed, "bands_linked_by_inference": band_linked,
            "release_years_added": year_added, "release_types_added": type_added,
            "band_link_outcomes": links, "discography_match_outcomes": matches,
            "discography_rows_indexed": source_rows, "discography_rows_invalid": source_invalid,
            "discography_checksum": checksum, "records_remaining": 0 if done else None,
            "estimated_coverage": None, "error_summary": errors, "checkpoint": checkpoint,
            "estimated_confidence": self.store.summary().get("verification_ratio"),
            "source_note": "Exact unique name links are inferred, not directly verified. Year/type use unique exact band-ID/title matches from Kaggle v3; source observation dates are unknown.",
        }
        self.store.finish_run(run_id, **metrics)
        return {"run_id": run_id, "processed": processed, **metrics}
