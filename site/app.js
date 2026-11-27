import {scalar, genres, primaryGenre, classificationValue, needsClassification, safeDataURL, historicalSnapshotNotice, albumSnapshotNotice} from './query.mjs';

const $ = id => document.getElementById(id);
const baseURL = new URL('./data/', document.baseURI).href;
const number = value => new Intl.NumberFormat().format(Number(value) || 0);
const date = value => {
  if (!value) return 'Not checked';
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? scalar(value) : parsed.toLocaleString(undefined, {year:'numeric',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
};
const estimate = value => {
  if (value === null || value === undefined || value === '') return 'Unknown';
  if (typeof value === 'number') return `${(value <= 1 ? value * 100 : value).toFixed(1)}%`;
  return scalar(value);
};

let manifest, summary = {}, runs = [], page = 1, total = 0, busy = false, requestId = 0, debounce;
let worker;

function textElement(tag, value, className) {
  const element = document.createElement(tag);
  element.textContent = scalar(value);
  if (className) element.className = className;
  return element;
}

function sourceLink(url, title, className) {
  const element = textElement('a', title, className);
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== 'https:' || !['www.metal-archives.com', 'metal-archives.com'].includes(parsed.hostname) || parsed.pathname.startsWith('/board')) throw new Error('Unsupported source link');
    element.href = parsed.href;
    element.target = '_blank';
    element.rel = 'noopener noreferrer';
  } catch { element.removeAttribute('href'); }
  return element;
}

function showNotice(message) {
  $('notice').textContent = message;
  $('notice').hidden = !message;
}

function empty(title, description) {
  $('empty-title').textContent = title;
  $('empty-description').textContent = description;
  $('empty-state').hidden = false;
  $('table-wrap').hidden = true;
}

function readFilters() {
  const filters = Object.fromEntries(new FormData($('filters')).entries());
  filters.needs_classification = $('filters').elements.needs_classification.checked;
  filters.query = $('search').value.trim();
  return filters;
}

function updatePagination() {
  const pageSize = Number($('page-size').value);
  const pages = Math.max(1, Math.ceil(total / pageSize));
  $('page-label').textContent = busy ? `Page ${number(page)} · scanning` : `Page ${number(page)} of ${number(pages)}`;
  $('previous').disabled = busy || page <= 1;
  $('next').disabled = busy || page >= pages;
  $('export').disabled = busy || !manifest?.total_records || !total;
}

function setBusy(value) {
  busy = value;
  $('scan-progress').hidden = !value;
  $('results').setAttribute('aria-busy', String(value));
  updatePagination();
}

function renderRows(records) {
  const fragment = document.createDocumentFragment();
  for (const record of records) {
    const row = document.createElement('tr');
    const name = document.createElement('td');
    name.append(sourceLink(record.source_url || record.ma_url, record.name || record.title || 'Untitled record', 'record-title'));
    name.append(textElement('span', record.entity_type === 'release' ? 'album / release' : record.entity_type, 'entity-tag'));
    name.append(textElement('span', `${record.entity_type === 'release' ? 'Release ID' : 'ID'} ${record.entity_id}`, 'record-meta'));
    const bandName = record.band_name || record.facet_band_name || record.band;
    const releaseTitle = record.release_title || record.facet_release_title;
    if (bandName && record.entity_type !== 'band') name.append(textElement('span', bandName, 'record-meta'));
    else if (record.band_id || record.facet_band_id) name.append(textElement('span', `Band ID ${record.band_id || record.facet_band_id}`, 'record-meta'));
    if (releaseTitle && record.entity_type === 'song') name.append(textElement('span', releaseTitle, 'record-meta'));
    const genre = document.createElement('td');
    const ordered = genres(record);
    genre.append(textElement('span', primaryGenre(record) || '—', 'genre-primary'));
    if (ordered.length > 1) genre.append(textElement('span', ordered.slice(1).join(' · '), 'secondary-text'));
    if (['band','release'].includes(record.genre_origin)) genre.append(sourceLink(record.genre_source_url, `From ${record.genre_origin} metadata`, 'secondary-text'));
    const classification = document.createElement('td');
    classification.append(textElement('span', classificationValue(record, 'ecommerce_primary_class') || 'Unclassified', `class-tag${needsClassification(record) ? ' needs-review' : ''}`));
    classification.append(textElement('span', scalar(classificationValue(record, 'classification_status')).replaceAll('_', ' ') || 'Needs classification', 'secondary-text'));
    const details = document.createElement('td');
    const place = [record.country || record.facet_country, record.location || record.facet_location].filter(Boolean).join(' · ');
    details.append(textElement('span', place || '—'));
    if (record.label_name || record.facet_label_name) details.append(textElement('span', record.label_name || record.facet_label_name, 'secondary-text'));
    if (record.release_date || record.expected_release_date) details.append(textElement('span', record.release_date || record.expected_release_date, 'secondary-text'));
    else if (record.release_year) details.append(textElement('span', `Year ${record.release_year}`, 'secondary-text'));
    if (record.release_type) details.append(textElement('span', record.release_type, 'secondary-text'));
    const verification = document.createElement('td');
    verification.append(textElement('span', scalar(record.verification_status).replaceAll('_', ' ') || 'Unverified', 'verification'));
    verification.append(textElement('span', date(record.last_checked), 'secondary-text'));
    if (record.active_status && record.active_status !== 'active') verification.append(textElement('span', record.active_status, 'secondary-text needs-review'));
    row.append(name, genre, classification, details, verification);
    fragment.append(row);
  }
  $('result-rows').replaceChildren(fragment);
  $('table-wrap').hidden = !records.length;
  $('empty-state').hidden = Boolean(records.length);
}

