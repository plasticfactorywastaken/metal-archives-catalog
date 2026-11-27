# Catalog reconciliation

Validated November 26, 2026, America/Chicago.

[Open the live application](https://plasticfactorywastaken.github.io/metal-archives-catalog/).

The cloud historical import and publication succeeded. This is a usable historical bands/labels catalog, not a complete current Metal-Archives mirror.

| Status | Verified result |
| --- | --- |
| Unique records | **196,080**: **182,542 bands** and **13,538 labels** |
| Band CSV | 194,196 rows: 182,542 inserted, 11,125 duplicate/existing, 529 invalid skipped |
| Label CSV | 241,570 rows: 13,538 inserted, 228,032 duplicate/existing |
| Historical import | Both pinned version-3 files reached EOF |
| Snapshot published | November 9, 2024; exact observation dates unknown |
| Direct live verification | **0%** |
| Current global coverage | **Unknown** |
| Unimported scopes | Releases, songs, artists, relationships, upcoming releases and reviews |
| Weekly sync | **Inactive**; SYNC_ENABLED not enabled |
| Sheets | Native panel connected; metadata updated; unattended worker credential missing and entity report tabs empty |
| Public site | About **802 MB**, within the 900 MB safety guard |
| Paid services or billing | None enabled |

Invalid rows lacked names or valid stable IDs/source URLs. Bounded diagnostics remain in the sync run. All imported records retain historical provenance and pending verification. The importer discards photo URLs, fetches no images or forum data, and preserves existing entities and manual controls. Finishing the files does not establish source-wide completeness.

## Deployment checkpoint

- [Source repository](https://github.com/plasticfactorywastaken/metal-archives-catalog).
- Deployed source commit: `b14315366b03e523973c96bf6c6f39088d5ebab3`.
- [Successful publication](https://github.com/plasticfactorywastaken/metal-archives-catalog/actions/runs/45937987392).
- [Completed import](https://github.com/plasticfactorywastaken/metal-archives-catalog/actions/runs/45937168956): import and state persistence succeeded; publication was recovered separately.
- [Checksummed SQLite snapshot](https://github.com/plasticfactorywastaken/metal-archives-catalog/releases/tag/catalog-state-20261127T035922Z-45937168956-1).

Cloud validation exposed a missing lambda parameter when selecting the latest release; it was fixed and a regression test added. Pages also exposed an artifact-visibility delay; publishing workflows now wait for readiness. **Publish saved catalog** rebuilds from persisted state without source requests or reimporting.

## Validation

**125 Python tests and 13 Node tests** cover storage, ordered genres, idempotence, overrides, source failures, resume, removals/reappearance, exports, Sheets controls, snapshots, historical import, artifact readiness, filters and early result cancellation.

GitHub Actions ran the code on Ubuntu, restored the first 25-record checkpoint, imported the remaining historical rows and saved the new database snapshot. The public HTML and five JavaScript/CSS assets loaded successfully. Counts reconcile across 197 partitions. A 1,000-record public band partition passed its SHA-256 check and deterministic country/genre/entity-type filtering checks without printing records.

The native Sheets Dashboard and Settings were updated and read back with cloud counts, live URL, historical date, skipped-row count and outstanding limitations. Manual classification tables and catalog report rows were not changed by this metadata update.

## Sources and remaining limitations

The pinned Kaggle bands and labels downloads worked in the cloud without credentials. [Exact endpoints and provenance](SOURCES.md).

The original Metal-API band probe failed with a connection error; its Swagger endpoint returned HTTP 502. Metal-Archives robots.txt returned HTTP 200, but the permitted detail probe returned HTTP 403 and further catalog requests stopped. No proxy, alternate identity, CAPTCHA bypass, private data, forum or artwork request was used.

Current additions, changes and missing entity scopes require a working permitted live source. Weekly sampling and rotating checks cannot guarantee every change is detected in the same week. Historical import completion is not live verification.

Unattended Sheets reporting requires the GOOGLE_SERVICE_ACCOUNT_JSON repository secret, the Sheets API enabled without billing, and sharing the existing workbook with that service account as Editor. The connected account used for setup is not an Actions credential. Never commit or paste keys into chat.

[Native control panel](https://docs.google.com/spreadsheets/d/1tmIM8FK0fBjg9TR2yQHRsZkciEs7nE8L8qab9T4OUGw/edit). Its reporting tabs are bounded views; SQLite and partitioned files remain the complete imported data layer.

The current site fits the free limit, but growth must be monitored. Oversized exports stop publication while preserving the snapshot. Search scans partitions sequentially and shows early matches while counting. Matching CSV export is capped at 10,000; complete imported data remains in the partitions and SQLite.

