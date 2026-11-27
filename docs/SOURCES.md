# Source adapters and access evidence

The worker is ordinary deterministic Python. It does not use an LLM, browser
automation, cookies, login, proxies, CAPTCHA solvers, or alternate identities.
It fetches only allowlisted public catalog endpoints. It never requests forum,
user-account, artwork, image or lyric URLs. Source HTML is parsed in memory and
never saved as a catalog export. Provenance contains the URL and response SHA-256.

## Observed access on November 26, 2026

The initial bounded probe made the following requests. It printed metadata only.

| Endpoint | Observed outcome |
| --- | --- |
| `https://metal-api.dev/bands/1` | Unavailable: Python `URLError`; no API record was imported |
| `https://www.metal-archives.com/robots.txt` | HTTP 200, plain text |
| `https://www.metal-archives.com/bands/1` | HTTP 403; all further website probes stopped |

An independent documentation fetch of
`https://metal-api.dev/swagger/v1/swagger.json` returned HTTP 502 through the web
research tool. The live API schema could not be confirmed. These outcomes are
environment-specific observations, not a claim that the services never work.

The website robots file allowed generic public search crawling but explicitly
disallowed AI fetch agents. Catalog content was not browsed through an AI fetch
tool. The one verification above exercised the intended deterministic metadata
worker identity after evaluating robots. It received 403, and that block was
respected. No alternate identity, browser challenge or proxy was attempted.

No catalog entity endpoint succeeded in this implementation session. Parser tests
use small, explicitly synthetic fixtures. They prove tested behavior, not current
compatibility with the blocked live website. After access becomes available, run
the bounded test mode before increasing the batch size.

## Third-party discovery

