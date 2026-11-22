"""Apply simulation transitions without performing I/O."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from .errors import Fault
from .fixture import Fixture, Scenario
from .models import (
    ActivateWindow, AdvanceClock, Count, DishState, DishStatus, Fact, IntRange, Queue,
    Sale, ServiceWindow, SimulationEvent, SnapshotFrame, SnapshotRef, SoldOut,
    SupplySnapshot, VenueControl, VenueState, VenueStatus,
)


@dataclass(frozen=True)
class AppliedEvent:
    event: SimulationEvent
    removed_portions: int | None = None


class Simulator:
    def __init__(self, fixture: Fixture, scenario: Scenario, run_id: str):
        self.fixture = fixture
        self.scenario = scenario
        self.run_id = run_id
        self.windows = fixture.scoped_windows(run_id)
        self._venue = {row.venue_id: row for row in fixture.venues}
        self._dish = {row.dish_id: row for row in fixture.dishes}

    def initial_frame(self) -> SnapshotFrame:
        plans, prep, ingredients = self.fixture.supply(self.run_id, self.windows)
        initial = SupplySnapshot(
            ref=self._ref(1, self.fixture.starts_at), active_window_id=None, venue_states=(), dish_states=(),
        )
        frame = SnapshotFrame(supply=initial, queues=(), plans=plans, prep=prep, ingredients=ingredients)
        return self._activate(frame, self.windows[0], self.fixture.starts_at)

    def _ref(self, revision: int, clock: datetime) -> SnapshotRef:
        return SnapshotRef(
            run_id=self.run_id, snapshot_id=f"{self.run_id}.s{revision}", revision=revision,
            clock_at=clock, data_mode="SIMULATION", catalog_version=self.fixture.fixture_version,
        )

    def _activate(self, frame: SnapshotFrame, window: ServiceWindow | None, clock: datetime) -> SnapshotFrame:
        fixture = self.fixture
        if window is None:
            venues = tuple(VenueState(
                venue_id=key,
                service_status=Fact[VenueStatus](**fixture.fact("CLOSED", clock, clock + timedelta(seconds=1))),
                queue_min=Fact[IntRange](**fixture.fact(None, clock)),
                service_min=Fact[IntRange](**fixture.fact(None, clock)),
            ) for key in self._venue)
            closed = SupplySnapshot(ref=frame.supply.ref, active_window_id=None, venue_states=venues, dish_states=())
            return frame.model_copy(update={"supply": closed, "queues": ()})

        override = self.scenario.queue_override if window.label == "W1" else None
        queues = tuple(
            override if override is not None and override.venue_id == row.venue_id else Queue(
                venue_id=row.venue_id, queue_depth=row.queue_depth, throughput_per_min=row.throughput_per_min,
            ) for row in fixture.venues
        )
        until = window.ends_at
        venues = tuple(VenueState(
            venue_id=queue.venue_id,
            service_status=Fact[VenueStatus](**fixture.fact("OPEN", clock, until)),
            queue_min=Fact[IntRange](**fixture.fact(queue.estimate(), clock, until)),
            service_min=Fact[IntRange](**fixture.fact(self._venue[queue.venue_id].service_min, clock, until)),
        ) for queue in queues)

        dishes = []
        for plan in frame.plans:
            if plan.window_id != window.window_id:
                continue
            dish = self._dish[plan.dish_id]
            stock = plan.base_planned_portions + plan.confirmed_extra_portions
            status = ("AVAILABLE" if stock else "SOLD_OUT") if dish.active else "UNAVAILABLE"
            dishes.append(DishState(
                window_id=window.window_id, dish_id=dish.dish_id,
                service_status=Fact[DishStatus](**fixture.fact(status, clock, until)),
                stock_portions=Fact[Count](**fixture.fact(stock, clock, until)),
                price_cents=Fact[Count](**fixture.fact(dish.price_cents, clock, until)),
            ))

        supply = SupplySnapshot(
            ref=frame.supply.ref, active_window_id=window.window_id, venue_states=venues, dish_states=tuple(dishes),
        )
        prep = tuple(row.model_copy(update={"reserved_prep_portions": 0})
                     if row.window_id == window.window_id and row.reserved_prep_portions else row for row in frame.prep)
        ingredients = tuple(row.model_copy(update={
            "extra_ingredient_portions": row.extra_ingredient_portions - row.reserved_ingredient_portions,
            "reserved_ingredient_portions": 0,
        }) if row.window_id == window.window_id and row.reserved_ingredient_portions else row
            for row in frame.ingredients)
        return frame.model_copy(update={"supply": supply, "queues": queues, "prep": prep, "ingredients": ingredients})

    def affected_venue(self, event: SimulationEvent) -> str | None:
        payload = event.payload
        if isinstance(payload, (Sale, SoldOut)):
            dish = self._dish.get(payload.dish_id)
            if dish is None:
                raise Fault("INVALID_INPUT", "Unknown dish.")
            return dish.venue_id
        if isinstance(payload, (Queue, VenueControl)):
            if payload.venue_id not in self._venue:
                raise Fault("INVALID_INPUT", "Unknown venue.")
            return payload.venue_id
        return None

    def _apply(self, frame: SnapshotFrame, event: SimulationEvent) -> tuple[SnapshotFrame, AppliedEvent]:
        payload = event.payload
        if isinstance(payload, ActivateWindow):
            window = next((w for w in self.windows if w.window_id == payload.window_id), None)
            if (payload.window_id is not None and (window is None or window.starts_at != event.scheduled_at)
                    or payload.window_id is None and event.scheduled_at != self.windows[-1].ends_at):
                raise Fault("INVALID_INPUT", "Invalid window boundary.")
            return self._activate(frame, window, event.scheduled_at), AppliedEvent(event)

        supply = frame.supply
        venue_id = self.affected_venue(event)
        if supply.active_window_id is None:
            raise Fault("INVALID_INPUT", "No current service window.")
        if isinstance(payload, (Sale, SoldOut)):
            venue = next(row for row in supply.venue_states if row.venue_id == venue_id)
            dish = next(row for row in supply.dish_states if row.dish_id == payload.dish_id)
            if isinstance(payload, SoldOut) and dish.service_status.value != "AVAILABLE":
                raise Fault("INVALID_INPUT", "Only an available dish can be marked sold out.")
            stock = dish.stock_portions.value
            if stock is None:
                raise Fault("INVALID_INPUT", "Stock is unknown.")
            if isinstance(payload, Sale) and (
                payload.portions > stock or dish.service_status.value != "AVAILABLE"
                or venue.service_status.value != "OPEN"
            ):
                raise Fault("INVALID_INPUT", "Sale exceeds available stock or venue is closed.")
            removed = payload.portions if isinstance(payload, Sale) else stock
            remainder = stock - removed
            updated = DishState(
                window_id=dish.window_id, dish_id=dish.dish_id, price_cents=dish.price_cents,
                stock_portions=dish.stock_portions.model_copy(update={"value": remainder}),
                service_status=dish.service_status.model_copy(update={
                    "value": "AVAILABLE" if remainder else "SOLD_OUT",
                }),
            )
            supply = supply.model_copy(update={"dish_states": tuple(
                updated if row.dish_id == dish.dish_id else row for row in supply.dish_states
            )})
            return frame.model_copy(update={"supply": supply}), AppliedEvent(event, removed)

        queues = frame.queues
        if isinstance(payload, Queue):
            queues = tuple(payload if q.venue_id == payload.venue_id else q for q in queues)
            changes = {"queue_min": Fact[IntRange](**self.fixture.fact(payload.estimate(), event.scheduled_at))}
        elif isinstance(payload, VenueControl):
            changes = {"service_status": Fact[VenueStatus](**self.fixture.fact(payload.service_status, event.scheduled_at))}
        else:
            raise Fault("INVALID_INPUT", "Unexpected event inside simulation batch.")
        supply = supply.model_copy(update={"venue_states": tuple(
            row.model_copy(update=changes) if row.venue_id == venue_id else row for row in supply.venue_states
        )})
        return frame.model_copy(update={"supply": supply, "queues": queues}), AppliedEvent(event)

    def transition(self, frame: SnapshotFrame, command: SimulationEvent, scheduled: tuple[SimulationEvent, ...]):
        start = frame.supply.ref.clock_at
        if isinstance(command.payload, AdvanceClock):
            target = command.payload.to_clock_at.astimezone(self.fixture.starts_at.tzinfo)
            if not start < target <= self.windows[-1].ends_at:
                raise Fault("INVALID_INPUT", "Clock must move forward within the scenario.")
            due = sorted(
                (e for e in scheduled if start < e.scheduled_at <= target),
                key=lambda e: (e.scheduled_at, e.event_type != "ACTIVATE_WINDOW", e.sequence, e.event_id),
            )
            applied = []
            for event in due:
                frame, entry = self._apply(frame, event)
                applied.append(entry)
            applied.append(AppliedEvent(command))
        else:
            target = start
            frame, entry = self._apply(frame, command)
            applied = [entry]
        return self._publish(frame, target), tuple(applied)

    def _publish(self, frame: SnapshotFrame, clock: datetime) -> SnapshotFrame:
        snapshot = frame.supply
        window = next((w for w in self.windows if w.window_id == snapshot.active_window_id), None)
        until = window.ends_at if window is not None else clock + timedelta(seconds=1)

        def refresh(fact: Fact) -> Fact:
            # A fixed stale or unknown fixture retains provenance. Fresh synthetic values age with the clock.
            if fact.value is None or (fact.valid_until is not None and fact.valid_until < until):
                return fact
            return fact.model_copy(update={"as_of": clock, "valid_until": until})

        venues = tuple(row.model_copy(update={
            field: refresh(getattr(row, field))
            for field in ("service_status", "queue_min", "service_min")
        }) for row in snapshot.venue_states)
        dishes = tuple(row.model_copy(update={
            field: refresh(getattr(row, field))
            for field in ("service_status", "stock_portions", "price_cents")
        }) for row in snapshot.dish_states)
        supply = SupplySnapshot(
            ref=self._ref(snapshot.ref.revision + 1, clock), active_window_id=snapshot.active_window_id,
            venue_states=venues, dish_states=dishes,
        )
        return frame.model_copy(update={"supply": supply})
