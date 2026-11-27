PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO schema_version VALUES(1);

CREATE TABLE IF NOT EXISTS entities(
  entity_type TEXT NOT NULL CHECK(entity_type IN ('band','release','song','label','artist','relationship','upcoming','review')),
  entity_id TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  source_url TEXT NOT NULL DEFAULT '',
  source_system TEXT NOT NULL DEFAULT 'Metal-Archives',
  data_json TEXT NOT NULL CHECK(json_valid(data_json)),
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  last_checked TEXT,
  source_updated_at TEXT,
  content_hash TEXT NOT NULL,
  active_status TEXT NOT NULL DEFAULT 'active' CHECK(active_status IN ('active','missing','removed')),
  verification_status TEXT NOT NULL DEFAULT 'pending',
  raw_source_ref TEXT,
  missing_count INTEGER NOT NULL DEFAULT 0,
  last_missing_run TEXT,
  last_changed_at TEXT NOT NULL,
  last_changed_run TEXT NOT NULL,
  PRIMARY KEY(entity_type,entity_id)
);
CREATE INDEX IF NOT EXISTS entities_checked ON entities(entity_type,last_checked);
CREATE INDEX IF NOT EXISTS entities_changed ON entities(last_changed_at);

CREATE TABLE IF NOT EXISTS source_observations(
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  source TEXT NOT NULL,
  data_json TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  checked_at TEXT NOT NULL,
  run_id TEXT NOT NULL,
  verified INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(entity_type,entity_id,source)
);

