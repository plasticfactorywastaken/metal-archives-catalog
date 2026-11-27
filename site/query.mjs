// Shared by the browser worker and deterministic tests. No runtime dependencies.
export function scalar(value) {
  if (value == null) return '';
  return Array.isArray(value) ? value.join(' · ') : String(value);
}

const normalized = value => scalar(value).toLocaleLowerCase().trim();
const includes = (value, query) => !query || normalized(value).includes(normalized(query));
const nonempty = value => value !== undefined && value !== null && value !== '';

export function genres(record) {
  for (const raw of [record.genres_json, record.facet_genres_json]) {
    if (Array.isArray(raw) && raw.length) return raw.map(scalar);
    if (typeof raw === 'string') {
      try { const parsed = JSON.parse(raw); if (Array.isArray(parsed) && parsed.length) return parsed.map(scalar); } catch { /* legacy export */ }
    }
  }
  return ['primary_genre', ...Array.from({length: 7}, (_, i) => `genre_${i + 2}`)]
    .map(key => record[key]).filter(nonempty).map(scalar);
}

export function primaryGenre(record) {
  return record.primary_genre || genres(record)[0] || record.facet_primary_genre || '';
}

export function classificationValue(record, field) {
  return Object.hasOwn(record, `facet_${field}`) ? record[`facet_${field}`] : record[field];
}

export function historicalSnapshotNotice(metadata) {
  if (!metadata || typeof metadata !== 'object' || !metadata.published_at) return null;
  const published = new Date(metadata.published_at);
  const publication = Number.isNaN(published.valueOf()) ? 'unknown' : published.toISOString().slice(0, 10);
  let sourceURL = '';
  try {
    const parsed = new URL(metadata.dataset_url);
    if (parsed.protocol === 'https:' && ['www.kaggle.com','kaggle.com'].includes(parsed.hostname) && parsed.pathname.startsWith('/datasets/')) sourceURL = parsed.href;
  } catch { /* metadata without a usable source link */ }
  return {
    title: `${scalar(metadata.snapshot_label) || 'Historical'} snapshot · published ${publication}`,
    description: `Historical public data from ${scalar(metadata.provider) || 'a third-party source'}. The observation date is unknown. Snapshot records are unverified until individually checked against Metal-Archives; this is not a complete, current mirror.`,
    progress: metadata.complete === true ? 'Snapshot import finished. This does not establish live catalog completeness.' : 'Snapshot import is in progress. You can search the records imported so far.',
    sourceURL,
  };
}

export function isUpcoming(record) {
  return record.is_upcoming === true || record.is_upcoming === 1 || record.is_upcoming === '1' || record.is_upcoming === 'true';
}

export function needsClassification(record) {
  return !nonempty(classificationValue(record, 'ecommerce_primary_class')) ||
    ['unmapped', 'unreviewed', 'needs_review', 'pending', 'conflict', 'missing'].includes(normalized(classificationValue(record, 'classification_status')));
}

function related(record, type) {
  const fields = {
    band: ['band_name', 'band_id', 'band_names', 'band_ids', 'band', 'facet_band_name', 'facet_band_id'],
    release: ['release_title', 'release_name', 'release_id', 'album_title', 'album_id', 'facet_release_title', 'facet_release_id'],
    song: ['song_title', 'song_name', 'song_id', 'ma_song_id'],
    label: ['label_name', 'label_id', 'label_names', 'label_ids', 'facet_label_name', 'facet_label_id'],
    artist: ['artist_name', 'artist_id', 'artist_names', 'artist_ids'],
  }[type];
  const values = fields.map(key => record[key]);
  if (record.entity_type === type || (type === 'release' && record.entity_type === 'upcoming')) values.push(record.name, record.title, record.entity_id);
  if (record.source_entity_type === type) values.push(record.source_entity_name, record.source_entity_id);
  if (record.target_entity_type === type) values.push(record.target_entity_name, record.target_entity_id);
  for (const linked of Array.isArray(record.related_entities) ? record.related_entities : []) {
    if (linked?.entity_type === type) values.push(linked.name, linked.entity_id);
  }
  return values.map(scalar).join(' ');
}

