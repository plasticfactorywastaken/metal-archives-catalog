# Implementation reconciliation

Validated November 26, 2026, America/Chicago. Source probe timestamp: 2026-11-27 02:43:27 UTC.

## Outcome

The deterministic system is implemented and fixture-tested. A native Google Sheets control panel was created and verified. This is **not yet a populated or deployed Metal-Archives mirror**. A public historical bands/labels snapshot was subsequently found and a resumable cloud bootstrap has been implemented and tested with synthetic data; live source access and deployment prerequisites remain unresolved.

| Required status | Observed result |
| --- | --- |
| Real catalog entities imported | **0** |
| Entities remaining unimported | All requested catalog scopes; total unknown |
| Duplicate real stable IDs | 0, because no real entities imported; uniqueness enforced and tested |
| Estimated global coverage | **Unknown**; no reliable denominator obtained |
| Estimated confidence | **Unknown**; no successful catalog verification |
| Weekly GitHub job | Code supplied; **not active**, repository not selected/created |
| GitHub Pages | Static app implemented and browser-tested locally; **not published** |
| Google Sheets | **Connected and created**, 14 native tabs; unattended worker credential missing |
| Billing / paid services | None enabled |

## What was built

- SQLite schema for eight entity types, source observations, per-field old/new history, sync runs, checkpoints, durable queue, genre map, overrides and exception review.
- Exact raw genres and ordered duplicate-preserving tokens, eight visible genre positions and complete JSON lists, classification overlays and protected manual overrides.
- API and public website adapters, strict URL scopes, robots checks, rate limiting/backoff, bounded response cache, and access-block stop behavior.
- Bounded test/batch/full modes, resume queue, isolated-record failures, sampling, stale rechecks, repeated-absence removal and verified reappearance.
- Separate upcoming discovery and upcoming-to-released transitions.
- Atomic partitioned JSON/CSV exports and explicit derived parent genre facets for releases/songs without source genre fields.
- GitHub Actions weekly workflow; corruption-safe, checksummed GitHub Release snapshots with persisted source cooldowns; public-repository and Pages-size safeguards.
- Google Sheets control import/report adapter and a native 14-tab control workbook with filters, frozen headers, validations and conditional review formatting.
- Static filter app with all requested filter controls, sequential worker scanning, pagination, provenance, source/run summaries and bounded matching-CSV export.
- Setup, recovery, classification, data-dictionary and source-limitation documentation.

## What was tested

**117 Python tests passed**, including genre ordering/missing fields/>8 genres, stable-ID uniqueness, hashes, idempotence, field history, soft removal/reappearance, source conflicts, override preservation, partial failure, resume/interruption, upcoming transitions, Google Sheets exports/controls, weekly summaries, queue fairness, snapshot integrity/recovery, historical import limits/checkpoints, existing-record preservation, download truncation/size guards, and persistent historical provenance.

**13 Node frontend tests passed**, including combined filters, ordered genre facets, inherited parent facets without raw mutation, linked entities, CSV escaping, changed-since behavior, bounded worker retention, failed partitions, historical disclosure, label joins, early result previews and cancellation of obsolete searches.

The application was served over local HTTP and checked in a real browser: honest zero-data/unknown-coverage state, filter application/reset, disabled empty export, and readable layout. No real result rows could be verified because catalog access failed. The parent-facet enhancement was subsequently covered by export/frontend tests and a final app reload.

The native workbook import succeeded. All 14 native tabs were visually inspected, classification dropdown tables were verified, filters and frozen headers persisted, and initial source status/unknown coverage values were read back. A date-cell import omission was corrected in the native workbook. Google Sheets export was tested via the connected account; the unattended service-account path is covered by mocks and still requires real authorization.

The live GitHub Actions workflow, Release upload/restore and Pages deployment were **not executed** because no destination repository is available. Their deterministic components were tested locally. API and website parsers are validated against small synthetic fixtures, not successful current live detail responses.

## Exact source results

| Endpoint | Result |
| --- | --- |
| `https://metal-api.dev/bands/1` | Unavailable, Python `URLError`; zero records imported |
| `https://metal-api.dev/swagger/v1/swagger.json` | Research fetch returned HTTP 502; live schema not confirmed |
| `https://www.metal-archives.com/robots.txt` | **HTTP 200** |
| `https://www.metal-archives.com/bands/1` | **HTTP 403**; website probing stopped |

No catalog entity endpoint worked during the initial probe. Later bounded GET requests to the pinned Kaggle bands and labels CSV downloads returned HTTP 206 through ordinary public storage redirects. No full real dataset was imported locally; that remains a cloud job. No proxies, CAPTCHA bypasses, alternate identities, forum requests or private/account requests were used. The machine-readable metadata-only evidence for the original probe is [source-probe.json](source-probe.json); exact bootstrap endpoints are in [SOURCES.md](SOURCES.md).

## Credentials and deployment blockers

1. **Destination public repository:** GitHub account connection is available, but the connector has no repository-creation tool, the workspace has no remote, and the browser is not signed into GitHub. An asynchronous request for a public repository URL was issued. No existing unrelated repository was modified.
2. **Unattended Sheets authorization:** put `GOOGLE_SERVICE_ACCOUNT_JSON` into a repository secret and share the native workbook with that service account as Editor. The Google Sheets API must be enabled. Set repository variable `GOOGLE_SHEET_ID` to the ID below. The connected Drive account is sufficient for this one-time setup but cannot be exported into Actions.
3. **Live source access:** the worker needs permitted working catalog endpoints for current records and weekly changes. A credential is not documented for these public endpoints, and no credential or bypass is proposed to defeat the 403. The newly verified public Kaggle downloads provide a separate historical bootstrap published November 9, 2024; they do not restore live access.
4. **Initial cloud run:** select Pages → GitHub Actions, run **Load historical catalog** in mode `test` with limit `25`, explicitly allow initial empty state once, inspect the result, then resume mode `full` with limit `250000`. This imports bands and labels only and can take more than one run. Enable weekly live sync only when its source behavior is understood.

Native control panel: https://docs.google.com/spreadsheets/d/1tmIM8FK0fBjg9TR2yQHRsZkciEs7nE8L8qab9T4OUGw/edit

Workbook ID: `1tmIM8FK0fBjg9TR2yQHRsZkciEs7nE8L8qab9T4OUGw`. It is stored in the user's ChatGPT Drive folder. Sharing was not broadened.

## Remaining source and scale limits

- Metal-API name searches cap results at 200 and do not enumerate the whole archive or provide a proven modified-since feed.
- Legacy website browse/upcoming shapes and detail parsers could not be validated live after blocking. Unsupported shapes fail visibly.
- Related-entity expansion may miss disconnected artists, labels, releases or public review metadata. Some relationship roles/dates and source update timestamps may be unavailable. Missing song IDs use explicitly marked composites.
- Weekly sampling and rotating rechecks cannot guarantee detection of every change during the same week; freshness must be measured after live runs.
- Global coverage remains unknown even when a separate band denominator becomes available. Confidence is verification share, not proof of completeness.
- Pages publication stops at 900 MB to stay below its 1 GB ceiling. A sufficiently large full catalog can outgrow the static publication allocation. SQLite snapshots still persist; no paid fallback is selected.
- Browser search scans relevant partitions and can be slow across a large catalog. The matching CSV download is capped at 10,000; full data stays in SQLite and partitioned files.
- No wholesale completeness claim is made. Live bounded import, source reconciliation and a successful scheduled run remain outstanding.

See [README](../README.md) for operating instructions and [SOURCES](SOURCES.md) for source details.