function startSearch({resetPage = true, action = 'search'} = {}) {
  clearTimeout(debounce);
  if (!manifest || !worker) return;
  if (resetPage) page = 1;
  const filters = readFilters();
  const label = $('filters').elements.entity_type.selectedOptions[0].textContent;
  $('results-title').textContent = filters.entity_type ? label : 'All records';
  if (action === 'search') {
    total = 0;
    $('result-count').textContent = 'Scanning…';
    $('result-rows').replaceChildren();
    empty('Searching the catalog…', 'Matches appear as each partition is read. The count remains partial until the scan finishes.');
  }
  setBusy(true);
  $('scan-label').textContent = action === 'export' ? 'Preparing CSV export…' : 'Scanning catalog partitions…';
  $('scan-progress').value = 0;
  $('scan-progress').max = manifest.partitions.length || 1;
  const id = ++requestId;
  // Recreate the worker to cancel an in-flight network request promptly.
  worker.terminate();
  worker = createWorker();
  worker.postMessage({requestId:id, action, manifest, filters, baseURL, page, pageSize:Number($('page-size').value), excludedEntityTypes:['song']});
}

function createWorker() {
  const current = new Worker(new URL('./worker.js', import.meta.url), {type:'module'});
  current.addEventListener('message', ({data}) => {
    if (data.requestId !== requestId) return;
    if (data.kind === 'progress') {
      $('scan-progress').max = data.partitions || 1;
      $('scan-progress').value = data.completed;
      $('scan-label').textContent = `${number(data.scanned)} records scanned · ${number(data.completed)} / ${number(data.partitions)} partitions${data.failures ? ` · ${data.failures} unavailable` : ''}`;
      if (data.action !== 'export') {
        total = data.total;
        $('result-count').textContent = `${number(total)} ${total === 1 ? 'match' : 'matches'} so far · scanning${data.failures ? ' available partitions' : ''}…`;
      }
    } else if (data.kind === 'preview') {
      total = data.total;
      renderRows(data.rows);
      // Keep pagination/export disabled until the complete count is known.
      // Source links in the first matching page are already usable.
      updatePagination();
    } else if (data.kind === 'result') {
      total = data.total;
      setBusy(false);
      renderRows(data.rows);
      $('result-count').textContent = `${number(total)} ${total === 1 ? 'match' : 'matches'}${data.failures ? ' in available partitions' : ''}`;
      if (!data.scanned) $('scan-label').textContent = manifest.total_records ? 'No data partitions could be searched.' : 'No catalog records imported yet.';
      if (!data.rows.length) {
        if (!manifest.total_records) empty('The catalog is waiting.', 'The application is ready. A successful initial import is needed before real catalog records can appear.');
        else if (data.failures) empty('Some data is unavailable.', 'This search could not read all data partitions. Try again after the next successful publication.');
        else empty('No matching records.', 'Try removing a filter or searching a broader name or genre. Missing metadata is not treated as a match.');
      }
      if (data.failures) showNotice(`${data.failures} data partition(s) could not be read. These results are partial; no records have been deleted.`);
    } else if (data.kind === 'export') {
      setBusy(false);
      const blob = new Blob(['\ufeff', data.csv], {type:'text/csv;charset=utf-8'});
      const link = document.createElement('a');
      link.href = URL.createObjectURL(blob);
      link.download = `metal-catalog-${new Date().toISOString().slice(0, 10)}.csv`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(link.href), 1000);
      $('scan-label').textContent = `Exported ${number(data.exported)} of ${number(data.total)} matches${data.capped ? ' · 10,000-row limit reached' : ''}${data.failures ? ' · partial data' : ''}.`;
    } else if (data.kind === 'error') {
      setBusy(false);
      $('result-count').textContent = `${number(total)} matches in partial search`;
      showNotice(`Search could not finish: ${data.error}`);
    }
  });
  current.addEventListener('error', () => {
    if (current !== worker) return;
    setBusy(false);
    showNotice('The search worker could not start. Serve this application over HTTP or HTTPS and use a browser that supports JavaScript module workers.');
  });
  return current;
}

