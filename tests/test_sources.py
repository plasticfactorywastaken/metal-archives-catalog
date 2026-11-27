import json
import unittest

from catalog_mirror.sources import ApiSource, WebsiteSource, normalize_api, parse_website, SourceUnavailable
from catalog_mirror.transport import Response


BAND_HTML = '''<h1 class="band_name"><a href="/bands/Fixture/101">Fixture</a></h1>
<div id="band_stats"><dl><dt>Country of origin:</dt><dd>Exampleland</dd><dt>Location:</dt><dd>Fixture City</dd>
<dt>Status:</dt><dd>Active</dd><dt>Formed in:</dt><dd>2001</dd></dl>
<dl><dt>Genre:</dt><dd>Black Metal / Doom Metal / Black Metal</dd><dt>Current label:</dt>
<dd><a href="https://www.metal-archives.com/labels/Fixture_Label/202">Fixture Label</a></dd></dl></div>
<table><tr><td><a href="/artists/Fixture_Artist/303">Fixture Artist</a></td><td>Guitars (2001-present)</td></tr></table>'''

ALBUM_HTML = '''<h1 class="album_name">Fixture Release</h1><h2 class="band_name"><a href="/bands/Fixture/101">Fixture</a></h2>
<dl><dt>Type:</dt><dd>Full-length</dd><dt>Release date:</dt><dd>January 1st, 2099</dd><dt>Label:</dt>
<dd><a href="/labels/Fixture_Label/202">Fixture Label</a></dd><dt>Format:</dt><dd>CD</dd><dt>Reviews:</dt><dd>1 review (80%)</dd></dl>
<table class="table_lyrics"><tr><td>1.</td><td>Fixture Song</td><td>04:12</td><td><a id="lyricsLink_707">Show lyrics</a></td></tr>
<tr><td>2.</td><td>Fixture Song Two</td><td>03:00</td><td></td></tr></table>'''


class FakeTransport:
    def __init__(self, payloads): self.payloads, self.urls = list(payloads), []
    def get(self, url):
        self.urls.append(url)
        value = self.payloads.pop(0)
        if isinstance(value, Exception): raise value
        body = json.dumps(value) if isinstance(value, (dict, list)) else value
        return Response(url, 200, body.encode(), "application/json" if isinstance(value, (dict, list)) else "text/html")