export function matches(record, filters = {}) {
  if (filters.entity_type && record.entity_type !== filters.entity_type) return false;
  if (!includes([record.name, record.title, record.entity_id], filters.name)) return false;
  for (const type of ['band', 'release', 'song', 'label', 'artist']) {
    if (!includes(related(record, type), filters[type])) return false;
  }
  const ordered = genres(record);
  if (!includes(primaryGenre(record), filters.primary_genre)) return false;
  if (filters.any_genre && !ordered.some(value => includes(value, filters.any_genre))) return false;
  if (filters.genre_position) {
    const position = Number(filters.genre_position);
    if (!Number.isInteger(position) || position < 1 || !ordered[position - 1]) return false;
    if (!includes(ordered[position - 1], filters.position_genre)) return false;
  } else if (filters.position_genre && !ordered.some(value => includes(value, filters.position_genre))) return false;
  for (const key of ['country', 'location', 'release_type']) {
    if (!includes(record[key] || record[`facet_${key}`], filters[key])) return false;
  }
  if (!includes(classificationValue(record, 'ecommerce_primary_class'), filters.ecommerce_primary_class)) return false;
  if (!includes(record.band_status || record.facet_band_status || record.status, filters.status)) return false;
  for (const [prefix, key] of [['formed', 'formed_in'], ['release', 'release_year']]) {
    const year = Number.parseInt(record[key] || record[`facet_${key}`] || (prefix === 'release' ? scalar(record.release_date).slice(0, 4) : ''), 10);
    if (filters[`${prefix}_from`] && (!Number.isFinite(year) || year < Number(filters[`${prefix}_from`]))) return false;
    if (filters[`${prefix}_to`] && (!Number.isFinite(year) || year > Number(filters[`${prefix}_to`]))) return false;
  }
  if (filters.upcoming && (isUpcoming(record) !== (filters.upcoming === 'yes'))) return false;
  if (filters.needs_classification && !needsClassification(record)) return false;
  if (filters.changed_since) {
    const changed = Date.parse(record.facet_last_changed_at || record.last_changed_at || record.changed_at || '');
    const since = Date.parse(filters.changed_since);
    if (!Number.isFinite(changed) || !Number.isFinite(since) || changed < since) return false;
  }
  if (filters.query) {
    const searchValues = [record.name, record.title, record.entity_id, record.country || record.facet_country, record.location || record.facet_location,
      primaryGenre(record), ...ordered, classificationValue(record, 'ecommerce_primary_class'), classificationValue(record, 'ecommerce_secondary_tags'),
      ...['band', 'release', 'song', 'label', 'artist'].map(type => related(record, type))];
    if (!includes(searchValues, filters.query)) return false;
  }
  return true;
}

export const CSV_FIELDS = ['entity_type', 'entity_id', 'name', 'band_name', 'release_title', 'primary_genre',
  'genres_raw', 'genres_json', 'secondary_genres', 'facet_primary_genre', 'facet_genres_json', 'genre_origin', 'genre_source_url',
  'ecommerce_primary_class', 'facet_ecommerce_primary_class', 'country', 'location', 'label_name', 'release_date',
  'last_checked', 'verification_status', 'classification_status', 'source_url'];

export function csvCell(value) {
  let result = scalar(value);
  // Prevent spreadsheet formula execution, including whitespace-prefixed formulas.
  if (/^[\s\u0000-\u001f]*[=+@-]/.test(result)) result = "'" + result;
  return '"' + result.replaceAll('"', '""') + '"';
}

export function csvRow(record) {
  return CSV_FIELDS.map(key => csvCell(record[key])).join(',') + '\r\n';
}

export function safeDataURL(path, baseURL) {
  const base = new URL(baseURL);
  const result = new URL(path, base);
  if (result.origin !== base.origin || !result.pathname.startsWith(base.pathname) || !['http:', 'https:'].includes(result.protocol)) {
    throw new Error('Data files must stay inside the catalog data directory.');
  }
  return result.href;
}
