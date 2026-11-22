"""Confirm and inspect simulated kitchen operations under snapshot isolation."""

import json
from datetime import UTC, datetime

from .demand_insights import permitted_venues
from .errors import Fault
from .fixture import Fixture
from .models import AuditRecord, Catalog, SnapshotFrame, SnapshotRef
from .ops_events import write_ops_audit
from .ops_execution_models import (
    ActionProjection, AuditProjection, ConfirmOpsRequest, OpsConfirmation, OpsState, OpsStateQuery, PlanProjection,
)
from .store import OpsActor, OpsAuthority, SimulationStore, WriteApproval, resolve_operator


class OperationsExecutionService:
    def __init__(self, store: SimulationStore):
        self.store = store

    @staticmethod
    def proposal_scope(db, request: ConfirmOpsRequest, actor: OpsActor):
        proposal = db.execute(
            "SELECT * FROM ops_proposals WHERE run_id=? AND proposal_id=?",
            (request.run_id, request.proposal_id),
        ).fetchone()
        if proposal is None or proposal["venue_id"] not in actor.allowed_venue_ids:
            raise Fault("NOT_FOUND", "Proposal not found.")
        return proposal

    def confirm(
        self, request: ConfirmOpsRequest, actor: OpsAuthority, *, approval: WriteApproval | None = None,
    ) -> OpsConfirmation:
        with self.store.transaction(write=True) as db:
            actor = resolve_operator(actor, db, request.run_id)
            run = self.store._run(db, request.run_id)
            proposal = self.proposal_scope(db, request, actor)
            scope = actor.principal_id, request.run_id, "confirm-operations", request.idempotency_key
            digest = request.model_dump_json(exclude={"idempotency_key"})
            replay = self.store._saved(db, scope, digest, OpsConfirmation)
            if replay is not None:
                return replay
            if approval is not None:
                approval(db, actor.principal_id, request)

            frame = self.store._frame(db, request.run_id)
            ref = frame.supply.ref
            if proposal["status"] in ("CONFIRMED", "APPLIED"):
                raise Fault("OPS_ALREADY_CONFIRMED", "Proposal was already confirmed.", ref)
            if request.expected_snapshot_id != ref.snapshot_id:
                raise Fault("SNAPSHOT_CONFLICT", "Supply snapshot changed.", ref)
            fixture = Fixture.model_validate_json(run["fixture_json"])
            target = next(w for w in fixture.scoped_windows(ref.run_id) if w.window_id == proposal["target_window_id"])
            if (proposal["status"] != "PROPOSED" or proposal["based_on_snapshot_id"] != ref.snapshot_id
                    or proposal["signals_revision"] != run["signals_revision"] or target.starts_at <= ref.clock_at):
                raise Fault("STALE_PROPOSAL", "Proposal is no longer current. Generate a new proposal.", ref)

            amount = request.approved_extra_portions
            if amount > json.loads(proposal["proposal_json"])["proposed_extra_portions"]:
                raise Fault("INVALID_INPUT", "Approved amount exceeds the amount suggested.", ref)
            updated = reserve(frame, target.window_id, proposal["venue_id"], proposal["dish_id"], amount)
            output = updated.supply.ref
            now = datetime.now(UTC)
            result = OpsConfirmation(
                proposal_id=request.proposal_id, approved_extra_portions=amount, actor_id=actor.principal_id,
                confirmed_at=now, confirmation_snapshot=output,
            )
            # Change status before publishing so the snapshot invalidates only other pending proposals.
            db.execute("UPDATE ops_proposals SET status='CONFIRMED' WHERE proposal_id=?", (request.proposal_id,))
            self.store._insert_frame(db, updated)
            db.execute(
                """INSERT INTO ops_confirmations
                   (proposal_id,approved_extra_portions,actor_id,confirmed_at,confirmation_snapshot_id)
                   VALUES (?,?,?,?,?)""",
                (request.proposal_id, amount, actor.principal_id, now.isoformat(), output.snapshot_id),
            )
            write_ops_audit(db, output, action="OPS_CONFIRMED", actor_type="OPS", actor_id=actor.principal_id,
                            venue_id=proposal["venue_id"], proposal_id=request.proposal_id, at=now)
            self.store._save(db, scope, digest, result)
            return result

    def state(self, query: OpsStateQuery, actor: OpsActor) -> tuple[SnapshotRef, OpsState]:
        with self.store.transaction() as db:
            run = self.store._run(db, query.run_id)
            frame = self.store._frame(db, query.run_id, query.snapshot_id)
            ref = frame.supply.ref
            catalog = Catalog.model_validate_json(run["catalog_json"])
            allowed = permitted_venues(catalog, actor, query.venue_id, ref)
            fixture = Fixture.model_validate_json(run["fixture_json"])
            if query.target_window_id not in {window.window_id for window in fixture.scoped_windows(ref.run_id)}:
                raise Fault("NOT_FOUND", "Service window not found.")
            venues = {dish.dish_id: dish.venue_id for dish in catalog.dishes if dish.venue_id in allowed}
            plans = sorted(
                (PlanProjection(venue_id=venues[p.dish_id], dish_id=p.dish_id,
                                base_planned_portions=p.base_planned_portions,
                                confirmed_extra_portions=p.confirmed_extra_portions,
                                max_served_portions=p.max_served_portions)
                 for p in frame.plans if p.window_id == query.target_window_id and p.dish_id in venues),
                key=lambda p: (p.venue_id, p.dish_id),
            )
            actions = read_actions(db, ref, query.target_window_id, allowed)
            audit = read_audit(db, ref, {action.proposal_id for action in actions})
            return ref, OpsState(target_window_id=query.target_window_id, plans=tuple(plans), actions=actions, audit=audit)


