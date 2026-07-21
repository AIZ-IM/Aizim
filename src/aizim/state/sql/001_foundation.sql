CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL
);

CREATE TABLE events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    schema_version INTEGER NOT NULL CHECK (schema_version > 0),
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    actor TEXT NOT NULL,
    run_id TEXT,
    causation_id TEXT,
    payload_json TEXT NOT NULL
);

CREATE TABLE projections (
    projection_name TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 0),
    state_json TEXT NOT NULL,
    PRIMARY KEY (projection_name, entity_id)
);

CREATE TABLE capability_tokens (
    token_hash TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    role TEXT NOT NULL,
    lease_id TEXT,
    operations_json TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE TABLE publication_queue (
    enqueue_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    contribution_id TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL,
    state_version INTEGER NOT NULL,
    claimed_by TEXT,
    claimed_at TEXT
);

CREATE TABLE artifacts (
    content_hash TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    relative_path TEXT NOT NULL UNIQUE,
    media_type TEXT NOT NULL,
    byte_length INTEGER NOT NULL CHECK (byte_length >= 0)
);

CREATE INDEX events_run_sequence ON events(run_id, sequence);
CREATE INDEX projections_name ON projections(projection_name);
