"""Bounded Google Sheets control panel. Manual tabs are read, never replaced."""
from __future__ import annotations

import itertools
import json
import os
import time
from urllib.parse import quote

from .store import canonical_json, utcnow

ENTITY_TABS = {"Bands":"band", "Releases":"release", "Songs":"song", "Labels":"label",
               "Artists":"artist", "Relationships":"relationship", "Upcoming":"upcoming"}
COMMON = ["entity_id", "name", "primary_genre", "secondary_genres", "ecommerce_primary_class", "country", "location",
          "label_name", "release_date", "source_url", "last_checked", "verification_status"]
HEADERS = {name:COMMON[:] for name in ENTITY_TABS}
HEADERS.update({
    "Dashboard":["Metric","Value"],
    "Changes":["run_id","detected_at","entity_type","entity_id","entity_name","change_type","changed_field","previous_value","new_value","source_url","review_status"],
    "Sync Runs":["run_id","started_at","finished_at","status","api_status","website_status","records_discovered","records_discovered_api","records_discovered_website","records_checked","records_verified_directly","records_skipped","records_inserted","records_updated","records_unchanged","records_removed","records_reappeared","conflicts","review_items","estimated_coverage","estimated_confidence","estimated_band_coverage","records_remaining","api_failures","website_failures","sheets_status","error_summary","checkpoint"],
    "Genre Map":["Original genre/style","E-commerce classification","Secondary tags","Confidence","Review status","Notes","Taxonomy version","Last updated"],
    "Classification Overrides":["Entity type","Stable entity ID","Field","Override value","Reason","Reviewer","Updated timestamp"],
    "Review Queue":["review_id","entity_type","entity_id","reason","field","details_json","source_url","review_status"],
    "Settings":["Setting","Value","Notes"]})
HEADERS["Bands"] += ["status","formed_in","genres_raw","genres_json"]
HEADERS["Releases"] += ["band_id","band_name","release_type","release_year","formats","is_upcoming"]
HEADERS["Songs"] += ["band_id","release_id","track_number","duration"]
HEADERS["Labels"] += ["status","parent_label","sub_label"]
HEADERS["Artists"] += ["aliases","status"]
HEADERS["Relationships"] = ["entity_id","source_entity_type","source_entity_id","target_entity_type","target_entity_id","relationship_type","role","start_date","end_date","source_url"]
HEADERS["Upcoming"] += ["band_id","announced_date","expected_release_date","status"]
MANUAL_TABS = {"Genre Map", "Classification Overrides", "Settings"}
TAB_ORDER = ["Dashboard", *ENTITY_TABS, "Changes", "Sync Runs", "Genre Map", "Classification Overrides", "Review Queue", "Settings"]