def reserve(frame: SnapshotFrame, window: str, venue: str, dish: str, amount: int) -> SnapshotFrame:
    ref = frame.supply.ref
    plan = next((row for row in frame.plans if row.window_id == window and row.dish_id == dish), None)
    prep = next((row for row in frame.prep if row.window_id == window and row.venue_id == venue), None)
    pool = next((row for row in frame.ingredients if row.window_id == window and row.dish_id == dish), None)
    if plan is None or prep is None or pool is None or amount > min(
        plan.max_served_portions - plan.base_planned_portions - plan.confirmed_extra_portions,
        prep.extra_prep_capacity_portions - prep.reserved_prep_portions,
        pool.extra_ingredient_portions - pool.reserved_ingredient_portions,
    ):
        raise Fault("STALE_PROPOSAL", "Available resources no longer cover the approved amount.", ref)
    revision = ref.revision + 1
    updated_ref = ref.model_copy(update={"revision": revision, "snapshot_id": f"{ref.run_id}.s{revision}"})
    return frame.model_copy(update={
        "supply": frame.supply.model_copy(update={"ref": updated_ref}),
        "plans": tuple(row.model_copy(update={"confirmed_extra_portions": row.confirmed_extra_portions + amount})
                       if row is plan else row for row in frame.plans),
        "prep": tuple(row.model_copy(update={"reserved_prep_portions": row.reserved_prep_portions + amount})
                      if row is prep else row for row in frame.prep),
        "ingredients": tuple(row.model_copy(update={"reserved_ingredient_portions": row.reserved_ingredient_portions + amount})
                             if row is pool else row for row in frame.ingredients),
    })


def read_actions(db, ref: SnapshotRef, window: str, allowed: set[str]) -> tuple[ActionProjection, ...]:
    rows = db.execute(
        """SELECT p.proposal_id,p.venue_id,p.dish_id,c.approved_extra_portions,c.confirmation_snapshot_id,
                  c.application_snapshot_id,c.applied_at,applied.revision AS applied_revision
           FROM ops_proposals p JOIN ops_confirmations c USING (proposal_id)
           JOIN snapshots confirmed ON confirmed.snapshot_id=c.confirmation_snapshot_id
           LEFT JOIN snapshots applied ON applied.snapshot_id=c.application_snapshot_id
           WHERE p.run_id=? AND p.target_window_id=? AND confirmed.revision<=?
           ORDER BY p.venue_id,p.dish_id,p.proposal_id""", (ref.run_id, window, ref.revision),
    )
    actions = []
    for row in rows:
        if row["venue_id"] not in allowed:
            continue
        applied = row["applied_revision"] is not None and row["applied_revision"] <= ref.revision
        actions.append(ActionProjection(
            proposal_id=row["proposal_id"], venue_id=row["venue_id"], dish_id=row["dish_id"],
            status="APPLIED" if applied else "CONFIRMED", approved_extra_portions=row["approved_extra_portions"],
            confirmation_snapshot_id=row["confirmation_snapshot_id"],
            application_snapshot_id=row["application_snapshot_id"] if applied else None,
            applied_at=row["applied_at"] if applied else None,
        ))
    return tuple(actions)


def read_audit(db, ref: SnapshotRef, proposals: set[str]) -> tuple[AuditProjection, ...]:
    if not proposals:
        return ()
    rows = db.execute(
        """SELECT a.audit_json FROM audit a JOIN snapshots s ON s.run_id=a.run_id
             AND s.snapshot_id=json_extract(a.audit_json,'$.snapshot_id')
           WHERE a.run_id=? AND s.revision<=? ORDER BY s.revision,a.audit_id""", (ref.run_id, ref.revision),
    )
    audit = []
    for row in rows:
        record = AuditRecord.model_validate_json(row["audit_json"])
        if record.proposal_id in proposals and record.action in ("OPS_CONFIRMED", "OPS_APPLIED"):
            audit.append(AuditProjection(**record.model_dump(
                include={"audit_id", "venue_id", "proposal_id", "action", "result_code", "at", "snapshot_id"},
            )))
    return tuple(audit)
