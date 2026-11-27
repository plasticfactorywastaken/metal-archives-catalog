"""Durable, checksummed snapshots in free public GitHub Releases, never Actions caches.

Uses the runner's gh CLI and GITHUB_TOKEN. Uploads to a draft release, then
publishes only after every part succeeds. Restore never silently resets corrupt state.
"""
from __future__ import annotations
import argparse
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone

CHUNK = 64 * 1024 * 1024
COOLDOWN_NAME = re.compile(r"^[0-9a-f]{64}\.cooldown\.json$")


def cooldowns_in(cache_dir):
    result = {}
    for path in sorted(Path(cache_dir).glob("*.cooldown.json")):
        if not COOLDOWN_NAME.fullmatch(path.name) or path.stat().st_size > 4096:
            raise RuntimeError("Invalid cooldown metadata")
        value = json.loads(path.read_text(encoding="utf-8"))
        until = value.get("until")
        if not isinstance(until, (int, float)) or not math.isfinite(until) or until < 0:
            raise RuntimeError("Invalid cooldown expiry")
        result[path.name] = {"until": until}
    return result


def gh(*args):
    proc = subprocess.run(["gh", *args], capture_output=True, text=True)
    if proc.returncode:
        raise RuntimeError("GitHub operation failed: " + " ".join(args[:2]) + "; state retained")
    return proc.stdout


def digest(path):
    value = hashlib.sha256()
    with open(path,"rb") as stream:
        for block in iter(lambda:stream.read(1024*1024),b""):
            value.update(block)
    return value.hexdigest()


def pack(db, directory, cache_dir=None):
    db = Path(db)
    if not db.is_file():
        raise RuntimeError("No existing database to snapshot; refusing an empty replacement")
    directory = Path(directory); directory.mkdir(parents=True,exist_ok=True)
    backup = directory/"catalog.sqlite"
    if backup.resolve() == db.resolve():
        raise ValueError("Snapshot staging directory must differ from the source database directory")
    source = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) if db.is_absolute() else sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    destination = sqlite3.connect(backup)
    try:
        source.backup(destination)
        if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Refusing corrupt snapshot")
    finally:
        destination.close()
        source.close()
    zipped = directory/"catalog.sqlite.gz"
    with open(backup,"rb") as src, open(zipped,"wb") as target:
        with gzip.GzipFile(filename="", mode="wb", fileobj=target, mtime=0) as dst:
            shutil.copyfileobj(src,dst)
    parts=[]
    with open(zipped,"rb") as stream:
        index=0
        for block in iter(lambda:stream.read(CHUNK),b""):
            name=f"catalog.sqlite.gz.part{index:04d}"
            path=directory/name; path.write_bytes(block)
            parts.append({"name":name,"sha256":digest(path),"size":len(block)})
            index+=1
    manifest={"format_version":1,"sqlite_sha256":digest(backup),"sqlite_size":backup.stat().st_size,
              "parts":parts,"created_at":datetime.now(timezone.utc).isoformat()}
    cooldown_path = directory / "cooldowns.json"
    cooldown_path.write_text(json.dumps(cooldowns_in(cache_dir or db.parent / "cache"), sort_keys=True, allow_nan=False), encoding="utf-8")
    manifest["cooldown_asset"] = {"name": cooldown_path.name, "sha256": digest(cooldown_path), "size": cooldown_path.stat().st_size}
    (directory/"snapshot-manifest.json").write_text(json.dumps(manifest,indent=2),encoding="utf-8")
    backup.unlink(); zipped.unlink()
    return manifest


