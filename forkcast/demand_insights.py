"""Read-only, venue-scoped demand observations for authorized OPS clients."""

from collections import Counter
from sqlite3 import Connection, Row
from typing import Literal

from .errors import Fault
from .feedback_ledger import effective_demo_declarations
from .fixture import Fixture
from .models import ID, Catalog, Count, Model, ServiceWindow, SnapshotRef, Timestamp
from .store import OpsActor, OpsAuthority, SimulationStore, resolve_operator
from .student_models import FEEDBACK_CHANNELS, RunRequest

MIN_VISIBLE_SUBJECTS = 3
Visibility = Literal["VISIBLE", "SUPPRESSED"]
CHANNEL_FIELDS = {
    "PREFERENCE": "preferred_sessions",
    "INTENT": "intended_sessions",
    "MEAL": "dined_sessions",
}


class DemandInsightsQuery(RunRequest):
    reference_window_id: ID
    venue_id: ID | None = None


class DemandVisibility(Model):
    preferred_sessions: Visibility
    intended_sessions: Visibility
    dined_sessions: Visibility


class DemandInsight(Model):
    run_id: ID
    reference_window_id: ID
    venue_id: ID
    dish_id: ID
    preferred_sessions: Count | None
    intended_sessions: Count | None
    dined_sessions: Count | None
    visibility: DemandVisibility
    signals_revision: Count
    observed_until: Timestamp


class DemandInsights(Model):
    signals_revision: Count
    observed_until: Timestamp
    rows: tuple[DemandInsight, ...]


def permitted_venues(catalog: Catalog, actor: OpsActor, venue_id: str | None, ref: SnapshotRef) -> set[str]:
    allowed = {venue.venue_id for venue in catalog.venues}.intersection(actor.allowed_venue_ids)
    if venue_id is None:
        return allowed
    if venue_id not in allowed:
        raise Fault("FORBIDDEN", "Operator has no access to this venue.", ref)
    return {venue_id}


class DemandAggregationService:
    def __init__(self, store: SimulationStore):
        self.store = store

    def read(self, query: DemandInsightsQuery, actor: OpsAuthority) -> tuple[SnapshotRef, DemandInsights]:
        with self.store.transaction() as db:
            actor = resolve_operator(actor, db, query.run_id)
            run = self.store._run(db, query.run_id)
            ref = self.store._frame(db, query.run_id).supply.ref
            catalog = Catalog.model_validate_json(run["catalog_json"])
            allowed = permitted_venues(catalog, actor, query.venue_id, ref)

            fixture = Fixture.model_validate_json(run["fixture_json"])
            window = next((row for row in fixture.scoped_windows(query.run_id)
                           if row.window_id == query.reference_window_id), None)
            if window is None:
                raise Fault("NOT_FOUND", "Reference window not found.")
            if window.starts_at > ref.clock_at:
                raise Fault("INVALID_INPUT", "Reference window has not opened.", ref)
            return ref, self.aggregate(db, run, ref, window, catalog, allowed)

    @staticmethod
    def aggregate(
        db: Connection, run: Row, ref: SnapshotRef, window: ServiceWindow, catalog: Catalog, allowed: set[str],
    ) -> DemandInsights:
        """Project disclosed counts under the caller's fixed snapshot and transaction."""
        # Row shape and order depend only on the authorized catalog, never on private counts.
        dishes = sorted((row.venue_id, row.dish_id) for row in catalog.dishes if row.venue_id in allowed)
        allowed_dishes = {dish_id for _, dish_id in dishes}
        anchors = {bundle.bundle_id: bundle.anchor_dish_id for bundle in catalog.bundles}
        counts = Counter()
        for event in effective_demo_declarations(db, run, ref.clock_at, window.window_id):
            channel = FEEDBACK_CHANNELS[event["event_type"]]
            dish_id = event["dish_id"] if channel == "PREFERENCE" else anchors.get(event["bundle_id"])
            if dish_id in allowed_dishes:
                counts[dish_id, channel] += 1

        revision = run["signals_revision"]
        observed_until = min(ref.clock_at, window.ends_at)
        rows = []
        for venue_id, dish_id in dishes:
            visible = {
                field: counts[dish_id, channel] if counts[dish_id, channel] >= MIN_VISIBLE_SUBJECTS else None
                for channel, field in CHANNEL_FIELDS.items()
            }
            rows.append(DemandInsight(
                run_id=ref.run_id, reference_window_id=window.window_id, venue_id=venue_id, dish_id=dish_id,
                signals_revision=revision, observed_until=observed_until, **visible,
                visibility={field: "SUPPRESSED" if value is None else "VISIBLE" for field, value in visible.items()},
            ))
        return DemandInsights(signals_revision=revision, observed_until=observed_until, rows=tuple(rows))