def scalar(value):
    if isinstance(value, (dict,list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return "" if value is None else value


def report_tables(store, row_limit=1000):
    if not 1 <= int(row_limit) <= 10000:
        raise ValueError("Sheet report row limit must be between 1 and 10000")
    tables = {}
    for title, entity_type in ENTITY_TABS.items():
        tables[title] = [HEADERS[title]] + [[scalar(row.get(c)) for c in HEADERS[title]]
            for row in itertools.islice(store.iter_entities(entity_type), row_limit)]
    collections = {
        "Changes": store.iter_changes(limit=row_limit),
        "Sync Runs": store.runs(limit=row_limit),
        "Review Queue": (dict(r) for r in store.conn.execute("SELECT * FROM review_queue ORDER BY rowid DESC LIMIT ?", (row_limit,))),
    }
    for title, rows in collections.items():
        tables[title] = [HEADERS[title]] + [[scalar(row.get(c)) for c in HEADERS[title]] for row in rows]
    summary = store.summary()
    tables["Dashboard"] = [HEADERS["Dashboard"], ["Panel row limit per entity",row_limit],
        ["Complete data", "SQLite snapshot and partitioned exports"],
        ["Coverage interpretation", "Unknown until a reliable global source count exists"],
        ["Confidence interpretation", "Verified share of imported records; not global completeness"]]
    tables["Dashboard"] += [[key,scalar(value)] for key,value in summary.items() if not isinstance(value, (list,dict))]
    tables["Dashboard"] += [["entity_count_" + key,value] for key,value in summary.get("entity_counts",{}).items()]
    for checkpoint_name in ("historical_snapshot", "album_snapshot"):
        snapshot = store.get_checkpoint(checkpoint_name, {})
        if snapshot:
            tables["Dashboard"] += [[checkpoint_name + "_" + field, scalar(snapshot.get(field))]
                for field in ("provider","published_at","observation_date","complete","unverified")]
    tables["Dashboard"] += [[key,scalar(value)] for key,value in summary.get("weekly",{}).items()]
    tables["Dashboard"] += [[key,scalar(summary.get("latest_run",{}).get(key))] for key in ("run_id","finished_at","api_status","website_status","status","sheets_status","sheets_error")]
    return tables


class SheetsClient:
    def __init__(self, spreadsheet_id, session=None):
        self.spreadsheet_id = spreadsheet_id
        if session is None:
            try:
                from google.oauth2 import service_account
                from google.auth.transport.requests import AuthorizedSession
            except ImportError as exc:
                raise RuntimeError("Install the optional sheets dependencies") from exc
            secret = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
            if not secret:
                raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is missing")
            credentials = service_account.Credentials.from_service_account_info(json.loads(secret),
                scopes=["https://www.googleapis.com/auth/spreadsheets"])
            session = AuthorizedSession(credentials)
        self.session = session
        self.base = "https://sheets.googleapis.com/v4/spreadsheets/" + quote(spreadsheet_id, safe="")

    def request(self, method, path, **kwargs):
        for attempt in range(4):
            response = self.session.request(method, self.base + path, timeout=60, **kwargs)
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 3:
                    time.sleep(min(2 ** (attempt+2), 32))
                    continue
            if response.status_code >= 400:
                raise RuntimeError(f"Google Sheets returned HTTP {response.status_code}")
            return response.json()
        raise RuntimeError("Google Sheets retry limit")

    def read_controls(self, max_rows=10000):
        if not 1 <= int(max_rows) <= 100000:
            raise ValueError("Control row limit must be between 1 and 100000")
        # One bounded read per input; extra row detects truncation instead of silently dropping rules.
        result = {}
        for title in ("Genre Map", "Classification Overrides", "Review Queue", "Settings"):
            end = len(HEADERS[title])
            col = chr(ord('A') + end - 1)
            path = "/values/" + quote(f"'{title}'!A1:{col}{max_rows+2}", safe="") + "?valueRenderOption=UNFORMATTED_VALUE&dateTimeRenderOption=FORMATTED_STRING"
            rows = self.request("GET", path).get("values", [])
            if len(rows) > max_rows + 1:
                raise RuntimeError(f"{title} exceeds configured control limit; no rules applied")
            if rows and rows[0] != HEADERS[title]:
                raise ValueError(f"{title}: headers changed; refusing ambiguous control import")
            result[title] = [dict(zip(HEADERS[title], row + [""]*(end-len(row)))) for row in rows[1:] if any(row)]
        return result

    def publish(self, tables, row_limit=1000):
        if not 1 <= int(row_limit) <= 10000:
            raise ValueError("Sheet report row limit must be between 1 and 10000")
        # Reject all forbidden writes before performing any partial publication.
        if any(title in MANUAL_TABS or title not in HEADERS for title in tables):
            raise ValueError("Refusing to overwrite manual or unknown control-panel tabs")
        # Read metadata first; never assume tab IDs, and never write a manual tab.
        meta = self.request("GET", "?fields=sheets.properties")
        ids = {s["properties"]["title"]:s["properties"]["sheetId"] for s in meta["sheets"]}
        missing = set(TAB_ORDER)-set(ids)
        if missing:
            raise ValueError("Missing control-panel tabs: " + ", ".join(sorted(missing)))
        for title, rows in tables.items():
            if title in MANUAL_TABS:
                raise ValueError("Refusing to overwrite manual tab " + title)
            # Review status/notes have been imported before this publication.
            sheet_id = ids[title]
            required = max(row_limit+1, len(rows)+1)
            self.request("POST", ":batchUpdate", json={"requests":[
                {"updateSheetProperties":{"properties":{"sheetId":sheet_id,"gridProperties":{"rowCount":required,"columnCount":max(26,len(HEADERS[title])),"frozenRowCount":1}},"fields":"gridProperties"}}
            ]})
            # Atomic cell replacement also clears stale rows, while preserving formatting.
            data = [{"values":[{"userEnteredValue":{"boolValue":v}} if isinstance(v,bool) else
                               {"userEnteredValue":{"numberValue":v}} if isinstance(v,(int,float)) else
                               {"userEnteredValue":{"stringValue":str(v)}} for v in row]} for row in rows]
            self.request("POST", ":batchUpdate", json={"requests":[
                {"updateCells":{"range":{"sheetId":sheet_id,"startRowIndex":0,"endRowIndex":required,"startColumnIndex":0,"endColumnIndex":len(HEADERS[title])},"rows":data,"fields":"userEnteredValue"}},
                {"setBasicFilter":{"filter":{"range":{"sheetId":sheet_id,"startRowIndex":0,"endRowIndex":max(2,len(rows)),"startColumnIndex":0,"endColumnIndex":len(HEADERS[title])}}}}
            ]})


def controls_settings(controls):
    """Only the bounded reporting row limit is editable from the control panel."""
    settings = {}
    for row in controls.get("Settings", []):
        if row.get("Setting") == "sheet_row_limit":
            value = float(row.get("Value", ""))
            if not value.is_integer() or not 1 <= value <= 10000:
                raise ValueError("sheet_row_limit must be an integer between 1 and 10000")
            settings["sheet_row_limit"] = int(value)
    return settings


def apply_controls(store, controls):
    """Validate first, then apply all manual controls in one SQLite transaction.

    Empty or incomplete input does not delete cached classifications. Explicit
    removals use the store's delete_override API instead of guessing whether a
    temporarily empty Sheet is an intentional deletion.
    """
    mappings, overrides, reviews = [], [], []
    controls_settings(controls)  # Validate editable settings before any mutation.
    timestamp = utcnow()
    allowed = {"ecommerce_primary_class","ecommerce_secondary_tags","classification_status","classification_confidence"}
    for row in controls.get("Genre Map", []):
        if not row["Original genre/style"]:
            continue
        confidence = float(row["Confidence"] or 0)
        if not 0 <= confidence <= 1:
            raise ValueError("Genre confidence must be between 0 and 1")
        mappings.append((row["Original genre/style"], row["E-commerce classification"],
            canonical_json([s.strip() for s in str(row["Secondary tags"]).split(";") if s.strip()]),
            confidence, row["Review status"] or "needs_review", row["Notes"],
            row["Taxonomy version"] or "v1", row["Last updated"] or timestamp))
    for row in controls.get("Classification Overrides", []):
        if not row["Stable entity ID"]:
            continue
        if row["Field"] not in allowed or row["Entity type"] not in set(ENTITY_TABS.values()):
            raise ValueError("Override field or entity type is invalid")
        value = row["Override value"]
        if row["Field"] == "ecommerce_secondary_tags":
            value = [s.strip() for s in str(value).split(";") if s.strip()]
        if row["Field"] == "classification_confidence":
            value = float(value)
            if not 0 <= value <= 1:
                raise ValueError("Override confidence must be between 0 and 1")
        elif row["Field"] != "ecommerce_secondary_tags":
            value = str(value)
        overrides.append((row["Entity type"], str(row["Stable entity ID"]), row["Field"], canonical_json(value),
                          row["Reason"], row["Reviewer"], row["Updated timestamp"] or timestamp))
    for row in controls.get("Review Queue", []):
        if row["review_id"] and row["review_status"] in ("unresolved","resolved","ignored"):
            review_id = int(row["review_id"])
            if review_id < 1:
                raise ValueError("Review IDs must be positive integers")
            reviews.append((row["review_status"], timestamp, review_id))
    # The Store setter methods commit independently; a single batch transaction
    # here is needed to guarantee all-or-nothing control-panel refreshes.
    with store.conn:
        store.conn.executemany(
            "INSERT INTO genre_map VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(original_genre) DO UPDATE SET ecommerce_classification=excluded.ecommerce_classification,secondary_tags=excluded.secondary_tags,confidence=excluded.confidence,review_status=excluded.review_status,notes=excluded.notes,taxonomy_version=excluded.taxonomy_version,last_updated=excluded.last_updated", mappings)
        store.conn.executemany(
            "INSERT INTO classification_overrides VALUES(?,?,?,?,?,?,?) ON CONFLICT(entity_type,entity_id,field) DO UPDATE SET override_value=excluded.override_value,reason=excluded.reason,reviewer=excluded.reviewer,updated_at=excluded.updated_at", overrides)
        store.conn.executemany("UPDATE review_queue SET review_status=?,updated_at=? WHERE review_id=?", reviews)