CREATE TABLE IF NOT EXISTS sync_runs(
  run_id TEXT PRIMARY KEY,
  mode TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL DEFAULT 'running',
  api_status TEXT NOT NULL DEFAULT 'not_checked',
  website_status TEXT NOT NULL DEFAULT 'not_checked',
  records_discovered INTEGER NOT NULL DEFAULT 0,
  records_checked INTEGER NOT NULL DEFAULT 0,
  records_inserted INTEGER NOT NULL DEFAULT 0,
  records_updated INTEGER NOT NULL DEFAULT 0,
  records_unchanged INTEGER NOT NULL DEFAULT 0,
  records_removed INTEGER NOT NULL DEFAULT 0,
  records_reappeared INTEGER NOT NULL DEFAULT 0,
  conflicts INTEGER NOT NULL DEFAULT 0,
  review_items INTEGER NOT NULL DEFAULT 0,
  estimated_coverage REAL,
  estimated_confidence REAL,
  error_summary TEXT NOT NULL DEFAULT '',
  checkpoint TEXT,
  metrics_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS changes(
  change_id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES sync_runs(run_id),
  detected_at TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  entity_name TEXT NOT NULL,
  change_type TEXT NOT NULL,
  changed_field TEXT NOT NULL,
  previous_value TEXT,
  new_value TEXT,
  source_url TEXT NOT NULL,
  review_status TEXT NOT NULL DEFAULT 'unreviewed'
);
CREATE INDEX IF NOT EXISTS changes_run ON changes(run_id);
CREATE INDEX IF NOT EXISTS changes_entity ON changes(entity_type,entity_id,detected_at);

CREATE TABLE IF NOT EXISTS review_queue(
  review_id INTEGER PRIMARY KEY AUTOINCREMENT,
  review_key TEXT UNIQUE NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  run_id TEXT,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  field TEXT NOT NULL DEFAULT '',
  details_json TEXT NOT NULL DEFAULT '{}',
  source_url TEXT NOT NULL DEFAULT '',
  review_status TEXT NOT NULL DEFAULT 'unresolved'
);
CREATE TABLE IF NOT EXISTS checkpoints(
  checkpoint_key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS genre_map(
  original_genre TEXT PRIMARY KEY,
  ecommerce_classification TEXT NOT NULL DEFAULT '',
  secondary_tags TEXT NOT NULL DEFAULT '[]',
  confidence REAL NOT NULL DEFAULT 0 CHECK(confidence BETWEEN 0 AND 1),
  review_status TEXT NOT NULL DEFAULT 'needs_review',
  notes TEXT NOT NULL DEFAULT '',
  taxonomy_version TEXT NOT NULL DEFAULT '1',
  last_updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS classification_overrides(
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  field TEXT NOT NULL,
  override_value TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  reviewer TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL,
  PRIMARY KEY(entity_type,entity_id,field)
);

CREATE VIEW IF NOT EXISTS bands AS SELECT
  entity_id AS ma_band_id,name, json_extract(data_json,'$.aliases') AS aliases,
  json_extract(data_json,'$.country') AS country,json_extract(data_json,'$.location') AS location,
  json_extract(data_json,'$.status') AS status,json_extract(data_json,'$.formed_in') AS formed_in,
  json_extract(data_json,'$.genres_raw') AS genres_raw,json_extract(data_json,'$.genres_json') AS genres_json,
  json_extract(data_json,'$.primary_genre') AS primary_genre,
  json_extract(data_json,'$.genre_2') AS genre_2,json_extract(data_json,'$.genre_3') AS genre_3,
  json_extract(data_json,'$.genre_4') AS genre_4,json_extract(data_json,'$.genre_5') AS genre_5,
  json_extract(data_json,'$.genre_6') AS genre_6,json_extract(data_json,'$.genre_7') AS genre_7,
  json_extract(data_json,'$.genre_8') AS genre_8,json_extract(data_json,'$.themes') AS themes,
  json_extract(data_json,'$.official_url') AS official_url,source_url AS ma_url,
  first_seen,last_seen,last_checked,source_updated_at,content_hash,active_status,verification_status,
  raw_source_ref,source_system,entity_type FROM entities WHERE entity_type='band';
CREATE VIEW IF NOT EXISTS releases AS SELECT
  entity_id AS ma_release_id,json_extract(data_json,'$.band_id') AS band_id,name AS title,
  json_extract(data_json,'$.release_type') AS release_type,json_extract(data_json,'$.release_date') AS release_date,
  json_extract(data_json,'$.release_year') AS release_year,json_extract(data_json,'$.label_id') AS label_id,
  json_extract(data_json,'$.label_name') AS label_name,json_extract(data_json,'$.formats') AS formats,
  json_extract(data_json,'$.genres_raw') AS genres_raw,json_extract(data_json,'$.is_upcoming') AS is_upcoming,
  source_url AS ma_url,first_seen,last_seen,last_checked,source_updated_at,content_hash,active_status,
  verification_status,raw_source_ref,source_system,entity_type FROM entities WHERE entity_type='release';
CREATE VIEW IF NOT EXISTS songs AS SELECT
  entity_id AS ma_song_id,json_extract(data_json,'$.release_id') AS release_id,
  json_extract(data_json,'$.band_id') AS band_id,json_extract(data_json,'$.track_number') AS track_number,
  name AS title,json_extract(data_json,'$.duration') AS duration,
  source_url AS ma_url,first_seen,last_seen,last_checked,source_updated_at,content_hash,active_status,
  verification_status,raw_source_ref,source_system,entity_type FROM entities WHERE entity_type='song';
CREATE VIEW IF NOT EXISTS labels AS SELECT
  entity_id AS ma_label_id,name,json_extract(data_json,'$.country') AS country,
  json_extract(data_json,'$.status') AS status,json_extract(data_json,'$.parent_label') AS parent_label,
  json_extract(data_json,'$.sub_label') AS sub_label,source_url AS ma_url,
  first_seen,last_seen,last_checked,source_updated_at,content_hash,active_status,verification_status,
  raw_source_ref,source_system,entity_type FROM entities WHERE entity_type='label';
CREATE VIEW IF NOT EXISTS artists AS SELECT
  entity_id AS ma_artist_id,name,json_extract(data_json,'$.aliases') AS aliases,
  json_extract(data_json,'$.status') AS status,source_url AS ma_url,
  first_seen,last_seen,last_checked,source_updated_at,content_hash,active_status,verification_status,
  raw_source_ref,source_system,entity_type FROM entities WHERE entity_type='artist';
CREATE VIEW IF NOT EXISTS relationships AS SELECT
  entity_id AS relationship_id,json_extract(data_json,'$.source_entity_type') AS source_entity_type,
  json_extract(data_json,'$.source_entity_id') AS source_entity_id,
  json_extract(data_json,'$.target_entity_type') AS target_entity_type,
  json_extract(data_json,'$.target_entity_id') AS target_entity_id,
  json_extract(data_json,'$.relationship_type') AS relationship_type,
  json_extract(data_json,'$.role') AS role,json_extract(data_json,'$.start_date') AS start_date,
  json_extract(data_json,'$.end_date') AS end_date,source_url,
  first_seen,last_seen,last_checked,source_updated_at,content_hash,active_status,verification_status,
  raw_source_ref,source_system,entity_type FROM entities WHERE entity_type='relationship';
CREATE VIEW IF NOT EXISTS upcoming_releases AS SELECT
  entity_id AS ma_release_id,json_extract(data_json,'$.band_id') AS band_id,name AS title,
  json_extract(data_json,'$.announced_date') AS announced_date,
  json_extract(data_json,'$.expected_release_date') AS expected_release_date,
  json_extract(data_json,'$.label_name') AS label,json_extract(data_json,'$.status') AS status,
  source_url,first_seen,last_seen,last_checked,source_updated_at,content_hash,active_status,
  verification_status,raw_source_ref,source_system,entity_type FROM entities WHERE entity_type='upcoming';
CREATE VIEW IF NOT EXISTS review_metadata AS SELECT
  entity_id AS ma_review_id,name,json_extract(data_json,'$.release_id') AS release_id,
  json_extract(data_json,'$.rating') AS rating,json_extract(data_json,'$.review_date') AS review_date,
  source_url,first_seen,last_seen,last_checked,source_updated_at,content_hash,active_status,
  verification_status,raw_source_ref,source_system,entity_type FROM entities WHERE entity_type='review';