[Metal-API documentation](https://metal-api.dev/) lists:

- `GET /bands/{bandId}`
- `GET /albums/{albumId}`
- `GET /search/bands/name/{name}`
- `GET /search/bands/genre/{genre}`
- `GET /search/albums/title/{title}`

Its documentation states that search returns at most 200 results. The adapter
searches configurable name terms, resumes within each response, and marks capped
queries. API search does **not** provide exhaustive enumeration or a reliable
modified-since cursor. The API's listed artist, label and additional relationship
features are planned; this implementation does not pretend they exist. Only
bands and releases are fetched directly from this API.

API values are mapped by known property aliases; unrecognized structure is an
error, never an empty successful catalog. Unknown fields, lyrics, images and
account information are discarded. Genre strings are preserved verbatim; ordered
genre arrays are retained as ordered arrays, with JSON representation in the raw
field because there was no original string.

## Website discovery and verification

The fallback walks the website's public alphabetic browse endpoint using stable
IDs and offsets: `/browse/ajax-letter/l/{letter}/json/1`. It uses `NBR`, A–Z and
the special `~` partition, and records individual reported counts. This legacy
interface is evidenced by the public
[MetalAPI scraper implementation](https://github.com/tobocop2/MetalAPI/blob/master/scraper/scraper/spiders/ma_spider.py).
The `~` partition and all live endpoint shapes remain unverified in this session.
An unsupported partition fails visibly; it cannot silently mark the pass complete.
An empty page before a reported total is an error and does not advance its cursor.

The public upcoming endpoint `/release/ajax-upcoming/json/1` is implemented with
its own bounded offset cursor. The endpoint is evidenced by the public
[metal-archives.el client](https://github.com/seblemaguer/metal-archives.el/blob/master/metal-archives.el).
This adapter returns release stubs that still require detail verification.

Band verification also reads the public `/band/discography/id/{id}/tab/all` tab.
A band probe can therefore require robots, detail and discography requests when
accessible. Discography failure preserves the band and emits a source warning.
Related band, release, artist and label links become queued discovery stubs,
not fully verified records. Embedded track, relationship and public review-summary
metadata are verified through their parent detail page. Review bodies and reviewer
account identities are excluded.

Tracks use the source song ID when exposed. Where absent they use an explicitly
marked `release:{id}:track:{position}` composite, which can change when track
positions change. Relationship IDs are deterministic composites; dates and roles
remain separately inspectable. This is practical stable-ID handling, not a claim
that every source table has a publicly exposed primary key.

## Limits on coverage and weekly freshness

Alphabetic discovery plus related-page expansion can build substantial coverage
when allowed, but there is no verified complete export or modification feed.
Artists or labels unlinked from discovered bands/releases may remain undiscovered.
Changes outside a sampled/rotating recheck are detected only when that entity is
checked again. API name searches are capped and cannot guarantee all weekly
additions. The upcoming feed improves release discovery when accessible.

Offset pagination can move while the live source changes. Reconciliation must
repeat completed discovery passes; one pass is not proof of completeness. A
numerical band coverage estimate is justified only once all partition counts are
available. It must not be used as the coverage denominator for songs, labels,
artists or relationships. Whole-catalog coverage is unknown at this point.

## Request policy and caching

- At least five seconds between network requests to the same host, including
  retries and permitted redirects; respect longer robots crawl delays.
- At most two retries after network/5xx failures, with bounded exponential backoff.
- HTTP 401, 403, 429, CAPTCHA or a challenge stops that host for the run. A saved
  cooldown honors a longer `Retry-After`; no short retry loop attempts a bypass.
- Check robots before catalog requests. Missing robots (404/410) follows normal
  robots semantics; unavailable/malformed robots blocks catalog access.
- Only confirmed HTTP 404/410 from a detail request is `SourceMissing`. A parser
  error, timeout, 403 or empty index page cannot become removal evidence.
- A 32-response in-memory cache avoids repeated requests in a bounded run without
  storing full HTML. The persistent cache directory stores host cooldown metadata.
- Four MB response cap; accepted responses are text/HTML/JSON only. Redirects to
  another host or outside allowlisted paths are refused.

No source credential is needed by the documented public endpoints. Restored
permitted live access remains necessary for current data and weekly changes.
The historical bootstrap below does not solve or work around the block.

## Historical bootstrap available November 26, 2026

[Every Metal Archives Band November 2024](https://www.kaggle.com/datasets/guimacrlh/every-metal-archives-band-october-2024)
is a public third-party snapshot. The provider's [metadata API](https://www.kaggle.com/api/v1/datasets/view/guimacrlh/every-metal-archives-band-october-2024)
reports version 3, publication update `2024-11-09T13:34:11.417Z`, and license label
`Apache 2.0`. The old October slug is not an observation timestamp. The precise
age of each source record is unknown; source modification dates stay null.

Two individual public downloads were verified with bounded GET range requests:

- `https://www.kaggle.com/api/v1/datasets/download/guimacrlh/every-metal-archives-band-october-2024/metal_bands_roster.csv?datasetVersionNumber=3` — 32,956,677 bytes according to file metadata.
- `https://www.kaggle.com/api/v1/datasets/download/guimacrlh/every-metal-archives-band-october-2024/labels_roster.csv?datasetVersionNumber=3` — 17,424,004 bytes according to file metadata.

Both returned HTTP 206 through ordinary public Google storage redirects, without
credentials. HEAD is unsupported and returned 404; it is not used to decide
availability. Only headers and metadata were inspected in model context. Full
downloads and deterministic import are reserved for the cloud workflow.

The provider describes 194,196 band rows. That is a historical provider claim,
not a measured current coverage denominator. The label roster repeats labels
for multiple bands and requires stable-ID deduplication. Only bands and labels
are imported. The available discography file lacks release IDs and original
release URLs, so it is not imported as authoritative release entities.

All imported records remain pending direct verification, with empty
`last_checked`, explicit dataset/version/publication provenance, and checksums.
Original genres retain their order and raw text. Photo URLs are discarded;
neither photos nor artwork are downloaded. Existing records and classification
controls are preserved. A completed snapshot import is still a partial catalog
with unknown global coverage and no promise of current weekly updates.

## Albums-only public snapshot

The public [fairygaze/music dataset](https://huggingface.co/datasets/fairygaze/music)
has a separate `metal-archives` partition. Revision
`06856d4d785dce8b029551bcca4839ee1e4d0ccc` contains ten Parquet files totaling
195,778,779 bytes and a manifest reporting 4,510,074 source rows. The provider
publication timestamp is August 24, 2026; observation dates and license are
unspecified. These rows are not a unique album count or a current completeness
denominator.

The cloud worker reads only release ID, release URL, album title and parent
artist identification columns. It never imports song rows. Album IDs must match
the original Metal-Archives release URL, and duplicates collapse by release ID.
File byte counts and SHA-256 digests are pinned. Progress commits with each
bounded transaction; replay preserves existing records and manual overrides.
All imported albums remain pending direct verification. Unknown dates, formats,
release types and unresolved band identities stay unknown.

The weekly workflow is enabled for Monday 09:17 UTC. It checks the permitted
live sources using the existing request policy, preserves the last good data
when blocked, and reports partial runs. The immutable snapshot itself does not
create a live update feed.
