import copy
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote

from catalog_mirror.sheets import HEADERS, MANUAL_TABS, TAB_ORDER, SheetsClient, apply_controls, controls_settings, report_tables
from catalog_mirror.store import Store


def control_row(title, **values):
    return {**{header: "" for header in HEADERS[title]}, **values}


class Response:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self):
        self.calls = []
        self.ids = {title: index + 50 for index, title in enumerate(TAB_ORDER)}
        self.values = {title: [HEADERS[title]] for title in ("Genre Map", "Classification Overrides", "Review Queue", "Settings")}
        self.fail_title = None

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, copy.deepcopy(kwargs)))
        if method == "GET" and "?fields=sheets.properties" in url:
            return Response({"sheets": [{"properties": {"title": title, "sheetId": sheet_id}} for title, sheet_id in self.ids.items()]})
        if method == "GET" and "/values/" in url:
            title = unquote(url.split("/values/")[1]).split("'!")[0].lstrip("'")
            if title == self.fail_title:
                return Response({}, 403)
            return Response({"values": self.values[title]})
        return Response({})


class SheetsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "catalog.sqlite")
        self.run = self.store.start_run("test")
        self.session = FakeSession()
        self.client = SheetsClient("synthetic-sheet-id", self.session)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_cell_export_uses_raw_strings_not_formulas(self):
        self.store.upsert({"entity_type": "band", "entity_id": "1", "name": "=IMPORTDATA(\"https://invalid.test\")", "source_url": "https://www.metal-archives.com/bands/1"}, self.run, verified=True)
        tables = report_tables(self.store, 10)
        self.client.publish(tables, row_limit=10)
        cells = []
        for method, _, kwargs in self.session.calls:
            if method != "POST":
                continue
            for request in kwargs["json"]["requests"]:
                for row in request.get("updateCells", {}).get("rows", []):
                    cells.extend(row["values"])
        self.assertTrue(any(cell.get("userEnteredValue", {}).get("stringValue", "").startswith("=IMPORTDATA") for cell in cells))
        self.assertFalse(any("formulaValue" in cell.get("userEnteredValue", {}) for cell in cells))

    def test_publication_never_writes_manual_tabs(self):
        self.client.publish(report_tables(self.store, 5), row_limit=5)
        modified_ids = set()
        for method, _, kwargs in self.session.calls:
            if method != "POST":
                continue
            for request in kwargs["json"]["requests"]:
                if "updateCells" in request:
                    modified_ids.add(request["updateCells"]["range"]["sheetId"])
        self.assertTrue(modified_ids)
        self.assertTrue(modified_ids.isdisjoint({self.session.ids[title] for title in MANUAL_TABS}))
        with self.assertRaises(ValueError):
            self.client.publish({"Bands": [HEADERS["Bands"]], "Genre Map": [HEADERS["Genre Map"]]})

    def test_forbidden_write_fails_before_any_calls(self):
        with self.assertRaises(ValueError):
            self.client.publish({"Bands": [HEADERS["Bands"]], "Settings": [HEADERS["Settings"]]})
        self.assertEqual(self.session.calls, [])

    def test_missing_tab_fails_before_mutation(self):
        del self.session.ids["Genre Map"]
        with self.assertRaises(ValueError):
            self.client.publish(report_tables(self.store, 10), row_limit=10)
        self.assertTrue(all(method == "GET" for method, _, _ in self.session.calls))

    def test_control_read_is_bounded_and_header_checked(self):
        result = self.client.read_controls(max_rows=2)
        self.assertEqual(result["Genre Map"], [])
        self.assertTrue(all("4" in unquote(url).split("!")[-1] for _, url, _ in self.session.calls))
        self.assertTrue(all("valueRenderOption=UNFORMATTED_VALUE" in url and "dateTimeRenderOption=FORMATTED_STRING" in url for _, url, _ in self.session.calls))
        self.session.values["Genre Map"] = [["changed header"]]
        with self.assertRaises(ValueError):
            self.client.read_controls(max_rows=2)

    def test_numeric_google_cells_and_formatted_dates(self):
        mapping = control_row("Genre Map", **{"Original genre/style": "Doom", "E-commerce classification": "Doom", "Confidence": 0.85, "Last updated": "2026-11-26"})
        override = control_row("Classification Overrides", **{"Entity type": "band", "Stable entity ID": 1, "Field": "classification_confidence", "Override value": 0.9, "Updated timestamp": "2026-11-26"})
        self.session.values["Genre Map"].append([mapping[header] for header in HEADERS["Genre Map"]])
        self.session.values["Classification Overrides"].append([override[header] for header in HEADERS["Classification Overrides"]])
        controls = self.client.read_controls()
        apply_controls(self.store, controls)
        self.assertEqual(self.store.genre_mappings()["Doom"]["confidence"], 0.85)
        self.assertEqual(self.store.genre_mappings()["Doom"]["last_updated"], "2026-11-26")
        self.assertEqual(self.store.overrides()[0]["override_value"], 0.9)
        self.assertEqual(self.store.overrides()[0]["entity_id"], "1")

    def test_editable_report_setting_is_bounded(self):
        self.assertEqual(controls_settings({"Settings": [{"Setting": "sheet_row_limit", "Value": 25.0}]}), {"sheet_row_limit": 25})
        for value in (0, -1, 10001, 1.5, "invalid", "nan"):
            with self.assertRaises(ValueError):
                controls_settings({"Settings": [{"Setting": "sheet_row_limit", "Value": value}]})

    def test_oversized_controls_fail_without_silent_truncation(self):
        self.session.values["Genre Map"] = [HEADERS["Genre Map"], ["one"], ["two"], ["three"]]
        with self.assertRaises(RuntimeError):
            self.client.read_controls(max_rows=2)

    def test_failed_control_read_keeps_cached_overrides(self):
        self.store.set_override("band", "1", "ecommerce_primary_class", "Manual class")
        self.session.fail_title = "Classification Overrides"
        with self.assertRaises(RuntimeError):
            apply_controls(self.store, self.client.read_controls())
        self.assertEqual(self.store.overrides()[0]["override_value"], "Manual class")

    def test_invalid_late_override_keeps_prior_controls_atomically(self):
        self.store.set_genre_mapping("Doom", "Old mapping")
        controls = {
            "Genre Map": [control_row("Genre Map", **{"Original genre/style": "Doom", "E-commerce classification": "Replacement", "Confidence": "0.8"})],
            "Classification Overrides": [control_row("Classification Overrides", **{"Entity type": "band", "Stable entity ID": "1", "Field": "classification_confidence", "Override value": "not-a-number"})],
        }
        with self.assertRaises(ValueError):
            apply_controls(self.store, controls)
        self.assertEqual(self.store.genre_mappings()["Doom"]["ecommerce_classification"], "Old mapping")
        self.assertEqual(self.store.overrides(), [])

    def test_database_failure_rolls_back_entire_control_import(self):
        self.store.set_genre_mapping("Doom", "Old mapping")
        self.store.conn.execute("CREATE TEMP TRIGGER simulate_failure BEFORE INSERT ON classification_overrides BEGIN SELECT RAISE(ABORT, 'simulated failure'); END")
        controls = {
            "Genre Map": [control_row("Genre Map", **{"Original genre/style": "Doom", "E-commerce classification": "Replacement", "Confidence": "0.8"})],
            "Classification Overrides": [control_row("Classification Overrides", **{"Entity type": "band", "Stable entity ID": "1", "Field": "ecommerce_primary_class", "Override value": "New class"})],
        }
        with self.assertRaises(Exception):
            apply_controls(self.store, controls)
        self.assertEqual(self.store.genre_mappings()["Doom"]["ecommerce_classification"], "Old mapping")

    def test_empty_controls_preserve_manual_values(self):
        self.store.set_genre_mapping("Doom", "Manual mapping")
        self.store.set_override("band", "1", "ecommerce_primary_class", "Manual class")
        apply_controls(self.store, {"Genre Map": [], "Classification Overrides": []})
        self.assertEqual(self.store.genre_mappings()["Doom"]["ecommerce_classification"], "Manual mapping")
        self.assertEqual(self.store.overrides()[0]["override_value"], "Manual class")

    def test_review_resolution_preserved_in_next_export(self):
        review_id = self.store.add_review("band", "1", "source_conflict", self.run, details={"website": "A", "api": "B"})
        apply_controls(self.store, {"Review Queue": [control_row("Review Queue", review_id=str(review_id), review_status="resolved")]})
        tables = report_tables(self.store, 10)
        row = dict(zip(tables["Review Queue"][0], tables["Review Queue"][1]))
        self.assertEqual(row["review_status"], "resolved")
        self.assertEqual(self.store.summary()["review_items"], 0)

    def test_weekly_summary_and_extra_run_metrics(self):
        record = {"entity_type": "band", "entity_id": "1", "name": "Synthetic", "genres_raw": "Doom/Black"}
        self.store.upsert(record, self.run, verified=True)
        self.store.upsert({**record, "genres_raw": "Black/Doom"}, self.run, verified=True)
        self.store.finish_run(self.run, records_discovered_api=2, records_discovered_website=3, api_failures=1, sheets_status="connected")
        tables = report_tables(self.store, 10)
        dashboard = dict(tables["Dashboard"][1:])
        self.assertEqual(dashboard["new_records"], 1)
        self.assertEqual(dashboard["genre_changes"], 1)
        self.assertEqual(dashboard["primary_genre_changes"], 1)
        run_row = dict(zip(tables["Sync Runs"][0], tables["Sync Runs"][1]))
        self.assertEqual(run_row["records_discovered_api"], 2)
        self.assertEqual(run_row["records_discovered_website"], 3)
        self.assertEqual(run_row["api_failures"], 1)
        self.assertEqual(run_row["sheets_status"], "connected")
        change_rows = [dict(zip(tables["Changes"][0], row)) for row in tables["Changes"][1:]]
        primary = next(row for row in change_rows if row["changed_field"] == "primary_genre")
        self.assertEqual(primary["previous_value"], "Doom")
        self.assertEqual(primary["new_value"], "Black")


if __name__ == "__main__":
    unittest.main()
