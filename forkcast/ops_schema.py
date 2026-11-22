"""Versioned proposal storage. Proposal generation never writes physical resources."""

OPS_SCHEMA = """
CREATE TABLE IF NOT EXISTS ops_proposals (
    proposal_id TEXT PRIMARY KEY NOT NULL,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    reference_window_id TEXT NOT NULL,
    target_window_id TEXT NOT NULL,
    venue_id TEXT NOT NULL,
    dish_id TEXT NOT NULL,
    based_on_snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id) ON DELETE CASCADE,
    signals_revision INTEGER NOT NULL CHECK (signals_revision >= 0),
    status TEXT NOT NULL CHECK (status IN ('PROPOSED', 'CONFIRMED', 'APPLIED', 'STALE')),
    proposal_json TEXT NOT NULL,
    UNIQUE (run_id, reference_window_id, target_window_id, venue_id, dish_id,
            based_on_snapshot_id, signals_revision)
);
CREATE INDEX IF NOT EXISTS ops_proposals_by_status ON ops_proposals(run_id, status);

CREATE TABLE IF NOT EXISTS ops_confirmations (
    proposal_id TEXT PRIMARY KEY NOT NULL REFERENCES ops_proposals(proposal_id) ON DELETE CASCADE,
    approved_extra_portions INTEGER NOT NULL CHECK (approved_extra_portions > 0),
    actor_id TEXT NOT NULL,
    confirmed_at TEXT NOT NULL,
    confirmation_snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id) ON DELETE CASCADE,
    application_snapshot_id TEXT REFERENCES snapshots(snapshot_id) ON DELETE CASCADE,
    applied_at TEXT,
    CHECK ((application_snapshot_id IS NULL) = (applied_at IS NULL))
);

CREATE TRIGGER IF NOT EXISTS ops_proposals_stale_on_signal_change
AFTER UPDATE OF signals_revision ON runs
WHEN OLD.signals_revision <> NEW.signals_revision
BEGIN
    UPDATE ops_proposals SET status = 'STALE'
    WHERE run_id = NEW.run_id AND status = 'PROPOSED' AND signals_revision <> NEW.signals_revision;
END;

CREATE TRIGGER IF NOT EXISTS ops_proposals_stale_on_snapshot_change
AFTER INSERT ON snapshots
BEGIN
    UPDATE ops_proposals SET status = 'STALE'
    WHERE run_id = NEW.run_id AND status = 'PROPOSED' AND based_on_snapshot_id <> NEW.snapshot_id;
END;
"""
