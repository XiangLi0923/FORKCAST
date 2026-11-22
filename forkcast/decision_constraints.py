"""Candidate enumeration and hard constraints from R01 and R03."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .decision_models import DecisionInput, Evidence, EvidenceCode, PublicEvidenceValue
from .errors import Fault
from .models import Fact, Freshness, IntRange, MealBundle

MEAL_MINUTES = IntRange(min=8, max=12)
MINUTE = timedelta(minutes=1)
MICROSECOND = timedelta(microseconds=1)

_CONFLICT_DIMENSIONS = (
    ("REQUESTED_BUNDLE_NOT_REGISTERED",),
    ("RESTRICTED_INGREDIENT", "RESTRICTED_ALLERGEN", "RESTRICTION_UNVERIFIED"),
    ("VENUE_CLOSED", "OUTSIDE_SERVICE_WINDOW", "HANDOFF_TIME_UNKNOWN"),
    ("SOLD_OUT", "UNAVAILABLE", "INSUFFICIENT_STOCK", "SUPPLY_UNVERIFIED"),
    ("PRICE_UNKNOWN", "OVER_BUDGET"),
    ("OVER_TIME",),
)
_CONFLICT_ORDER = {code: i for i, group in enumerate(_CONFLICT_DIMENSIONS) for code in group}


def distinct(evidence: tuple[Evidence, ...]) -> tuple[Evidence, ...]:
    return tuple(dict.fromkeys(evidence))


def sorted_conflicts(evidence: tuple[Evidence, ...]) -> tuple[Evidence, ...]:
    return tuple(sorted(distinct(evidence), key=lambda e: (
        _CONFLICT_ORDER[e.code], e.code, e.entity_id is None, e.entity_id or "", e.field is None, e.field or "",
    )))


def notice(code: EvidenceCode, entity: str | None = None, field: str | None = None,
           value: PublicEvidenceValue | None = None) -> Evidence:
    return Evidence(code=code, entity_id=entity, field=field, source_kind="UNKNOWN",
                    source_ref=None, as_of=None, freshness="UNKNOWN", value=value)


@dataclass(frozen=True)
class FactUse:
    entity: str
    field: str
    fact: Fact | None
    freshness: Freshness

    @property
    def current(self) -> Any:
        return self.fact.value if self.freshness == "FRESH" and self.fact is not None else None

    def evidence(self, code: EvidenceCode, value: PublicEvidenceValue | None = None) -> Evidence:
        return Evidence(
            code=code, entity_id=self.entity, field=self.field,
            source_kind=self.fact.source_kind if self.fact is not None else "UNKNOWN",
            source_ref=self.fact.source_ref if self.fact is not None else None,
            as_of=self.fact.as_of if self.fact is not None else None,
            freshness=self.freshness, value=value,
        )


@dataclass(frozen=True)
class Assessment:
    bundle: MealBundle
    family_id: str
    taste_tags: frozenset[str]
    conflicts: tuple[Evidence, ...]
    price: int | None
    queue: FactUse
    total_time: IntRange | None
    portions: int | None
    critical: tuple[FactUse, ...]
    prices: tuple[FactUse, ...]
    time_facts: tuple[FactUse, ...]
    nutrition: tuple[FactUse, ...]
    kcal: IntRange | None


def validated_request(inputs: DecisionInput) -> None:
    request, catalog = inputs.request, inputs.catalog
    constraints, preferences = request.constraints, request.preferences
    checks = (
        (constraints.avoid_ingredients, catalog.ingredient_keys),
        (constraints.avoid_allergens, catalog.allergen_keys),
        (preferences.taste_tags, catalog.taste_tags),
        (preferences.recent_family_ids, catalog.family_ids),
    )
    if any(not set(keys).issubset(dictionary) for keys, dictionary in checks):
        raise Fault("INVALID_INPUT", "A decision field contains an unknown dictionary key.")
    if preferences.requested_dish_id is not None and not any(
        row.dish_id == preferences.requested_dish_id for row in catalog.dishes
    ):
        raise Fault("INVALID_INPUT", "Unknown requested dish.")
    if request.origin_zone_id not in {walk.origin_zone_id for walk in catalog.origin_walks} and (
        not request.origin_assumed or inputs.snapshot.ref.data_mode != "SIMULATION"
    ):
        raise Fault("INVALID_INPUT", "Unknown origin zone.")


class ConstraintEvaluator:
    def __init__(self, inputs: DecisionInput, time_budget_min: int | None):
        validated_request(inputs)
        self.inputs = inputs
        self.clock = inputs.snapshot.ref.clock_at
        self.time_budget_min = time_budget_min
        self.venues = {row.venue_id: row for row in inputs.catalog.venues}
        self.dishes = {row.dish_id: row for row in inputs.catalog.dishes}
        self.venue_states = {row.venue_id: row for row in inputs.snapshot.venue_states}
        self.dish_states = {row.dish_id: row for row in inputs.snapshot.dish_states}
        self.walks = {row.venue_id: row.walk_min for row in inputs.catalog.origin_walks
                      if row.origin_zone_id == inputs.request.origin_zone_id}

    def _fact(self, entity: str, field: str, fact: Fact | None, *, dynamic: bool = False) -> FactUse:
        freshness = fact.freshness(self.clock, dynamic=dynamic) if fact is not None else "UNKNOWN"
        if fact is not None and fact.as_of is None:
            freshness = "UNKNOWN"
        return FactUse(entity, field, fact, freshness)

    def _restrictions(self, bundle: MealBundle) -> tuple[tuple[FactUse, ...], tuple[Evidence, ...]]:
        needs = self.inputs.request.constraints
        facts, conflicts = [], []
        for item in bundle.items:
            dish = self.dishes[item.dish_id]
            for excluded, field, code in (
                (needs.avoid_ingredients, "ingredients", "RESTRICTED_INGREDIENT"),
                (needs.avoid_allergens, "allergens", "RESTRICTED_ALLERGEN"),
            ):
                if not excluded:
                    continue
                raw = getattr(dish, field)
                fact = self._fact(dish.dish_id, field, raw)
                facts.append(fact)
                # An old known hit still blocks. Never serialize the user's restriction or the matched word.
                if fact.freshness != "UNKNOWN" and raw.value is not None and set(raw.value).intersection(excluded):
                    conflicts.append(fact.evidence(code))
                elif fact.current is None:
                    conflicts.append(fact.evidence("RESTRICTION_UNVERIFIED"))
        return tuple(facts), tuple(conflicts)

    def _handoff(self, venue_id: str):
        venue, state = self.venues[venue_id], self.venue_states.get(venue_id)
        hours = self._fact(venue_id, "opening_hours", venue.opening_hours)
        status = self._fact(venue_id, "service_status", state.service_status if state else None, dynamic=True)
        walk = self._fact(venue_id, "walk_min", self.walks.get(venue_id))
        queue = self._fact(venue_id, "queue_min", state.queue_min if state else None, dynamic=True)
        service = self._fact(venue_id, "service_min", state.service_min if state else None, dynamic=True)
        facts = hours, status, walk, queue, service
        conflicts = [fact.evidence("HANDOFF_TIME_UNKNOWN") for fact in facts if fact.current is None]
        known_hours = hours.current
        open_until = max((period.ends_at for period in known_hours or ()
                          if period.starts_at <= self.clock < period.ends_at), default=None)
        if status.current == "CLOSED":
            conflicts.append(status.evidence("VENUE_CLOSED", "CLOSED"))
        if known_hours is not None and open_until is None:
            conflicts.append(hours.evidence("VENUE_CLOSED"))

        window = self.inputs.window
        in_window = window is not None and window.starts_at <= self.clock < window.ends_at
        if not in_window:
            conflicts.append(notice("OUTSIDE_SERVICE_WINDOW", window.window_id if window else None, "active_window_id"))

        total = None
        if all(fact.current is not None for fact in (walk, queue, service)):
            lower = walk.current.min + queue.current.min + service.current.min
            upper = walk.current.max + queue.current.max + service.current.max
            total = IntRange(min=lower + MEAL_MINUTES.min, max=upper + MEAL_MINUTES.max)
            pickup_us = upper * (MINUTE // MICROSECOND)
            if open_until is not None and pickup_us >= (open_until - self.clock) // MICROSECOND:
                conflicts.append(hours.evidence("VENUE_CLOSED", open_until.isoformat()))
            if in_window and pickup_us >= (window.ends_at - self.clock) // MICROSECOND:
                conflicts.append(notice("OUTSIDE_SERVICE_WINDOW", window.window_id, "ends_at", window.ends_at.isoformat()))
            if self.time_budget_min is not None and total.max > self.time_budget_min:
                conflicts.append(queue.evidence("OVER_TIME", total))
        return queue, total, facts, tuple(conflicts)

    def _supply(self, bundle: MealBundle):
        state_facts, prices, portions, conflicts = [], [], [], []
        price = 0
        for item in bundle.items:
            state = self.dish_states.get(item.dish_id)
            status = self._fact(item.dish_id, "service_status", state.service_status if state else None, dynamic=True)
            stock = self._fact(item.dish_id, "stock_portions", state.stock_portions if state else None, dynamic=True)
            unit = self._fact(item.dish_id, "price_cents", state.price_cents if state else None, dynamic=True)
            state_facts.extend((status, stock))
            prices.append(unit)
            if unit.current is not None:
                price += unit.current * item.portions
            if stock.current is not None:
                portions.append(stock.current // item.portions)
            if status.current is None:
                conflicts.append(status.evidence("SUPPLY_UNVERIFIED"))
            elif status.current == "SOLD_OUT":
                conflicts.append(status.evidence("SOLD_OUT", status.current))
            elif status.current == "UNAVAILABLE":
                conflicts.append(status.evidence("UNAVAILABLE", status.current))
            elif stock.current is None:
                conflicts.append(stock.evidence("SUPPLY_UNVERIFIED"))
            elif stock.current < item.portions:
                conflicts.append(stock.evidence("INSUFFICIENT_STOCK", stock.current))

        known_price = price if all(item.current is not None for item in prices) else None
        budget = self.inputs.request.constraints.budget_max_cents
        if budget is not None:
            if known_price is None:
                conflicts.extend(item.evidence("PRICE_UNKNOWN") for item in prices if item.current is None)
            elif known_price > budget:
                conflicts.append(prices[0].evidence("OVER_BUDGET", known_price))
        count = min(portions) if len(portions) == len(bundle.items) else None
        return known_price, count, tuple(state_facts), tuple(prices), tuple(conflicts)

    def _nutrition(self, bundle: MealBundle) -> tuple[tuple[FactUse, ...], IntRange | None]:
        if self.inputs.request.preferences.nutrition_goal != "LIGHT":
            return (), None
        facts = tuple(self._fact(item.dish_id, "kcal_range", self.dishes[item.dish_id].kcal_range)
                      for item in bundle.items)
        if any(row.current is None for row in facts):
            return facts, None
        pairs = tuple(zip(facts, bundle.items, strict=True))
        return facts, IntRange(
            min=sum(row.current.min * item.portions for row, item in pairs),
            max=sum(row.current.max * item.portions for row, item in pairs),
        )

    def assess(self, bundle: MealBundle) -> Assessment:
        restrictions, dietary_conflicts = self._restrictions(bundle)
        queue, total, time_facts, time_conflicts = self._handoff(bundle.venue_id)
        price, count, stock_facts, prices, stock_conflicts = self._supply(bundle)
        nutrition, kcal = self._nutrition(bundle)
        return Assessment(
            bundle=bundle, family_id=self.dishes[bundle.anchor_dish_id].family_id,
            taste_tags=frozenset(tag for item in bundle.items for tag in self.dishes[item.dish_id].taste_tags),
            conflicts=sorted_conflicts(dietary_conflicts + time_conflicts + stock_conflicts),
            price=price, queue=queue, total_time=total, portions=count, prices=prices, time_facts=time_facts,
            critical=time_facts + stock_facts + prices + restrictions + (nutrition if kcal is not None else ()),
            nutrition=nutrition, kcal=kcal,
        )

    def candidates(self) -> tuple[Assessment, ...]:
        return tuple(self.assess(bundle) for bundle in sorted(self.inputs.catalog.bundles, key=lambda row: row.bundle_id)
                     if bundle.meal_role == "COMPLETE_MEAL")


def deadline_for(clock_at: datetime, time_budget_min: int | None) -> datetime | None:
    return clock_at + time_budget_min * MINUTE if time_budget_min is not None else None


def minutes_remaining(clock_at: datetime, deadline_at: datetime | None) -> int | None:
    return max(0, (deadline_at - clock_at) // MINUTE) if deadline_at is not None else None
