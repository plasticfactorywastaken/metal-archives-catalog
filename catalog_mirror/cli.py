"""Command line used identically in Actions and local development."""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import sys

from .store import Store
from .export import export_catalog
from .sources import ApiSource, WebsiteSource
from .transport import Transport
from .sync import SyncEngine
from .snapshot import SnapshotImporter
from .albums import AlbumSnapshotImporter
from .album_enrichment import AlbumEnricher
from .sheets import SheetsClient, apply_controls, controls_settings, report_tables, HEADERS, TAB_ORDER


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/settings.json")
    parser.add_argument("--db")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("probe")
    sync = sub.add_parser("sync")
    sync.add_argument("--mode", choices=("test","batch","full"), default="test")
    sync.add_argument("--limit", type=int)
    sync.add_argument("--seconds", type=int)
    sync.add_argument("--skip-sheets", action="store_true")
    bootstrap = sub.add_parser("bootstrap-snapshot", help="Import the pinned historical public Kaggle bands/labels snapshot")
    bootstrap.add_argument("--mode", choices=("test","batch","full"), default="full")
    bootstrap.add_argument("--limit", type=int, help="Maximum CSV rows inspected, including duplicates and skipped rows")
    bootstrap.add_argument("--seconds", type=int, default=7200)
    bootstrap.add_argument("--skip-sheets", action="store_true")
    albums = sub.add_parser("bootstrap-albums", help="Import albums only from the pinned public release-ID snapshot")
    albums.add_argument("--mode", choices=("test","batch","full"), default="full")
    albums.add_argument("--limit", type=int, help="Maximum projected source rows inspected, including duplicates")
    albums.add_argument("--seconds", type=int, default=7200)
    albums.add_argument("--skip-sheets", action="store_true")
    enrichment = sub.add_parser("enrich-albums", help="Add conservative band links and historical album year/type")
    enrichment.add_argument("--mode", choices=("test","batch","full"), default="full")
    enrichment.add_argument("--limit", type=int)
    enrichment.add_argument("--seconds", type=int, default=1800)
    enrichment.add_argument("--bands-only", action="store_true")
    enrichment.add_argument("--skip-sheets", action="store_true")
    sheets_sync = sub.add_parser("sync-sheets", help="Read private controls and refresh reports without requesting catalog sources")
    sheets_sync.add_argument("--skip-sheets", action="store_true")
    export = sub.add_parser("export")
    export.add_argument("--output")
    sub.add_parser("sheet-template")
    sub.add_parser("summary")
    args = parser.parse_args(argv)
    settings = json.loads(Path(args.config).read_text(encoding="utf-8"))
    if args.db:
        settings["database"] = args.db
    settings["sheet_id"] = os.environ.get("GOOGLE_SHEET_ID") or settings.get("sheet_id", "")
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    with Store(settings["database"]) as store:
        if args.command == "init":
            print(json.dumps({"status":"initialized","records":store.summary()["total_records"]}))
            return 0
        if args.command == "summary":
            summary = store.summary()
            print(json.dumps({k:v for k,v in summary.items() if k != "latest_run"}, indent=2))
            return 0
        if args.command == "sheet-template":
            tables = report_tables(store, settings.get("sheet_row_limit",1000))
            for title in TAB_ORDER:
                tables.setdefault(title,[HEADERS[title]])
            tables["Settings"] += [["sheet_row_limit", settings.get("sheet_row_limit",1000),"Reports only; complete catalog remains in SQLite"],
                ["weekly_schedule", "Monday 09:17 UTC", "Enable via repository SYNC_ENABLED variable"],
                ["manual_controls", "Genre Map and Classification Overrides", "Read before every scheduled run; never overwritten"],
                ["source_status","Blocked during initial validation","No catalog imported"],
                ["source_reference","https://www.metal-archives.com","Public catalog only"],
                ["api_reference","https://metal-api.dev","Unofficial API; search capped at 200"]]
            Path("state").mkdir(exist_ok=True)
            Path("state/sheet-template.json").write_text(json.dumps(tables, ensure_ascii=False, indent=2),encoding="utf-8")
            print(json.dumps({"path":"state/sheet-template.json","tabs":len(tables)}))
            return 0
        if args.command == "export":
            manifest = export_catalog(store, args.output or settings["export_directory"])
            print(json.dumps({"status":"exported","records":store.summary()["total_records"]}))
            return 0
        if args.command not in {"bootstrap-snapshot", "bootstrap-albums", "enrich-albums", "sync-sheets"}:
            transport = Transport(cache_dir=settings.get("cache_directory"),
                min_interval=settings.get("request_interval_seconds",8), user_agent=settings["user_agent"],
                timeout=settings.get("request_timeout_seconds",25), max_retries=settings.get("max_retries",2))
            api = ApiSource(transport, settings["api_base_url"],settings.get("api_search_terms"))
            website = WebsiteSource(transport,settings["website_base_url"])
        if args.command == "probe":
            results = {"api":api.probe(),"website":website.probe()}
            print(json.dumps(results, indent=2))
            return 0
        sheets, sheets_error = None, None
        if not args.skip_sheets and settings["sheet_id"]:
            try:
                sheets = SheetsClient(settings["sheet_id"])
                controls = sheets.read_controls(settings.get("sheet_control_limit",10000))
                updates = controls_settings(controls)
                apply_controls(store, controls)
                settings.update(updates)
            except Exception as exc:
                # Failed reads cannot replace manual classifications. Keep cached controls.
                sheets_error = f"Control import failed: {type(exc).__name__}: {exc}"
                sheets = None
        if args.command == "bootstrap-snapshot":
            result = SnapshotImporter(store).run(args.mode,args.limit,args.seconds)
        elif args.command == "bootstrap-albums":
            result = AlbumSnapshotImporter(store).run(args.mode,args.limit,args.seconds)
        elif args.command == "enrich-albums":
            result = AlbumEnricher(store).run(args.mode,args.limit,args.seconds,include_discography=not args.bands_only)
        elif args.command == "sync-sheets":
            run_id = store.start_run("sheets_only")
            store.finish_run(run_id, status="success", api_status="not_checked", website_status="not_checked")
            result = {"run_id":run_id,"processed":0,"records_remaining":None,"estimated_coverage":None,
                      "estimated_confidence":store.summary()["verification_ratio"]}
        else:
            result = SyncEngine(store,api,website,settings).run(args.mode,args.limit,args.seconds)
        if not settings["sheet_id"] or args.skip_sheets:
            store.update_run(result["run_id"],sheets_status="not_configured" if not settings["sheet_id"] else "skipped")
        if sheets_error:
            store.update_run(result["run_id"],status="partial",sheets_status="failed",sheets_error=sheets_error)
        elif sheets:
            try:
                store.update_run(result["run_id"],sheets_status="connected")
                sheets.publish(report_tables(store,settings.get("sheet_row_limit",1000)),settings.get("sheet_row_limit",1000))
            except Exception as exc:
                store.update_run(result["run_id"],status="partial",sheets_status="failed",sheets_error=str(exc))
        export_catalog(store, settings["export_directory"])
        print(json.dumps({"run_id":result["run_id"],"status":store.runs(1)[0]["status"],"processed":result["processed"],
                          "remaining_known":result["records_remaining"],"estimated_coverage":result["estimated_coverage"],
                          "estimated_confidence":result["estimated_confidence"]}))
        return 1 if args.command == "sync-sheets" and store.runs(1)[0].get("sheets_status") != "connected" else 0


if __name__ == "__main__":
    raise SystemExit(main())