def unpack(directory, db, cache_dir=None):
    directory, db=Path(directory),Path(db)
    manifest=json.loads((directory/"snapshot-manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format_version") != 1 or not isinstance(manifest.get("parts"), list) or not manifest.get("parts"):
        raise RuntimeError("Invalid snapshot manifest")
    size = manifest.get("sqlite_size")
    if not isinstance(size, int) or size <= 0 or not re.fullmatch(r"[0-9a-f]{64}", str(manifest.get("sqlite_sha256", ""))):
        raise RuntimeError("Invalid snapshot database metadata")
    # Never replace the main SQLite file underneath a live/stale WAL.
    if Path(str(db) + "-wal").exists():
        raise RuntimeError("Database has a WAL file; close/checkpoint it before restoring")
    cooldowns = {}
    if "cooldown_asset" in manifest:
        asset = manifest["cooldown_asset"]
        if asset.get("name") != "cooldowns.json" or not isinstance(asset.get("size"), int) or not 0 <= asset["size"] <= 1024 * 1024:
            raise RuntimeError("Invalid cooldown asset metadata")
        asset_path = directory / "cooldowns.json"
        if not asset_path.is_file() or asset_path.stat().st_size != asset["size"] or digest(asset_path) != asset.get("sha256"):
            raise RuntimeError("Cooldown checksum mismatch")
        cooldowns = json.loads(asset_path.read_text(encoding="utf-8"))
        if not isinstance(cooldowns, dict):
            raise RuntimeError("Invalid cooldown asset")
        for name, value in cooldowns.items():
            until = value.get("until") if isinstance(value, dict) else None
            if not COOLDOWN_NAME.fullmatch(name) or not isinstance(until, (int, float)) or not math.isfinite(until) or until < 0:
                raise RuntimeError("Invalid persisted cooldown")
    compressed=directory/"restore.sqlite.gz"
    with open(compressed,"wb") as dst:
        for index, part in enumerate(manifest["parts"]):
            expected_name = f"catalog.sqlite.gz.part{index:04d}"
            if part.get("name") != expected_name:
                raise RuntimeError("Invalid snapshot part path")
            path=directory/part["name"]
            if not isinstance(part.get("size"), int) or not 0 < part["size"] <= CHUNK or not path.is_file() or path.stat().st_size != part["size"] or digest(path)!=part.get("sha256"):
                raise RuntimeError("Snapshot checksum mismatch; refusing fresh import")
            with open(path,"rb") as src: shutil.copyfileobj(src,dst)
    db.parent.mkdir(parents=True,exist_ok=True)
    temp=db.with_suffix(".restoring.sqlite")
    try:
        with gzip.open(compressed,"rb") as src,open(temp,"wb") as dst:
            remaining = size
            while remaining:
                block = src.read(min(1024 * 1024, remaining))
                if not block:
                    break
                dst.write(block)
                remaining -= len(block)
            if src.read(1):
                raise RuntimeError("Restored database exceeds the declared size")
        if temp.stat().st_size!=manifest["sqlite_size"] or digest(temp)!=manifest["sqlite_sha256"]:
            raise RuntimeError("Database checksum mismatch")
        check = sqlite3.connect(temp.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Database integrity failure")
        finally:
            check.close()
        # Restore throttling before activating the database. Never shorten an
        # existing Retry-After when restoring an older snapshot.
        cache_dir = Path(cache_dir or db.parent / "cache")
        existing_cooldowns = cooldowns_in(cache_dir)
        if cooldowns:
            cache_dir.mkdir(parents=True, exist_ok=True)
        for name, value in cooldowns.items():
            value = {"until": max(value["until"], existing_cooldowns.get(name, {}).get("until", 0))}
            temporary_cooldown = cache_dir / (name + ".restoring")
            temporary_cooldown.write_text(json.dumps(value, allow_nan=False), encoding="utf-8")
            os.replace(temporary_cooldown, cache_dir / name)
        os.replace(temp,db)
    finally:
        if temp.exists(): temp.unlink()
        if compressed.exists(): compressed.unlink()


def restore(db, allow_empty=False, cache_dir=None):
    # Releases API pagination prevents accidentally missing a valid older state release.
    releases=json.loads(gh("api","--paginate","--slurp","repos/{owner}/{repo}/releases?per_page=100"))
    candidates=[r for page in releases for r in page if not r.get("draft") and r["tag_name"].startswith("catalog-state-")]
    if not candidates:
        if allow_empty:
            print(json.dumps({"restore":"initial_empty_import"})); return
        raise RuntimeError("No published state snapshot. Explicitly enable ALLOW_INITIAL_IMPORT once; refusing silent reset.")
    latest=max(candidates,key=lambda:r["published_at"])
    with tempfile.TemporaryDirectory() as directory:
        gh("release","download",latest["tag_name"],"--dir",directory,"--pattern","snapshot-manifest.json")
        manifest = json.loads((Path(directory) / "snapshot-manifest.json").read_text(encoding="utf-8"))
        patterns = ["--pattern", "catalog.sqlite.gz.part*"]
        if "cooldown_asset" in manifest:
            patterns += ["--pattern", "cooldowns.json"]
        gh("release","download",latest["tag_name"],"--dir",directory,*patterns)
        unpack(directory,db,cache_dir)
    print(json.dumps({"restore":"verified","snapshot":latest["tag_name"]}))


def publish(db, cache_dir=None):
    tag="catalog-state-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"-"+os.environ.get("GITHUB_RUN_ID","local")+"-"+os.environ.get("GITHUB_RUN_ATTEMPT","1")
    with tempfile.TemporaryDirectory() as directory:
        pack(db,directory,cache_dir)
        gh("release","create",tag,"--draft","--target",os.environ.get("GITHUB_SHA","main"),"--title","Catalog state "+tag,"--notes","Checksummed SQLite snapshot, including checkpoints, source data, history, mappings, overrides, and HTTP cooldown metadata. Partial runs retain previously imported records.")
        for path in sorted(Path(directory).iterdir()):
            gh("release","upload",tag,str(path))
        gh("release","edit",tag,"--draft=false","--latest=false")
    print(json.dumps({"published":tag}))


if __name__=="__main__":
    parser=argparse.ArgumentParser(); parser.add_argument("command",choices=("restore","publish")); parser.add_argument("--db",default="state/catalog.sqlite"); parser.add_argument("--cache-dir"); parser.add_argument("--allow-empty",action="store_true")
    args=parser.parse_args()
    if args.command=="restore": restore(args.db,args.allow_empty,args.cache_dir)
    else: publish(args.db,args.cache_dir)