async function loadJSON(path) {
  const response = await fetch(safeDataURL(path, baseURL), {cache:'no-cache'});
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

function renderSummary() {
  const latest = summary.latest_run || runs[0] || {};
  const totalRecords = summary.total_records ?? manifest.total_records;
  $('total-records').textContent = number(totalRecords);
  $('processed-label').textContent = `${number(summary.records_processed ?? totalRecords)} records processed`;
  $('coverage').textContent = estimate(summary.estimated_coverage ?? latest.estimated_coverage);
  $('confidence').textContent = estimate(summary.verification_ratio ?? summary.estimated_confidence ?? latest.estimated_confidence);
  $('confidence-note').textContent = 'Directly verified share of imported records';
  $('review-items').textContent = number(summary.review_items ?? latest.review_items);
  $('last-sync').textContent = latest.finished_at ? date(latest.finished_at) : latest.started_at ? date(latest.started_at) : 'Not run yet';
  const latestStatus = latest.status ? scalar(latest.status).replaceAll('_', ' ') : 'No synchronization recorded';
  $('sync-status').textContent = latest.import_kind === 'historical_snapshot' ? `Historical import · ${latestStatus}` : latestStatus;
  $('run-id').textContent = latest.run_id || 'No synchronization recorded';
  $('api-status').textContent = latest.api_status || 'Not checked';
  $('website-status').textContent = latest.website_status || 'Not checked';
  $('remaining').textContent = summary.records_remaining == null ? 'Unknown' : number(summary.records_remaining);
  const counters = [
    ['records_checked','Checked'],['new_records','New','records_inserted'],['updated_records','Updated','records_updated'],
    ['removed_records','Removed','records_removed'],['reappeared_records','Reappeared','records_reappeared'],['genre_changes','Genre changes'],
    ['primary_genre_changes','Primary genre changes'],['label_changes','Label changes'],
    ['new_upcoming_releases','New upcoming'],['upcoming_now_released','Now released'],
    ['api_failures','API failures'],['website_failures','Website failures'],['conflicts','Conflicts'],['review_items','Review items'],
  ];
  const counts = summary.weekly || latest.summary || latest;
  const fragment = document.createDocumentFragment();
  for (const [key, label, alias] of counters) {
    const element = textElement('span', label, 'summary-item');
    element.append(textElement('strong', number(counts[key] ?? latest[key] ?? latest[alias])));
    fragment.append(element);
  }
  $('summary-counts').replaceChildren(fragment);
  const checkpoint = summary.checkpoint || latest.checkpoint;
  if (checkpoint && !(typeof checkpoint === 'object' && !Object.keys(checkpoint).length)) {
    $('checkpoint').textContent = `Checkpoint: ${typeof checkpoint === 'object' ? JSON.stringify(checkpoint) : checkpoint}`;
    $('checkpoint').hidden = false;
  }
  if (latest.status && ['partial','failed','blocked'].includes(latest.status)) showNotice(`Latest synchronization is ${latest.status}. Previously imported data is retained. ${latest.error_summary || 'See source status and review items for details.'}`);
  if (!totalRecords) showNotice('No live catalog has been imported. Coverage and confidence remain unknown until source discovery and verification succeed.');
  for (const run of runs) {
    if (!run.started_at || !run.run_id) continue;
    const option = document.createElement('option');
    option.value = run.started_at;
    option.textContent = `${date(run.started_at)} · ${run.run_id}`;
    $('changed-since').append(option);
  }
  const download = manifest.downloads?.sqlite || manifest.downloads?.database || manifest.downloads?.snapshot;
  if (typeof download === 'string') {
    try { $('dataset-link').href = safeDataURL(download, baseURL); $('dataset-link').hidden = false; } catch { /* don't expose unsupported URLs */ }
  }
}

function renderHistoricalNotice() {
  for (const [prefix, notice] of [
    ['historical', historicalSnapshotNotice(manifest.historical_snapshot || summary.historical_snapshot)],
    ['album', albumSnapshotNotice(manifest.album_snapshot || summary.album_snapshot)],
  ]) {
    if (!notice) continue;
    $(`${prefix}-notice`).hidden = false;
    $(`${prefix}-title`).textContent = notice.title;
    $(`${prefix}-description`).textContent = notice.description;
    $(`${prefix}-progress`).textContent = notice.progress;
    if (notice.sourceURL) {
      $(`${prefix}-source`).href = notice.sourceURL;
      $(`${prefix}-source`).hidden = false;
    }
  }
}

function renderChanges(changes) {
  const entries = Array.isArray(changes) ? changes : changes.changes || [];
  const fragment = document.createDocumentFragment();
  for (const change of entries.slice(0, 100)) {
    const row = document.createElement('tr');
    const id = textElement('td', date(change.detected_at));
    id.append(textElement('span', `${change.entity_type} · ${change.entity_id}`, 'secondary-text'));
    const field = textElement('td', change.change_type);
    field.append(textElement('span', change.changed_field || scalar(change.changed_fields), 'secondary-text'));
    const previous = textElement('td', typeof change.previous_value === 'object' && change.previous_value !== null ? JSON.stringify(change.previous_value) : change.previous_value ?? '—');
    const next = textElement('td', typeof change.new_value === 'object' && change.new_value !== null ? JSON.stringify(change.new_value) : change.new_value ?? '—');
    const source = document.createElement('td');
    source.append(sourceLink(change.source_url, 'Original record'));
    row.append(id, field, previous, next, source);
    fragment.append(row);
  }
  $('change-rows').replaceChildren(fragment);
  $('changes-note').textContent = entries.length ? `Showing ${Math.min(entries.length, 100)} recent field changes. Full history is retained in the SQLite snapshot and change exports.` : 'No changes recorded.';
}

$('filters').addEventListener('submit', event => { event.preventDefault(); startSearch(); });
$('filters').addEventListener('input', () => { clearTimeout(debounce); debounce = setTimeout(() => startSearch(), 500); });
$('search').addEventListener('input', () => { clearTimeout(debounce); debounce = setTimeout(() => startSearch(), 400); });
$('reset').addEventListener('click', () => { $('filters').reset(); $('search').value = ''; startSearch(); });
$('previous').addEventListener('click', () => { page--; startSearch({resetPage:false}); });
$('next').addEventListener('click', () => { page++; startSearch({resetPage:false}); });
$('page-size').addEventListener('change', () => startSearch());
$('export').addEventListener('click', () => startSearch({resetPage:false, action:'export'}));
document.addEventListener('keydown', event => { if (event.key === '/' && !['INPUT','SELECT','TEXTAREA'].includes(document.activeElement.tagName)) { event.preventDefault(); $('search').focus(); } });

try {
  manifest = await loadJSON('manifest.json');
  if (!Array.isArray(manifest.partitions)) throw new Error('Invalid catalog manifest');
  const results = await Promise.allSettled([
    typeof manifest.summary === 'string' ? loadJSON(manifest.summary) : Promise.resolve(manifest.summary || {}),
    typeof manifest.runs === 'string' ? loadJSON(manifest.runs) : Promise.resolve(manifest.runs || []),
    typeof manifest.changes === 'string' ? loadJSON(manifest.changes) : Promise.resolve([]),
  ]);
  summary = results[0].status === 'fulfilled' ? results[0].value : {};
  runs = results[1].status === 'fulfilled' && Array.isArray(results[1].value) ? results[1].value : [];
  renderSummary();
  renderHistoricalNotice();
  renderChanges(results[2].status === 'fulfilled' ? results[2].value : []);
  if (results.some(result => result.status === 'rejected')) showNotice('Catalog data loaded, but some synchronization reports are unavailable. Source and coverage information may be incomplete.');
  worker = createWorker();
  startSearch();
} catch (error) {
  showNotice(`Catalog data is not available yet (${error.message}). Publish an export from a successful synchronization to enable search.`);
  $('result-count').textContent = 'No data published';
  $('scan-label').textContent = 'Awaiting first data export';
  empty('The catalog is waiting.', 'The filter application is ready. Run a bounded test import and publish the generated data directory to begin.');
}
