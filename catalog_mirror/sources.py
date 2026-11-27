"""Public source adapters with bounded discovery and explicit parser failures.

All catalog parsing is deterministic. No lyrics, images, forums or accounts are
included. Related stubs are distinguished from verified detail records.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import string
import urllib.parse
from html.parser import HTMLParser

from .transport import SourceUnavailable, SourceMissing, SourceBlocked, Transport

MA = "https://www.metal-archives.com"
ENTITY_PATH = {"band": "bands", "release": "albums", "artist": "artists", "label": "labels"}
PATH_ENTITY = {v: k for k, v in ENTITY_PATH.items()}


def source_url(entity_type, entity_id):
    if entity_type not in ENTITY_PATH or not str(entity_id).isdigit():
        raise ValueError("Only stable numeric public entity IDs have independent source pages")
    return f"{MA}/{ENTITY_PATH[entity_type]}/{entity_id}"


def _base(entity_type, entity_id, name, url):
    return {"entity_type": entity_type, "entity_id": str(entity_id), "name": str(name),
            "source_url": url, "ma_url": url}


def _url_entity(url):
    url = urllib.parse.urljoin(MA, url)
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("https", "http") or p.hostname != "www.metal-archives.com":
        return None
    parts = [x for x in p.path.split("/") if x]
    if len(parts) < 2 or parts[0] not in PATH_ENTITY or not parts[-1].isdigit():
        return None
    return PATH_ENTITY[parts[0]], parts[-1], urllib.parse.urlunsplit(("https", p.netloc, p.path, "", ""))


def _pick(obj, *names):
    normalized = {re.sub(r"[^a-z0-9]", "", str(k).lower()): v for k, v in obj.items()}
    for name in names:
        k = re.sub(r"[^a-z0-9]", "", name.lower())
        if k in normalized:
            return normalized[k]
    return None


def _list_payload(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("results", "data", "bands", "albums"):
            val = _pick(payload, key)
            if isinstance(val, list):
                return val
    raise SourceUnavailable("API search response did not contain a recognized result list")


def _date(value):
    """Preserve uncertainty: year/month values never acquire an invented day."""
    if value is None:
        return None
    value = str(value).strip()
    if re.fullmatch(r"\d{4}(?:-\d{2}(?:-\d{2})?)?", value):
        return value
    value = re.sub(r"(\d+)(?:st|nd|rd|th)", r"\1", value)
    for pattern, result in (("%B %d, %Y", "%Y-%m-%d"), ("%B %Y", "%Y-%m")):
        try:
            return dt.datetime.strptime(value, pattern).strftime(result)
        except ValueError:
            pass
    return value


def _release_fields(record):
    value = record.get("release_date")
    if value:
        record["release_date"] = _date(value)
        match = re.match(r"^(\d{4})", record["release_date"])
        if match:
            record["release_year"] = int(match[1])
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", record["release_date"]):
            try:
                record["is_upcoming"] = dt.date.fromisoformat(record["release_date"]) > dt.datetime.now(dt.timezone.utc).date()
            except ValueError:
                pass


def normalize_api(payload, entity_type, entity_id=None, *, discovery=False):
    if not isinstance(payload, dict):
        raise SourceUnavailable("API entity is not an object")
    id_value = _pick(payload, "id", f"{entity_type}Id", "albumId" if entity_type == "release" else "bandId")
    id_value = str(id_value if id_value is not None else entity_id or "")
    if not id_value.isdigit() or (entity_id is not None and id_value != str(entity_id)):
        raise SourceUnavailable("API response stable ID missing or mismatched")
    name = _pick(payload, "name", "title", "bandName", "albumName")
    if not isinstance(name, str) or not name.strip():
        raise SourceUnavailable("API response lacks a recognizable name/title")
    out = _base(entity_type, id_value, name, source_url(entity_type, id_value))
    original = _pick(payload, "url", "sourceUrl", "metalArchivesUrl", "maUrl")
    original_info = _url_entity(original) if isinstance(original, str) else None
    if original_info and original_info[:2] == (entity_type, id_value):
        out["source_url"] = out["ma_url"] = original_info[2]
    fields = {
        "aliases": ("aliases",), "country": ("country", "countryOfOrigin"),
        "location": ("location",), "status": ("status",), "formed_in": ("formedIn", "yearFormed"),
        "genres_raw": ("genre", "genres", "style"), "themes": ("lyricalThemes", "themes"),
        "release_type": ("type", "releaseType"), "release_date": ("releaseDate", "date"),
        "formats": ("format", "formats"), "label_name": ("labelName",),
        "band_id": ("bandId",), "band": ("bandName",), "label_id": ("labelId",),
        "source_updated_at": ("lastModified", "updatedAt"),
        "official_url": ("officialUrl", "officialWebsite", "website"),
    }
    for key, aliases in fields.items():
        value = _pick(payload, *aliases)
        if value is not None and isinstance(value, (str, int, float, bool, list)):
            # A genre string is preserved verbatim; lists retain order separately.
            if key == "genres_raw" and isinstance(value, list):
                out["genres_json"] = value
                out["genres_raw"] = json.dumps(value, ensure_ascii=False)
            else:
                out[key] = value
    label = _pick(payload, "label")
    if isinstance(label, str):
        out["label_name"] = label
    elif isinstance(label, dict):
        if _pick(label, "name") is not None:
            out["label_name"] = _pick(label, "name")
        if str(_pick(label, "id")).isdigit():
            out["label_id"] = str(_pick(label, "id"))
    if discovery:
        out["_discovery_only"] = True
    _release_fields(out)
    return out


class ApiSource:
    name = "api"

    def __init__(self, transport=None, base_url="https://metal-api.dev", search_terms=None):
        if base_url.rstrip("/") != "https://metal-api.dev":
            raise ValueError("Unreviewed API hosts are not supported")
        self.transport = transport or Transport()
        self.base_url = base_url.rstrip("/")
        self.search_terms = search_terms or list(string.ascii_uppercase) + list(string.digits)

    def probe(self):
        url = self.base_url + "/bands/1"
        try:
            self.fetch("band", "1")
            return {"status": "available", "url": url, "coverage": "search-limited; no full enumeration"}
        except SourceUnavailable as exc:
            return {"status": "blocked" if isinstance(exc, SourceBlocked) else "unavailable", "url": url, "error": str(exc)}

    def discover(self, checkpoint=None, limit=25):
        checkpoint = dict(checkpoint or {})
        index, offset = int(checkpoint.get("term", 0)), int(checkpoint.get("offset", 0))
        if index >= len(self.search_terms):
            return [], {**checkpoint, "done": True, "coverage_complete": False}, None
        term = self.search_terms[index]
        url = self.base_url + "/search/bands/name/" + urllib.parse.quote(term, safe="")
        rows = _list_payload(self.transport.get(url).json())
        records = []
        for row in rows[offset:offset + limit]:
            try:
                records.append(normalize_api(row, "band", discovery=True))
            except SourceUnavailable as exc:
                records.append({"entity_type": "band", "entity_id": "", "source_url": url,
                                "_parse_error": str(exc), "_discovery_only": True})
        next_offset = offset + len(records)
        next_index = index
        if next_offset >= len(rows):
            next_index, next_offset = index + 1, 0
        cp = {"term": next_index, "offset": next_offset, "done": next_index >= len(self.search_terms),
              "coverage_complete": False, "truncated_queries": int(checkpoint.get("truncated_queries", 0)) + int(offset == 0 and len(rows) >= 200)}
        return records, cp, None

    def fetch(self, entity_type, entity_id, url=None):
        if entity_type not in ("band", "release"):
            raise SourceUnavailable(f"API v1 does not provide {entity_type} detail")
        if not str(entity_id).isdigit():
            raise SourceUnavailable("API entity ID must be numeric")
        endpoint = f"{self.base_url}/{ENTITY_PATH[entity_type]}/{entity_id}"
        response = self.transport.get(endpoint)
        payload = response.json()
        record = normalize_api(payload, entity_type, entity_id)
        record["raw_source_ref"] = json.dumps({"url": endpoint, "sha256": hashlib.sha256(response.body).hexdigest()}, sort_keys=True)
        records = [record]
        if entity_type == "band":
            releases = _pick(payload, "albums", "discography", "releases") or []
            if isinstance(releases, list):
                for row in releases:
                    try:
                        child = normalize_api(row, "release", discovery=True)
                    except SourceUnavailable as exc:
                        record.setdefault("_source_warnings", []).append(f"Invalid API discography item: {exc}")
                        continue
                    child.setdefault("band_id", str(entity_id)); child.setdefault("band", record["name"])
                    records.append(child)
        else:
            songs = _pick(payload, "songs", "tracks") or []
            if isinstance(songs, list):
                for pos, row in enumerate(songs, 1):
                    if not isinstance(row, dict):
                        continue
                    title = _pick(row, "name", "title")
                    if not title:
                        continue
                    track = _pick(row, "trackNumber", "number") or pos
                    song_id = _pick(row, "id", "songId")
                    sid = str(song_id) if str(song_id).isdigit() else f"release:{entity_id}:track:{track}"
                    child = _base("song", sid, title, record["source_url"])
                    child.update({"release_id": str(entity_id), "band_id": record.get("band_id"), "track_number": track,
                                  "duration": _pick(row, "duration", "length"), "_embedded": True,
                                  "id_kind": "source" if str(song_id).isdigit() else "release_track_composite"})
                    records.append(child)
        return records


class Node:
    def __init__(self, tag="root", attrs=(), parent=None):
        self.tag, self.attrs, self.parent, self.children = tag, dict(attrs), parent, []

    @property
    def text(self):
        return "".join(c if isinstance(c, str) else c.text for c in self.children)

    def all(self, tag=None, *, id=None, cls=None):
        found = []
        for child in self.children:
            if isinstance(child, Node):
                if (tag is None or child.tag == tag) and (id is None or child.attrs.get("id") == id) and (cls is None or cls in child.attrs.get("class", "").split()):
                    found.append(child)
                found.extend(child.all(tag, id=id, cls=cls))
        return found

    def first(self, tag=None, **kwargs):
        found = self.all(tag, **kwargs)
        return found[0] if found else None

    def ancestor(self, tag):
        node = self.parent
        while node and node.tag != tag:
            node = node.parent
        return node


class _TreeParser(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = self.current = Node()
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag in ("td", "th") and self.current.tag in ("td", "th"):
            self.current = self.current.parent
        child = Node(tag, attrs, self.current)
        self.current.children.append(child)
        if tag not in self.VOID:
            self.current = child

    def handle_endtag(self, tag):
        node = self.current
        while node.parent:
            if node.tag == tag:
                self.current = node.parent
                return
            node = node.parent

    def handle_data(self, value):
        if self.current.tag not in ("script", "style"):
            self.current.children.append(value)


def _defs(root):
    out = {}
    for dl in root.all("dl"):
        key = None
        for child in dl.children:
            if not isinstance(child, Node):
                continue
            if child.tag == "dt":
                key = re.sub(r"\s+", " ", child.text.strip().rstrip(":" )).lower()
            elif child.tag == "dd" and key:
                out[key] = child
                key = None
    return out


def _relation(source, target, relation_type, role="", start_date=None, end_date=None):
    date_match = re.search(r"\((\d{4})\s*[-–]\s*(\d{4}|present)\)", role, re.I)
    identity_role = role
    if date_match:
        start_date = start_date or date_match[1]
        end_date = end_date or (date_match[2] if date_match[2].lower() != "present" else None)
        identity_role = (role[:date_match.start()] + role[date_match.end():]).strip()
    identity = "|".join((source["entity_type"], source["entity_id"], target["entity_type"], target["entity_id"], relation_type, identity_role))
    record = _base("relationship", hashlib.sha256(identity.encode()).hexdigest()[:32], relation_type, source["source_url"])
    record.update({"source_entity_type": source["entity_type"], "source_entity_id": source["entity_id"],
                   "target_entity_type": target["entity_type"], "target_entity_id": target["entity_id"],
                   "relationship_type": relation_type, "role": role, "start_date": start_date, "end_date": end_date, "_embedded": True})
    return record


def parse_website(html, entity_type, entity_id, url):
    root = _TreeParser(html).root
    title_class = {"band": "band_name", "release": "album_name", "artist": "artist_name", "label": "label_name"}.get(entity_type)
    heading = root.first(cls=title_class) if title_class else None
    if entity_type == "artist" and heading is None:
        heading = root.first(cls="band_member_name")
    if not heading or not heading.text.strip():
        raise SourceUnavailable(f"Unrecognized {entity_type} HTML structure; record retained")
    title_link = heading.first("a")
    title_identity = _url_entity(title_link.attrs.get("href", "")) if title_link else None
    if title_identity and title_identity[:2] != (entity_type, str(entity_id)):
        raise SourceUnavailable("Website response heading has a different stable ID")
    record = _base(entity_type, entity_id, heading.text.strip(), url)
    record["raw_source_ref"] = json.dumps({"url": url, "sha256": hashlib.sha256(html.encode()).hexdigest()}, sort_keys=True)
    definitions = _defs(root)
    fieldmap = {"country of origin": "country", "country": "country", "location": "location", "status": "status", "formed in": "formed_in",
                "genre": "genres_raw", "lyrical themes": "themes", "also known as": "aliases", "real/full name": "full_name",
                "type": "release_type", "release date": "release_date", "format": "formats", "label": "label_name",
                "current label": "label_name", "parent label": "parent_label", "sub-labels": "sub_label", "catalog id": "catalog_id"}
    for key, field in fieldmap.items():
        if key in definitions:
            record[field] = definitions[key].text if field == "genres_raw" else definitions[key].text.strip()
    if entity_type == "release":
        record["title"] = record["name"]
        _release_fields(record)
    records, seen = [record], {(entity_type, str(entity_id))}
    for anchor in root.all("a"):
        info = _url_entity(anchor.attrs.get("href", ""))
        if not info or not anchor.text.strip():
            continue
        kind, child_id, child_url = info
        child = _base(kind, child_id, anchor.text.strip(), child_url)
        child["_discovery_only"] = True
        if entity_type == "band" and kind == "release":
            child.update({"band_id": str(entity_id), "band": record["name"]})
        if (kind, child_id) not in seen:
            records.append(child); seen.add((kind, child_id))
        row = anchor.ancestor("tr")
        if kind == "label" and entity_type in ("band", "release"):
            # Only the label in the actual definition field establishes affiliation.
            if any(anchor in definitions[k].all("a") for k in ("label", "current label") if k in definitions):
                record["label_id"], record["label_name"] = child_id, child["name"]
                records.append(_relation(record, child, f"{entity_type}_label"))
        if entity_type == "release" and kind == "band" and anchor.ancestor("h2"):
            record["band_id"], record["band"] = child_id, child["name"]
            records.append(_relation(child, record, "band_release"))
        if kind == "artist" and entity_type in ("band", "release") and row:
            cells = row.all("td")
            role = cells[1].text.strip() if len(cells) > 1 else ""
            records.append(_relation(record, child, "member" if entity_type == "band" else "release_performer", role))
        if entity_type == "artist" and kind == "band" and row:
            cells = row.all("td")
            records.append(_relation(child, record, "member", cells[-1].text.strip() if len(cells) > 1 else ""))
    if entity_type == "release":
        for row in root.all("tr"):
            cells = row.all("td")
            if len(cells) < 3 or not re.fullmatch(r"\d+\.?", cells[0].text.strip()):
                continue
            # Lyrics text itself is never copied, requested or parsed.
            number = cells[0].text.strip().rstrip(".")
            title = cells[1].text.strip()
            duration = cells[2].text.strip()
            if not title or not re.fullmatch(r"\d+:\d{2}|\?+|", duration):
                continue
            source_id = None
            for node in row.all():
                match = re.fullmatch(r"(?:lyricsLink_|song)(\d+)", node.attrs.get("id", ""))
                if match:
                    source_id = match[1]; break
            sid = source_id or f"release:{entity_id}:track:{number}"
            song = _base("song", sid, title, url)
            song.update({"release_id": str(entity_id), "band_id": record.get("band_id"), "track_number": number,
                         "duration": duration, "_embedded": True, "id_kind": "source" if source_id else "release_track_composite"})
            records.append(song)
        # Public review count/rating only: no reviewer identity or review body.
        if "reviews" in definitions:
            text = definitions["reviews"].text.strip()
            review = _base("review", f"release:{entity_id}:review-summary", "Review summary", url)
            review.update({"release_id": str(entity_id), "summary": text, "_embedded": True})
            records.append(review)
        if record.get("is_upcoming"):
            upcoming = _base("upcoming", entity_id, record["name"], url)
            upcoming.update({"release_id": str(entity_id), "band_id": record.get("band_id"),
                             "expected_release_date": record.get("release_date"), "label": record.get("label_name"), "status": "announced", "_embedded": True})
            records.append(upcoming)
    # A duplicate HTML link cannot create duplicate entity records.
    unique = {}
    for row in records:
        unique.setdefault((row["entity_type"], row["entity_id"]), row)
    return list(unique.values())


class WebsiteSource:
    name = "website"
    LETTERS = ["NBR"] + list(string.ascii_uppercase) + ["~"]

    def __init__(self, transport=None, base_url=MA):
        if base_url.rstrip("/") != MA:
            raise ValueError("Unreviewed website hosts are not supported")
        self.transport = transport or Transport()
        self.base_url = base_url.rstrip("/")

    def probe(self):
        url = self.base_url + "/bands/1"
        try:
            self.fetch("band", "1")
            return {"status": "available", "url": url}
        except SourceUnavailable as exc:
            return {"status": "blocked" if isinstance(exc, SourceBlocked) else "unavailable", "url": url, "error": str(exc)}

    def discover(self, checkpoint=None, limit=25):
        checkpoint = dict(checkpoint or {})
        index, offset = int(checkpoint.get("letter", 0)), int(checkpoint.get("offset", 0))
        totals = dict(checkpoint.get("partition_totals", {}))
        if index >= len(self.LETTERS):
            return [], {**checkpoint, "done": True}, sum(totals.values()) if len(totals) == len(self.LETTERS) else None
        limit = max(1, min(int(limit), 100))
        letter = self.LETTERS[index]
        query = urllib.parse.urlencode({"sEcho": 1, "iDisplayStart": offset, "iDisplayLength": limit,
                                      "iSortCol_0": 0, "sSortDir_0": "asc"})
        url = f"{self.base_url}/browse/ajax-letter/l/{urllib.parse.quote(letter, safe='')}/json/1?{query}"
        payload = self.transport.get(url).json()
        if not isinstance(payload, dict) or not isinstance(payload.get("aaData"), list):
            raise SourceUnavailable("Unrecognized website browse response")
        try:
            total = int(payload["iTotalRecords"])
        except (ValueError, KeyError, TypeError) as exc:
            raise SourceUnavailable("Website browse count unavailable") from exc
        rows, records = payload["aaData"][:limit], []
        totals[letter] = total
        for row in rows:
            if not isinstance(row, list) or len(row) < 1:
                records.append({"entity_type": "band", "entity_id": "", "source_url": url,
                                "_parse_error": "Invalid website browse row", "_discovery_only": True})
                continue
            anchor = _TreeParser(str(row[0])).root.first("a")
            info = _url_entity(anchor.attrs.get("href", "")) if anchor else None
            if not info or info[0] != "band":
                records.append({"entity_type": "band", "entity_id": "", "source_url": url,
                                "_parse_error": "Browse row missing stable band ID", "_discovery_only": True})
                continue
            kind, entity_id, entity_url = info
            item = _base(kind, entity_id, anchor.text.strip(), entity_url)
            item["_discovery_only"] = True
            for pos, field in ((1, "country"), (2, "genres_raw"), (3, "status")):
                if len(row) > pos:
                    value = _TreeParser(str(row[pos])).root.text
                    item[field] = value if field == "genres_raw" else value.strip()
            records.append(item)
        if not rows and offset < total:
            raise SourceUnavailable("Empty browse page before reported total; checkpoint retained")
        next_offset, next_index = offset + len(rows), index
        if next_offset >= total:
            next_index, next_offset = index + 1, 0
        cp = {"letter": next_index, "offset": next_offset, "partition_totals": totals, "done": next_index >= len(self.LETTERS)}
        all_total = sum(totals.values()) if len(totals) == len(self.LETTERS) else None
        return records, cp, all_total

    def discover_upcoming(self, checkpoint=None, limit=25):
        """Public upcoming feed, independently resumable; detail verification follows.

        This legacy endpoint is documented by a public client but was not live
        tested because this environment received 403 on its initial verification.
        An invalid/blocked response leaves the caller's checkpoint unchanged.
        """
        checkpoint = dict(checkpoint or {})
        offset = int(checkpoint.get("offset", 0))
        limit = max(1, min(int(limit), 100))
        query = urllib.parse.urlencode({"sEcho": 1, "iDisplayStart": offset, "iDisplayLength": limit})
        url = f"{self.base_url}/release/ajax-upcoming/json/1?{query}"
        payload = self.transport.get(url).json()
        if not isinstance(payload, dict) or not isinstance(payload.get("aaData"), list):
            raise SourceUnavailable("Unrecognized upcoming feed response")
        try:
            total = int(payload["iTotalRecords"])
        except (KeyError, ValueError, TypeError) as exc:
            raise SourceUnavailable("Upcoming feed count unavailable") from exc
        records = []
        for row in payload["aaData"][:limit]:
            if not isinstance(row, list):
                records.append({"entity_type": "release", "entity_id": "", "source_url": url,
                                "_parse_error": "Invalid upcoming feed row", "_discovery_only": True})
                continue
            links = []
            for cell in row:
                links.extend(_TreeParser(str(cell)).root.all("a"))
            band, release = None, None
            for anchor in links:
                info = _url_entity(anchor.attrs.get("href", ""))
                if info:
                    child = _base(info[0], info[1], anchor.text.strip(), info[2])
                    child["_discovery_only"] = True
                    if info[0] == "band": band = child
                    if info[0] == "release": release = child
            if release is None:
                records.append({"entity_type": "release", "entity_id": "", "source_url": url,
                                "_parse_error": "Upcoming feed row has no stable release ID", "_discovery_only": True})
                continue
            if band:
                release.update({"band_id": band["entity_id"], "band": band["name"]})
            records.append(release)
        if not records and offset < total:
            raise SourceUnavailable("Empty upcoming feed page before reported total")
        return records, {"offset": offset + len(records), "done": offset + len(records) >= total}, total

    def fetch(self, entity_type, entity_id, url=None):
        if entity_type not in ENTITY_PATH:
            raise SourceUnavailable("Embedded entities must be verified through their parent page")
        url = url or source_url(entity_type, entity_id)
        identity = _url_entity(url)
        if not identity or identity[:2] != (entity_type, str(entity_id)):
            raise SourceUnavailable("Source URL does not match requested stable ID")
        response = self.transport.get(identity[2])
        records = parse_website(response.text, entity_type, str(entity_id), identity[2])
        if entity_type == "band":
            # Discography is a separate public tab. If unavailable, retain band details
            # and signal incomplete child discovery rather than losing the parent.
            tab_url = f"{self.base_url}/band/discography/id/{entity_id}/tab/all"
            try:
                tab = _TreeParser(self.transport.get(tab_url).text).root
                for row in tab.all("tr"):
                    anchor = row.first("a")
                    info = _url_entity(anchor.attrs.get("href", "")) if anchor else None
                    if info and info[0] == "release":
                        child = _base("release", info[1], anchor.text.strip(), info[2])
                        child.update({"band_id": str(entity_id), "band": records[0]["name"], "_discovery_only": True})
                        cells = row.all("td")
                        if len(cells) > 1:
                            child["release_type"] = cells[1].text.strip()
                        if len(cells) > 2 and cells[2].text.strip().isdigit():
                            child["release_year"] = int(cells[2].text.strip())
                        records.append(child)
            except SourceUnavailable as exc:
                records[0]["_source_warnings"] = [f"Discography unavailable: {exc}"]
                if isinstance(exc, SourceBlocked):
                    records[0]["_source_blocked"] = True
        return records


# Friendly aliases for CLI integrations.
MetalAPI = ApiSource
MetalArchives = WebsiteSource
