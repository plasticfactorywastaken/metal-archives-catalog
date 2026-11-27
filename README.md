# Metal-Archives catalog mirror

A deterministic Python worker, SQLite catalog, partitioned JSON/CSV exports, Google Sheets control panel, and static GitHub Pages search application. Routine jobs use no LLM and require no running personal computer.

**Live application:** [Open the catalog](https://plasticfactorywastaken.github.io/metal-archives-catalog/). The historical bootstrap is complete: **182,542 unique bands and 13,538 unique labels** are searchable. These are unverified records from a snapshot published November 9, 2024. Global current coverage is unknown; direct verification is 0%. The live API and Metal-Archives access remain unavailable, weekly synchronization is inactive, and unattended Google Sheets reporting still needs its worker credential. See [RECONCILIATION.md](docs/RECONCILIATION.md) for validation and limitations.

To retry publication after a hosting failure, run **Publish saved catalog**. It restores the existing checksummed snapshot and rebuilds the site without importing or querying catalog sources. Publication waits for the uploaded Pages artifact to become visible before deployment.

## First usable catalog

After creating the public repository and enabling Pages with GitHub Actions, run **Load historical catalog** with mode `test`, limit `25`, and `allow_initial_import=true` only for the first run. This downloads two pinned public CSV files from Kaggle in the cloud and publishes a small searchable sample. It makes no requests to Metal-Archives or the unavailable API. Google Sheets credentials can be added later; missing credentials are reported and do not prevent publication.

After checking that run and the Pages link, repeat with mode `full`, limit `250000`, and `allow_initial_import=false`. Repeat if the checkpoint is unfinished. The importer preserves committed progress, deduplicates stable IDs, and skips every existing entity so historical values cannot overwrite newer records or manual controls. It imports bands and labels only. The provider reports about 194,000 bands, but the actual imported count is measured by the worker.

Filter these records by name, country, band status, ordered genres and label. Data is prominently labeled as a historical snapshot published November 9, 2024; direct verification is pending and last-checked dates remain empty. Releases, songs, artists, upcoming releases and live weekly changes still require a working permitted source. Source freshness and global completeness are not established by finishing this bootstrap.

Source: [Every Metal Archives Band November 2024, version 3](https://www.kaggle.com/datasets/guimacrlh/every-metal-archives-band-october-2024). The provider labels the dataset Apache 2.0. Image URLs are discarded and no image files are fetched. See [source notes](docs/SOURCES.md) for pinned endpoints and limitations.

## Architecture

```text
Metal-API discovery ─┐
                    ├─ bounded Python worker ─ SQLite + history + durable queue
Permitted MA pages ─┘             │                      │
                                 │                      ├─ checksummed GitHub Release snapshots
Google Sheets manual controls ────┤                      └─ partitioned JSON + CSV
                                 └─ bounded Sheets reports       │
                                                        GitHub Pages filters
```

The public repository contains code and workflows. Published GitHub Releases hold compressed, checksummed SQLite snapshots split into 64 MiB assets. SQLite contains the source observations, canonical entities, every change, manual rules, and discovery checkpoints. Releases avoid accumulating giant database binaries in Git history and are the authoritative resume mechanism. A draft snapshot becomes published only after all assets upload successfully. No Actions cache is required for recovery. Older published snapshots are retained.

The website scans data partitions sequentially in a Web Worker, retaining a page of matches. It does not load the entire catalog into model context or the UI's memory. Broad queries still require downloading many partitions; this is the tradeoff for static free hosting. CSV export from the browser is explicitly capped at 10,000 matches. Complete exports remain available as CSV partitions and a downloadable SQLite snapshot.

## Free-service assumptions and hard boundaries

- Use a **public GitHub repository** and standard `ubuntu-latest` runners. Both workflows refuse private repositories. GitHub documents standard public-repository runners as free: [Pages setup](https://docs.github.com/en/pages/getting-started-with-github-pages/creating-a-github-pages-site).
- GitHub Pages is limited to 1 GB and a soft 100 GB/month bandwidth limit. This project stops publication at 900 MB, retains the previous live site, and still saves SQLite state. A full catalog that exceeds the cap requires a smaller explicitly selected publication scope or another verified free data host; no paid fallback is enabled. This catalog browser does not facilitate commercial transactions. See [Pages limits](https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits).
- Release assets must each be below 2 GiB. The 64 MiB parts are comfortably below this. See [GitHub releases](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases).
- Google Sheets API usage is available at no additional cost under its published quotas. The bounded control panel avoids the spreadsheet cell ceiling and does not request paid quota expansion: [Sheets limits](https://developers.google.com/workspace/sheets/api/limits).
- No billing account, credit card, paid API, proxy network, CAPTCHA solver, or LLM credential is needed. No automatic service upgrades occur.
- No forum, forum user, private account, login-only data, lyrics, artwork, or image files are collected. The worker is read-only toward Metal-Archives.

## Cloud-only setup

1. Create a public repository, for example `metal-archives-catalog`, and upload the contents of this project. Keep `.github/workflows` intact. Do not upload local credentials or the `state` directory.
2. Open repository **Settings → Pages** and choose **GitHub Actions** as the source. Ensure Actions is enabled. The workflow uses the repository's automatic `GITHUB_TOKEN`; no personal access token is necessary at runtime.
3. To enable unattended Google Sheets updates, create a Google Cloud project and enable the Google Sheets API without enabling billing. Create a service account or use your organization's existing approved service account. A service-account key is a secret; never commit it or paste it into chat. Steps 3–5 may be deferred until after the first Pages publication.
4. Open the supplied native control workbook and share it with the service account's `client_email` as Editor. The connected Drive account used during setup does **not** supply unattended Actions credentials.
5. Under repository **Settings → Secrets and variables → Actions**, add secret `GOOGLE_SERVICE_ACCOUNT_JSON` containing the service-account JSON. Add variable `GOOGLE_SHEET_ID` containing the workbook ID, not its complete URL.
6. Set a useful operator/contact description in `config/settings.json` → `user_agent`. Keep the honest deterministic-worker identity. Changing identity to get around a block is prohibited.
7. Run **Load historical catalog → Run workflow**, mode `test`, record limit `25`, and `allow_initial_import=true` for the first run only. Inspect the published Pages app, then repeat mode `full` with limit `250000` and initial import disabled until the snapshot checkpoint is complete. This historical import does not probe the blocked live sources.
8. To test permitted live updates later, run **Weekly catalog synchronization** in mode `test` with limit `5` and initial import disabled. Inspect Sync Runs, source status, pending jobs, and the change summary. Enable scheduled work only after the result is understood by setting repository variable `SYNC_ENABLED=true`.

Missing credentials never enable a paid substitute. A missing or failed Sheets connection leaves manual rules in SQLite intact and exposes the failed status. An unreadable snapshot stops the job; it must never trigger a fresh empty backfill silently.

## Schedule and disabling the job

The weekly cron is `17 9 * * 1`: Monday at 09:17 UTC, which is 03:17 in Chicago during standard time and 04:17 during daylight time. GitHub scheduling can be delayed and may disable schedules in inactive public repositories; check the Actions page. Edit the cron in `.github/workflows/sync.yml` to change it.

To stop scheduled sync, set `SYNC_ENABLED=false` or disable the workflow in GitHub Actions. Manual runs remain possible with the variable false. All initial code is delivered with scheduled processing gated off. There is no Codex automation and no dependency on a chat staying open.

## Import modes and resume

Every run is finite and creates a unique run ID. `test` defaults to 5 detail jobs. `batch` processes a fixed limit, default 250. `full` continues discovery and backfill within the same job's record/time budget; it resumes in later jobs. It does not mean an unbounded scrape or an automatically complete catalog.

Run the workflow with mode `batch` and an appropriate limit after the test passes. For backfill use mode `full`, repeat as needed, and keep the same persisted snapshot chain. The limit counts detail queue jobs; an entity can require API, verification, and public discography requests and may yield several embedded entities. Every HTTP request is still rate limited.

The queue is durable. A discovery cursor advances only after its records have been queued. A detail job is removed only after successful direct verification or an explicit source absence check. Failed jobs remain pending. An interruption between an upsert and queue acknowledgement causes replay, and content hashes prevent duplicate changes. Each source and upcoming feed has a separate cursor. Finished cycles start a subsequent reconciliation pass; unfinished cycles retain their position.

On GitHub, restore is automatic from the newest **published** `catalog-state-*` release. Missing/corrupt assets fail closed. To recover a past snapshot, download all its parts and manifest and use `scripts/cloud_state.py`'s `unpack` function, or publish a verified recovery snapshot through the same tool. Do not remove the `state/catalog.sqlite` file as a routine recovery step.

At 8 seconds per request, a highly complete backfill can take many jobs. The API provides capped searches, not a bulk dump. Website enumeration and embedded entities remain subject to source availability and parser support. Increasing throughput to defeat source restrictions is not supported.

## Development commands (optional)

These commands are for initial implementation/debugging. Routine operation uses cloud Actions.

```sh
python -m pip install '.[sheets]'
python -m catalog_mirror init
python -m catalog_mirror sync --mode test --limit 5
python -m catalog_mirror sync --mode batch --limit 250
python -m catalog_mirror sync --mode full --limit 1000 --seconds 7200
python -m catalog_mirror summary
python -m catalog_mirror export --output site/data
python -m unittest discover -s tests -v
node --test tests/frontend.test.mjs
python -m http.server 8000 --directory site
```

Use `--db PATH` before the subcommand to select an independent development database. `--skip-sheets` deliberately omits remote reporting for local tests. Fixtures use invented records in temporary databases and never enter production exports.

## Data dictionary

The canonical `entities` table has primary key `(entity_type, entity_id)`, with a JSON record plus common indexed metadata. Typed SQL views expose the named catalog fields. Numeric-looking source IDs are stored as strings. Synthetic relationship IDs and missing song IDs are explicitly marked as composites; they are not represented as genuine Metal-Archives IDs.

| Table/view | Meaning and important fields |
| --- | --- |
| `entities` | Stable type/ID, name/title, source URL/system, `data_json`, first/last seen, last checked, source update time, SHA-256 content hash, active/missing/removed status, verification status, raw source reference, last changed time/run |
| `bands` | `ma_band_id`, name, aliases, country/location, source status, formed year, ordered genres, themes, official URL, original MA URL |
| `releases` | `ma_release_id`, band ID, title, release type/date/year, label ID/name, formats, genre data if supplied, upcoming flag |
| `songs` | Source or explicitly composite ID, release/band IDs, track number, title, duration, parent source URL |
| `labels` | ID, name, country/status, parent/sub-label fields when available |
| `artists` | ID, name, aliases/status when available |
| `relationships` | Stable relationship key, source and target type/ID, relationship type, role, start/end dates, source URL |
| `upcoming_releases` | Release ID, band, title, announced/expected date when available, label/status, provenance |
| `review_metadata` | Review ID, title, release ID, rating/date when discoverable; no review bodies or forum data |
| `source_observations` | Last observation by API/website, source-specific content/hash/check time/run and verification |
| `changes` | One changed field per row, run, timestamp, type/ID/name, change type, old/new JSON values, source URL, review status |
| `sync_runs` | Run status, availability, counters, errors, coverage/confidence, checkpoint, plus extensible `metrics_json` |
| `work_queue` | Durable pending detail jobs, reason/priority/attempts and last attempted run |
| `checkpoints` | Per-source discovery positions, partition totals and resume state |
| `genre_map` | Editable exact genre mapping, e-commerce class/tags, confidence, review state, notes, taxonomy version/update |
| `classification_overrides` | Keyed overlay: entity type/ID/field, value, reason, reviewer and timestamp |
| `review_queue` | Deduplicated unresolved exceptions with evidence JSON and original URL |

Source update timestamps remain null when unavailable. A successful source observation updates last seen/checked; failures do not pretend that a record was seen. Raw references hold endpoint and response digest, not artwork or private content. Missing fields on an incomplete response preserve earlier values.

## Genre classification and overrides

`genres_raw` preserves the source text. Ordered tokens are stored in `genres_json`; no sorting or silent deduplication occurs. Separators outside parentheses split genre tokens. `primary_genre` is token 1, then `genre_2` through `genre_8`. All additional tokens remain in `genres_json`; `genre_count` and `secondary_genres` preserve them.

The default product class comes from the primary token. Secondary genres are searchable tags. Editable `Genre Map` rules supply `ecommerce_primary_class`, `ecommerce_secondary_tags`, classification status/confidence, and taxonomy version without modifying raw genre data. Ambiguous descriptions remain visible for review. Parenthetical qualifiers are preserved rather than guessed into a taxonomy.

Releases and songs often have no source genre field. Exports then provide explicitly named `facet_*` values inherited from a known parent release or single band, with `genre_origin` metadata. The UI labels this provenance. The original `genres_raw`, `genres_json`, and source hash remain unchanged. Multi-band or missing parents are left unknown. Entity-specific classification overrides remain authoritative.

In **Genre Map**, enter the exact original token, desired e-commerce classification, semicolon-separated secondary tags, confidence from 0 to 1, review status, notes, and taxonomy version. The provided Heavy Metal row is a starter rule awaiting review, not a catalog record. Update its review status after checking it.

In **Classification Overrides**, enter type, stable source ID, field, override value, reason, reviewer and timestamp. Allowed fields are `ecommerce_primary_class`, `ecommerce_secondary_tags`, `classification_status`, and `classification_confidence`. Tags are separated by semicolons. Raw genre fields cannot be overridden. The template row with an empty ID is inactive. Importing fresh source data cannot erase manual overrides.

Deleting a row from a temporarily incomplete Sheet does not delete a cached override. To intentionally remove one, use `Store.delete_override(type,id,field)` against the persisted database and remove the row from Sheets before the next run. This explicit action prevents accidental mass loss after a partial Sheet read.

## Sheets control panel

Tabs: Dashboard, Bands, Releases, Songs, Labels, Artists, Relationships, Upcoming, Changes, Sync Runs, Genre Map, Classification Overrides, Review Queue, Settings.

The entity and reporting tabs are bounded previews, default 1,000 rows each. They are not complete catalog storage. All entity records and change history remain in SQLite and the partitioned exports; the Dashboard states this limit. No automatic multi-workbook expansion is needed because the complete catalog never depends on Sheets. Manual control reads have a separate 10,000-row guard; exceeding it stops control replacement and retains cached rules.

The worker reads manual controls before it writes report tabs. It never replaces Genre Map, Classification Overrides, or Settings. Review resolution status is read by stable `review_id` before refreshing Review Queue. Classification inputs have validation; headers are frozen, report tables filtered, unresolved reviews highlighted, and original URLs available. Settings permits changing `sheet_row_limit` (1–10,000); other displayed settings document values configured in the repository and are not silently applied from cells.

## Sources and detection limits

The API adapter supports public band/album detail and bounded band-name searches. The API's documentation says searches are limited to 200 results. API search exhaustiveness is therefore never assumed. The website adapter performs alphabetic public browse discovery, detail verification, band discography expansion, and upcoming-release discovery when permitted. Artists, labels, songs, relationships, and review metadata expand from reliably parsed public details. Disconnected entities not reachable from those paths can remain unimported.

New API-derived records receive direct verification attempts. Unverifiable records are marked pending and remain in the retry queue; a failed request is not a verified record. Same-run API/website conflicts are surfaced for review with website precedence. A random bounded sample and a rotating stale-record queue recheck known records. No reliable global modified-since feed was established, so changes may be discovered on a later rescan. This does not guarantee that every ordinary weekly source update is detected that week.

Removal requires repeated confirmed 404/410 evidence on separate runs (three by default), or an explicit strong-evidence call. The first absence is soft `missing`. Timeouts, 403s, 429s, parse errors and source outages never become removal evidence. Reappearance restores active status. Absence of an item in a discovery page does not remove it.

See [SOURCES.md](docs/SOURCES.md) for exact endpoints, probe results, parser limits, and fields not yet validated on live responses.

## Rate limits and failures

Defaults are 8 seconds between requests to the same source, 25-second timeout, and at most 2 retry attempts. The transport rejects request intervals below 5 seconds. It honors robots rules, Retry-After, cache limits, response-size/type limits, and strict public URL scopes. A 403, CAPTCHA, or access restriction stops that host. Host cooldown expirations are checksummed alongside the SQLite snapshot and survive a new cloud runner. No browser impersonation, proxy rotation, account login, or alternate identity is attempted.

An isolated parse failure remains queued and appears in run diagnostics. A partial run preserves existing catalog records, updates confirmed records, and publishes honest status. It never bulk-replaces the database with an empty result. Export is staged and swapped only after success. A failed publication leaves the existing Pages site in place. Snapshots persist state even if the export size guard fails.

If a source is blocked, keep the last good data and investigate permitted access; do not try evasive options. If Sheets is unauthorized, share the existing workbook with the configured service account and verify the ID and API enablement. If a parser changes, add a small sanitized fixture, fix the deterministic parser, run the tests, and retry the durable queue.

## Coverage and confidence

Global coverage is **unknown** until a trustworthy global denominator and reconciliation exist. The API search limit cannot supply one. When all website alphabetical band partitions report totals, the worker can report a separate band coverage ratio: imported bands / reported discoverable bands, capped at 1. This remains an estimate because an index can change during a long crawl, and it does not measure song/artist/label completeness.

Confidence is a transparent verification ratio: directly verified imported entities / imported entities. Empty catalogs report unknown, not 100%. Embedded entities verified through their parent page count as directly sourced. This ratio is not a statistical correctness probability or proof of global coverage. Source failures, unresolved conflicts, pending jobs, truncated API queries, and incomplete child discovery are reported separately so a high ratio cannot hide missing catalog scope.

## Exports and independent manipulation

`python -m catalog_mirror export` creates `manifest.json`, bounded entity JSON and CSV partitions, changes/run/review summaries, and provenance metadata under `site/data`. CSV export escapes spreadsheet formula prefixes; JSON retains exact source strings. Each partition carries a SHA-256 digest. Use `Store.backup(path)` for a consistent standalone SQLite file, including WAL-committed data.

Download the newest `catalog-state-*` Release's `snapshot-manifest.json` and every `.partNNNN` asset. The `unpack(directory, output_database)` helper in `scripts/cloud_state.py` validates hashes and SQLite integrity before replacing the target. You can then query the named SQL views in any SQLite client or export partitions independently. No LLM is involved.

Code changes, exceptions, ambiguous taxonomy, and failed runs can be brought back to Codex. Normal parsing, hashing, deduplication, filtering, and weekly updates are ordinary deterministic code.
