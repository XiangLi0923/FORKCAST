"""Operational audit and completion bookkeeping inside simulation transactions."""

from datetime import UTC, datetime
from sqlite3 import Connection
from typing import Literal
from uuid import uuid4

from .models import ActivateWindow, AuditRecord, SnapshotRef
from .simulator import AppliedEvent


def write_ops_audit(
    db: Connection, ref: SnapshotRef, *, action: Literal["OPS_CONFIRMED", "OPS_APPLIED"],
    actor_type: Literal["OPS", "SYSTEM"], actor_id: str, venue_id: str, proposal_id: str, at: datetime,
):
    audit = AuditRecord(
        audit_id=f"oa{uuid4().hex}", run_id=ref.run_id, venue_id=venue_id, proposal_id=proposal_id,
        action=action, actor_type=actor_type, actor_id=actor_id, result_code="OK", at=at, snapshot_id=ref.snapshot_id,
    )
    db.execute("INSERT INTO audit VALUES (?, ?, ?)", (audit.run_id, audit.audit_id, audit.model_dump_json()))


def record_window_applications(db: Connection, ref: SnapshotRef, applied: tuple[AppliedEvent, ...]):
    for entry in applied:
        payload = entry.event.payload
        if not isinstance(payload, ActivateWindow) or payload.window_id is None:
            continue
        actions = db.execute(
            """SELECT p.proposal_id, p.venue_id FROM ops_proposals p JOIN ops_confirmations c USING (proposal_id)
               WHERE p.run_id=? AND p.target_window_id=? AND p.status='CONFIRMED'
                 AND c.application_snapshot_id IS NULL ORDER BY p.venue_id, p.dish_id, p.proposal_id""",
            (ref.run_id, payload.window_id),
        ).fetchall()
        for action in actions:
            proposal, venue = action["proposal_id"], action["venue_id"]
            db.execute("UPDATE ops_proposals SET status='APPLIED' WHERE proposal_id=?", (proposal,))
            db.execute(
                "UPDATE ops_confirmations SET application_snapshot_id=?, applied_at=? WHERE proposal_id=?",
                (ref.snapshot_id, entry.event.scheduled_at.isoformat(), proposal),
            )
            write_ops_audit(db, ref, action="OPS_APPLIED", actor_type="SYSTEM", actor_id="simulator",
                            venue_id=venue, proposal_id=proposal, at=datetime.now(UTC))
