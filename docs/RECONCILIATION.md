# Catalog reconciliation

Checkpoint after completed album import, enrichment and publication on November 27, 2026 UTC (November 26, America/Chicago).

[Open the live application](https://plasticfactorywastaken.github.io/metal-archives-catalog/).

The cloud historical band, label and album imports and publication succeeded. The application contains **888,525 imported records**. Finishing these snapshots does not establish complete or current Metal-Archives coverage.

| Status | Verified result |
| --- | --- |
| Unique records | **888,525**: **692,445 albums**, **182,542 bands** and **13,538 labels** |
| Band CSV | 194,196 rows: 182,542 inserted, 11,125 duplicate/existing, 529 invalid skipped |
| Label CSV | 241,570 rows: 13,538 inserted, 228,032 duplicate/existing |
| Band/label import | Both pinned Kaggle version-3 files reached EOF |
| Album import | All **10** pinned Parquet files processed; **4,510,074** source rows examined; **0** invalid rows |
| Album identity validation | **692,445** unique releases with valid matching Metal-Archives IDs and URLs |
| Source publication dates | Bands/labels: November 9, 2024; albums: August 24, 2026; exact observation dates unknown |
| Direct live verification | **0%** |
| Current global coverage | **Unknown** |
| Unimported scopes | **0 songs**, excluded by user request; artists, relationships, upcoming releases and reviews unavailable |
| Album filter enrichment | Run **45945489756** completed: **692,445** albums processed, **0** remaining and **0** errors |
| Inferred band links | **514,342 albums** linked to **134,612 existing bands** by unique exact-name inference; **178,103 albums** remain unlinked |
| Album year/type | **454,571 albums** have year and type metadata from unique exact band-ID/title matches |
| Weekly sync | **Enabled**; SYNC_ENABLED=true; Monday 09:17 UTC; source availability still limits updates |
| Sheets | Native panel metadata updated; API enabled and workbook shared with the service account; worker credential not yet created and entity report tabs still empty |
| Public site | **363,323,380 bytes** (about **363.3 MB**); **890** gzip entity partitions, within the 900 MB safety guard |
| Paid services or billing | None enabled |

The 529 skipped band rows lacked names or valid stable IDs/source URLs; the album import had no invalid rows. Bounded diagnostics remain in the sync runs. All imported records retain historical provenance and pending verification. Importers discard photo URLs, fetch no images or forum data, and preserve existing entities and manual controls. The album worker projects only album/artist columns and creates no song entities.

Band links inferred from unique exact names are explicitly labeled as inference, not direct verification. Duplicate names or ambiguous discography matches remain unknown. Year/type enrichment uses the pinned Kaggle version-3 discography and does not establish current release metadata.

## Deployment checkpoint

- [Source repository](https://github.com/plasticfactorywastaken/metal-archives-catalog).
- Deployed source commit: `a686ce27a9529f40ff225b7077139e9cb8e2c45e`.
- [Completed album import](https://github.com/plasticfactorywastaken/metal-archives-catalog/actions/runs/45944588602), source commit `cb72e34adcab20989ef68cf3e56d5ef4dbf4f38f`.
- [Completed band/label import](https://github.com/plasticfactorywastaken/metal-archives-catalog/actions/runs/45937168956).
- [Checksummed SQLite snapshot](https://github.com/plasticfactorywastaken/metal-archives-catalog/releases/tag/catalog-state-20261127T053205Z-45945489756-1).
- [Completed album enrichment and publication](https://github.com/plasticfactorywastaken/metal-archives-catalog/actions/runs/45945489756).

Read the newest completed state checkpoint before the next publication. Publishing workflows restore the newest published `catalog-state-*` release; the snapshot linked above must not replace a newer completed run.

Cloud validation exposed a missing lambda parameter when selecting the latest release; it was fixed and a regression test added. Pages also exposed an artifact-visibility delay; publishing workflows now wait for readiness. **Publish saved catalog** rebuilds from persisted state without source requests or reimporting.

## Validation

Synthetic Python and Node tests cover storage, ordered genres, idempotence, overrides, source failures, resume, removals/reappearance, gzip exports, Sheets controls, snapshots, historical imports, artifact readiness, filters and early result cancellation. Album tests also cover projected columns, stable-ID validation, bounded resume, checksums and preserving existing releases.

GitHub Actions ran the code on Ubuntu, restored persisted state, completed all ten album source files and saved the new database snapshot. The subsequent enrichment run processed every imported album without errors. Cloud validation reconciled all 692,445 imported album IDs and URLs and confirmed zero songs. Counts reconcile across 890 gzip entity partitions. A browser verification scanned all **692,445 albums across 693 album partitions**; all **50 visible results** were albums. Browser readers validate stored-byte checksums before decompression and preserve support for earlier plain JSON exports. Complete entity CSV and change-history partitions remain available as gzip downloads.

A subsequent browser country-filter check displayed 50 album rows, all matching United States and all showing the inferred-band-link notice.

The native Sheets Dashboard and Settings metadata were updated with cloud counts, the enabled weekly schedule and outstanding limitations. Dashboard readback and visual verification confirmed the completed counts, schedule and pending worker credential. Manual classification tables and catalog report rows were not changed by this metadata update. Empty report tabs do not mean the cloud catalog is empty.

## Sources and remaining limitations

The pinned Kaggle bands and labels downloads and Hugging Face album files worked in the cloud without credentials. Albums use `fairygaze/music` revision `06856d4d785dce8b029551bcca4839ee1e4d0ccc`. Its publication timestamp is not an observation date, and its license is undeclared. [Exact endpoints and provenance](SOURCES.md).

The original Metal-API band probe failed with a connection error; its Swagger endpoint returned HTTP 502. Metal-Archives robots.txt returned HTTP 200, but the permitted detail probe returned HTTP 403 and further catalog requests stopped. No proxy, alternate identity, CAPTCHA bypass, private data, forum or artwork request was used.

Current additions, changes and missing entity scopes require a working permitted live source. Weekly sampling and rotating checks cannot guarantee every change is detected in the same week. Historical import completion is not live verification.

Google Sheets API is enabled in the unbilled project `onyx-virtue-516005-d6`. The existing workbook is shared as Editor with `metal-archives-sheets-sync@onyx-virtue-516005-d6.iam.gserviceaccount.com`; no project IAM roles were granted. The worker credential has not been created. Unattended reporting still requires the `GOOGLE_SERVICE_ACCOUNT_JSON` repository secret and a successful worker report run. The connected account used for setup is not an Actions credential. Never commit or paste keys into chat.

[Native control panel](https://docs.google.com/spreadsheets/d/1tmIM8FK0fBjg9TR2yQHRsZkciEs7nE8L8qab9T4OUGw/edit). Its reporting tabs are bounded views; SQLite and partitioned files remain the complete imported data layer.

The current site fits the free limit, but growth must be monitored. Oversized exports stop publication while preserving the snapshot. Search scans partitions sequentially and shows early matches while counting. Matching CSV export is capped at 10,000; complete imported data remains in the partitions and SQLite.

