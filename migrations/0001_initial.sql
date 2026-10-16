-- Initial schema (docs/06 "SQLite schema"). Timestamps are ISO-8601 UTC strings.

CREATE TABLE device (
  serial INTEGER PRIMARY KEY,
  product_name TEXT,
  hw_id INTEGER,
  dsp_version INTEGER,
  fw TEXT,
  daemon_url TEXT,
  first_seen TEXT,
  last_seen TEXT
);

-- Desired state plus the applied shadow (NULL / unknown_paths = not known to be on the device).
CREATE TABLE preset_state (
  serial INTEGER NOT NULL,
  preset INTEGER NOT NULL CHECK (preset BETWEEN 0 AND 3),
  desired_json TEXT NOT NULL,
  applied_json TEXT,
  unknown_paths_json TEXT NOT NULL DEFAULT '[]',
  revision INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT,
  PRIMARY KEY (serial, preset)
);

-- Undo/history. source: ui | api | import | profile | adopt | revert
CREATE TABLE revision (
  id INTEGER PRIMARY KEY,
  serial INTEGER NOT NULL,
  preset INTEGER NOT NULL,
  revision INTEGER NOT NULL,
  desired_json TEXT NOT NULL,
  summary TEXT,
  source TEXT,
  created_at TEXT
);
CREATE UNIQUE INDEX revision_by_preset ON revision (serial, preset, revision);

CREATE TABLE profile (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  description TEXT,
  product_name TEXT,
  config_json TEXT NOT NULL,
  created_at TEXT,
  updated_at TEXT
);

-- taps: little-endian float64, n_taps values.
CREATE TABLE fir_blob (
  id TEXT PRIMARY KEY,
  name TEXT,
  taps BLOB NOT NULL,
  n_taps INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  created_at TEXT
);
CREATE UNIQUE INDEX fir_blob_by_sha256 ON fir_blob (sha256);

CREATE TABLE channel_meta (
  serial INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('input', 'output')),
  idx INTEGER NOT NULL,
  name TEXT,
  color TEXT,
  max_gain_db REAL,
  PRIMARY KEY (serial, kind, idx)
);

CREATE TABLE setting (
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL
);

CREATE TABLE event_log (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  level TEXT NOT NULL,
  kind TEXT NOT NULL,
  message TEXT NOT NULL,
  data_json TEXT
);
