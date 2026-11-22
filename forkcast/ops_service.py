"""Transactional operations analysis and reuse of current-version proposals."""

import json
from sqlite3 import Connection
from uuid import uuid4

from .demand_insights import DemandAggregationService, permitted_venues
from .errors import Fault
from .fixture import Fixture
from .models import Catalog, ServiceWindow, SnapshotRef
from .ops_analysis import OpsAnalysis, OpsProposal, OpsProposalsRequest, OpsReport, analyze_supply
from .store import OpsAuthority, SimulationStore, resolve_operator


class OperationsAnalysisService:
    def __init__(self, store: SimulationStore):
        self.store = store

    def generate(self, request: OpsProposalsRequest, actor: OpsAuthority) -> tuple[SnapshotRef, OpsReport]:
        with self.store.transaction(write=True) as db:
            actor = resolve_operator(actor, db, request.run_id)
            run = self.store._run(db, request.run_id)
            frame = self.store._frame(db, request.run_id)
            ref = frame.supply.ref
            catalog = Catalog.model_validate_json(run["catalog_json"])
            allowed = permitted_venues(catalog, actor, request.venue_id, ref)
            fixture = Fixture.model_validate_json(run["fixture_json"])
            windows = sorted(fixture.scoped_windows(ref.run_id), key=lambda item: item.starts_at)
            by_id = {row.window_id: row for row in windows}
            reference = by_id.get(request.reference_window_id)
            target = by_id.get(request.target_window_id)
            if reference is None or target is None:
                raise Fault("NOT_FOUND", "Service window not found.")
            if reference.window_id != frame.supply.active_window_id or reference.starts_at > ref.clock_at:
                raise Fault("INVALID_INPUT", "Reference window must be the current open window.", ref)
            if windows.index(target) != windows.index(reference) + 1 or target.starts_at <= ref.clock_at:
                raise Fault("INVALID_INPUT", "Target window must be the next unopened window.", ref)

            demand = DemandAggregationService.aggregate(db, run, ref, reference, catalog, allowed)
            target_id = target.window_id
            plans = {p.dish_id: p for p in frame.plans if p.run_id == ref.run_id and p.window_id == target_id}
            prep = {p.venue_id: p for p in frame.prep if p.run_id == ref.run_id and p.window_id == target_id}
            stock = {p.dish_id: p for p in frame.ingredients if p.run_id == ref.run_id and p.window_id == target_id}
            analyses = tuple(
                analyze_supply(row, plans[row.dish_id], prep.get(row.venue_id), stock.get(row.dish_id))
                for row in demand.rows if row.dish_id in plans
            )
            feasible = [row for row in analyses if row.feasible_extra_portions]
            feasible.sort(key=lambda row: (-row.shortage_portions, row.venue_id, row.dish_id))
            proposals = tuple(
                reuse_or_create(db, ref, target, demand.signals_revision, row) for row in feasible
            )
            return ref, OpsReport(signals_revision=demand.signals_revision, analyses=analyses, proposals=proposals)


def reuse_or_create(
    db: Connection, ref: SnapshotRef, target: ServiceWindow, revision: int, analysis: OpsAnalysis,
) -> OpsProposal:
    key = (ref.run_id, analysis.reference_window_id, target.window_id, analysis.venue_id,
           analysis.dish_id, ref.snapshot_id, revision)
    existing = db.execute(
        """SELECT proposal_json, status FROM ops_proposals
           WHERE run_id=? AND reference_window_id=? AND target_window_id=? AND venue_id=?
             AND dish_id=? AND based_on_snapshot_id=? AND signals_revision=?""", key,
    ).fetchone()
    if existing is not None:
        return OpsProposal.model_validate_json(json.dumps({
            **json.loads(existing["proposal_json"]), "status": existing["status"],
        }))
    proposal = OpsProposal(
        proposal_id=f"op{uuid4().hex}", run_id=ref.run_id, venue_id=analysis.venue_id, dish_id=analysis.dish_id,
        reference_window_id=analysis.reference_window_id, target_window_id=target.window_id,
        based_on_snapshot_id=ref.snapshot_id, signals_revision=revision, expected_effective_at=target.starts_at,
        demand_proxy_portions=analysis.demand_proxy_portions, planned_supply_portions=analysis.planned_supply_portions,
        shortage_portions=analysis.shortage_portions, proposed_extra_portions=analysis.feasible_extra_portions,
        uncovered_portions=analysis.uncovered_portions, evidence_codes=analysis.evidence_codes, status="PROPOSED",
    )
    db.execute(
        """INSERT INTO ops_proposals
           (proposal_id,run_id,reference_window_id,target_window_id,venue_id,dish_id,
            based_on_snapshot_id,signals_revision,status,proposal_json)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (proposal.proposal_id, *key, proposal.status, proposal.model_dump_json(exclude={"status"})),
    )
    return proposal
