"""Additive, run-scoped M2.2 SQLite schema."""

STUDENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS student_secret (
    singleton INTEGER NOT NULL PRIMARY KEY CHECK (singleton = 1),
    secret BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS student_sessions (
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    principal_id TEXT NOT NULL,
    token_digest TEXT NOT NULL UNIQUE,
    session_kind TEXT NOT NULL CHECK (session_kind IN ('USABILITY_TEST', 'DEMO')),
    share_for_ops INTEGER NOT NULL CHECK (share_for_ops IN (0, 1)),
    consent_revision INTEGER NOT NULL CHECK (consent_revision >= 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, principal_id)
);
CREATE TABLE IF NOT EXISTS demo_enrollments (
    token_digest TEXT NOT NULL PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL,
    consumed_at TEXT
);
CREATE TABLE IF NOT EXISTS recommendation_records (
    recommendation_id TEXT NOT NULL PRIMARY KEY,
    run_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    record_json TEXT NOT NULL,
    FOREIGN KEY (run_id, principal_id) REFERENCES student_sessions(run_id, principal_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS feedback_events (
    commit_seq INTEGER PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    window_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    stream TEXT NOT NULL CHECK (stream IN ('SIMULATED', 'USABILITY_TEST')),
    event_type TEXT NOT NULL,
    channel TEXT,
    recommendation_id TEXT REFERENCES recommendation_records(recommendation_id) ON DELETE CASCADE,
    bundle_id TEXT,
    dish_id TEXT,
    consent_revision INTEGER NOT NULL CHECK (consent_revision >= 0),
    FOREIGN KEY (run_id, principal_id) REFERENCES student_sessions(run_id, principal_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS feedback_scope ON feedback_events(run_id, principal_id, channel, window_id, commit_seq);
"""