class SourceTests(unittest.TestCase):
    def test_band_end_to_end_parser_raw_genre_and_relationships(self):
        records = parse_website(BAND_HTML, "band", "101", "https://www.metal-archives.com/bands/Fixture/101")
        self.assertEqual(records[0]["genres_raw"], "Black Metal / Doom Metal / Black Metal")
        self.assertEqual(records[0]["country"], "Exampleland")
        self.assertEqual(records[0]["label_id"], "202")
        self.assertEqual(len([x for x in records if x["entity_type"] == "relationship"]), 2)
        self.assertTrue(all(x.get("_discovery_only") for x in records if x["entity_type"] in ("artist", "label")))
    def test_release_embedded_song_ids_reviews_upcoming(self):
        records = parse_website(ALBUM_HTML, "release", "505", "https://www.metal-archives.com/albums/Fixture/Release/505")
        self.assertEqual(records[0]["release_date"], "2099-01-01")
        self.assertEqual(records[0]["band_id"], "101")
        songs = [x for x in records if x["entity_type"] == "song"]
        self.assertEqual([x["entity_id"] for x in songs], ["707", "release:505:track:2"])
        self.assertTrue(all(x["_embedded"] for x in songs))
        self.assertEqual(len([x for x in records if x["entity_type"] == "upcoming"]), 1)
        self.assertEqual(len([x for x in records if x["entity_type"] == "review"]), 1)
        self.assertNotIn("lyrics", json.dumps(records).lower())
    def test_parser_failure_is_not_missing(self):
        with self.assertRaises(SourceUnavailable): parse_website("<h1>Error</h1>", "band", "101", "x")
    def test_api_requires_matching_identity(self):
        with self.assertRaises(SourceUnavailable): normalize_api({"id": 101, "name": "Fixture"}, "band", 102)
    def test_api_drops_images_and_lyrics(self):
        t = FakeTransport([{"id": 505, "name": "Fixture", "coverUrl": "ignored", "songs": [{"id": 707, "title": "Fixture Song", "lyrics": "omitted"}]}])
        records = ApiSource(t).fetch("release", "505")
        self.assertEqual(records[1]["entity_id"], "707")
        self.assertNotIn("omitted", json.dumps(records))
        self.assertNotIn("coverUrl", json.dumps(records))
    def test_api_bounded_discovery_resumes(self):
        payload = [{"id": 101, "name": "Fixture A"}, {"id": 102, "name": "Fixture B"}]
        api = ApiSource(FakeTransport([payload, payload]), search_terms=["A"])
        first, cp, total = api.discover({}, 1)
        second, cp, total = api.discover(cp, 1)
        self.assertEqual(first[0]["entity_id"], "101")
        self.assertEqual(second[0]["entity_id"], "102")
        self.assertTrue(cp["done"])
        self.assertFalse(cp["coverage_complete"])
        self.assertIsNone(total)
    def test_invalid_discovery_row_becomes_explicit_exception_without_hiding_other_rows(self):
        api = ApiSource(FakeTransport([[{"unknown": "value"}, {"id": 102, "name": "Fixture"}]]), search_terms=["A"])
        rows, cp, _ = api.discover({}, 2)
        self.assertIn("_parse_error", rows[0])
        self.assertEqual(rows[1]["entity_id"], "102")
        self.assertTrue(cp["done"])
    def test_website_discovery_does_not_advance_on_invalid_response(self):
        site = WebsiteSource(FakeTransport([{"iTotalRecords": 20, "aaData": []}]))
        with self.assertRaises(SourceUnavailable): site.discover({"offset": 0}, 5)
    def test_website_discovery_checkpoint_and_ids(self):
        rows = [["<a href='/bands/Fixture/101'>Fixture</a>", "Exampleland", "Doom/Black", "Active"]]
        site = WebsiteSource(FakeTransport([{"iTotalRecords": 2, "aaData": rows}]))
        records, cp, total = site.discover({}, 1)
        self.assertEqual(records[0]["entity_id"], "101")
        self.assertEqual(cp["offset"], 1)
        self.assertEqual(cp["letter"], 0)
        self.assertIsNone(total)
    def test_band_retained_when_discography_fails(self):
        site = WebsiteSource(FakeTransport([BAND_HTML, SourceUnavailable("temporary failure")]))
        records = site.fetch("band", "101")
        self.assertEqual(records[0]["entity_id"], "101")
        self.assertIn("_source_warnings", records[0])
    def test_source_url_id_mismatch_refused(self):
        site = WebsiteSource(FakeTransport([]))
        with self.assertRaises(SourceUnavailable): site.fetch("band", "101", "https://www.metal-archives.com/bands/102")
    def test_website_response_id_mismatch_refused(self):
        with self.assertRaises(SourceUnavailable): parse_website(BAND_HTML, "band", "102", "https://www.metal-archives.com/bands/102")
    def test_genre_whitespace_preserved(self):
        html = BAND_HTML.replace("<dd>Black Metal / Doom Metal / Black Metal</dd>", "<dd>  Black Metal / Doom Metal  </dd>")
        rows = parse_website(html, "band", "101", "https://www.metal-archives.com/bands/101")
        self.assertEqual(rows[0]["genres_raw"], "  Black Metal / Doom Metal  ")
    def test_upcoming_discovery_checkpoint(self):
        payload = {"iTotalRecords": 1, "aaData": [["2099-01-01", "<a href='/bands/Fixture/101'>Fixture</a>", "<a href='/albums/Fixture/Release/505'>Fixture Release</a>", "Doom", "Full-length"]]}
        site = WebsiteSource(FakeTransport([payload]))
        records, cp, total = site.discover_upcoming({}, 1)
        self.assertEqual(records[0]["entity_id"], "505")
        self.assertEqual(records[0]["band_id"], "101")
        self.assertTrue(records[0]["_discovery_only"])
        self.assertTrue(cp["done"])
        self.assertEqual(total, 1)
    def test_artist_and_label_detail_fields(self):
        artist = parse_website("<h1 class='band_member_name'>Fixture Artist</h1><dl><dt>Also known as:</dt><dd>Alias</dd></dl>", "artist", "303", "https://www.metal-archives.com/artists/303")
        self.assertEqual(artist[0]["aliases"], "Alias")
        label = parse_website("<h1 class='label_name'>Fixture Label</h1><dl><dt>Parent label:</dt><dd>Fixture Parent</dd></dl>", "label", "202", "https://www.metal-archives.com/labels/202")
        self.assertEqual(label[0]["parent_label"], "Fixture Parent")


if __name__ == "__main__": unittest.main()
