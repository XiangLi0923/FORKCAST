"""Immutable, strict models for the M1 simulation data contract."""

import re
from datetime import datetime
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import AwareDatetime, BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints, model_validator


def _timestamp(value: object) -> object:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value
    ):
        return datetime.fromisoformat(value)
    raise ValueError("expected an RFC 3339 timestamp with time zone")


Timestamp = Annotated[AwareDatetime, BeforeValidator(_timestamp)]
ID = Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")]
Key = Annotated[str, StringConstraints(min_length=8, max_length=128, pattern=r"^[!-~]+$")]
Count = Annotated[int, Field(ge=0, le=2**63 - 1)]
Positive = Annotated[int, Field(ge=1, le=2**63 - 1)]
Mode = Literal["SIMULATION", "LIVE"]
Confidence = Literal["HIGH", "MEDIUM", "LOW", "UNKNOWN"]
Freshness = Literal["FRESH", "STALE", "UNKNOWN"]
Source = Literal["AUTHORIZED", "PUBLIC", "ESTIMATED", "SYNTHETIC", "UNKNOWN"]
VenueStatus = Literal["OPEN", "CLOSED"]
DishStatus = Literal["AVAILABLE", "SOLD_OUT", "UNAVAILABLE"]
EventType = Literal["SIM_SALE", "SET_SOLD_OUT", "SET_QUEUE", "SET_VENUE_STATUS", "ACTIVATE_WINDOW", "ADVANCE_CLOCK"]
PublicEventType = Literal["SET_SOLD_OUT", "SET_QUEUE", "ADVANCE_CLOCK"]
ScenarioID = Literal["CLOSED_LOOP", "TIME_PRESSURE", "PREFERENCE_CONFLICT"]
T = TypeVar("T")


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class IntRange(Model):
    min: Count
    max: Count

    @model_validator(mode="after")
    def ordered(self):
        if self.min > self.max:
            raise ValueError("range minimum exceeds maximum")
        return self


class TimeRange(Model):
    starts_at: Timestamp
    ends_at: Timestamp

    @model_validator(mode="after")
    def ordered(self):
        if self.starts_at >= self.ends_at:
            raise ValueError("window must end after it starts")
        return self


class Fact(Model, Generic[T]):
    value: T | None
    source_kind: Source
    source_ref: str | None
    as_of: Timestamp | None
    valid_until: Timestamp | None
    confidence: Confidence

    @model_validator(mode="after")
    def valid_evidence(self):
        if self.as_of is not None and self.valid_until is not None and self.as_of >= self.valid_until:
            raise ValueError("fact validity must end after its source time")
        if self.value is None and self.confidence != "UNKNOWN":
            raise ValueError("missing value requires UNKNOWN confidence")
        if self.source_kind == "SYNTHETIC" and self.confidence == "HIGH":
            raise ValueError("synthetic evidence cannot be HIGH confidence")
        return self

    def freshness(self, clock_at: datetime, *, dynamic: bool) -> Freshness:
        if self.value is None or (dynamic and (self.as_of is None or self.valid_until is None)):
            return "UNKNOWN"
        if self.as_of is not None and self.as_of > clock_at:
            return "UNKNOWN"
        if self.valid_until is not None and clock_at >= self.valid_until:
            return "STALE"
        return "FRESH"


class Venue(Model):
    venue_id: ID
    name: Fact[str]
    location_label: Fact[str]
    opening_hours: Fact[tuple[TimeRange, ...]]
    aliases: tuple[str, ...]


class Dish(Model):
    dish_id: ID
    venue_id: ID
    name: Fact[str]
    family_id: ID
    taste_tags: tuple[ID, ...]
    serving_label: str
    ingredients: Fact[tuple[ID, ...]]
    allergens: Fact[tuple[ID, ...]]
    kcal_range: Fact[IntRange]


class BundleItem(Model):
    dish_id: ID
    portions: Positive


class MealBundle(Model):
    bundle_id: ID
    venue_id: ID
    label: str
    anchor_dish_id: ID
    items: Annotated[tuple[BundleItem, ...], Field(min_length=1, max_length=3)]
    meal_role: Literal["COMPLETE_MEAL"] = "COMPLETE_MEAL"

    @model_validator(mode="after")
    def valid_items(self):
        dish_ids = {item.dish_id for item in self.items}
        if len(dish_ids) != len(self.items) or self.anchor_dish_id not in dish_ids:
            raise ValueError("bundle requires unique items and an included anchor dish")
        return self


class OriginWalk(Model):
    origin_zone_id: ID
    venue_id: ID
    walk_min: Fact[IntRange]


class Catalog(Model):
    catalog_version: ID
    dictionary_version: ID
    venues: tuple[Venue, ...]
    dishes: tuple[Dish, ...]
    bundles: tuple[MealBundle, ...]
    origin_walks: tuple[OriginWalk, ...]
    ingredient_keys: tuple[ID, ...]
    allergen_keys: tuple[ID, ...]
    taste_tags: tuple[ID, ...]
    family_ids: tuple[ID, ...]

    @model_validator(mode="after")
    def references(self):
        venues = {venue.venue_id for venue in self.venues}
        dishes = {dish.dish_id: dish for dish in self.dishes}
        if len(venues) != len(self.venues) or len(dishes) != len(self.dishes):
            raise ValueError("duplicate catalog keys")
        if len({bundle.bundle_id for bundle in self.bundles}) != len(self.bundles):
            raise ValueError("duplicate bundle keys")
        if len({(walk.origin_zone_id, walk.venue_id) for walk in self.origin_walks}) != len(self.origin_walks):
            raise ValueError("duplicate origin and venue key")
        for dish in self.dishes:
            if dish.venue_id not in venues or dish.family_id not in self.family_ids:
                raise ValueError("dish references unknown venue or family")
            for values, keys in (
                (dish.taste_tags, self.taste_tags),
                (dish.ingredients.value, self.ingredient_keys),
                (dish.allergens.value, self.allergen_keys),
            ):
                if values is not None and not set(values).issubset(keys):
                    raise ValueError("dish references unknown dictionary key")
        for bundle in self.bundles:
            if bundle.venue_id not in venues or any(
                item.dish_id not in dishes or dishes[item.dish_id].venue_id != bundle.venue_id
                for item in bundle.items
            ):
                raise ValueError("bundle contains a missing or cross-venue dish")
        if any(walk.venue_id not in venues for walk in self.origin_walks):
            raise ValueError("walk references unknown venue")
        return self


class ServiceWindow(TimeRange):
    window_id: ID
    label: ID


class VenueState(Model):
    venue_id: ID
    service_status: Fact[VenueStatus]
    queue_min: Fact[IntRange]
    service_min: Fact[IntRange]


class DishState(Model):
    window_id: ID
    dish_id: ID
    service_status: Fact[DishStatus]
    stock_portions: Fact[Count]
    price_cents: Fact[Count]

    @model_validator(mode="after")
    def consistent_inventory(self):
        status, stock = self.service_status.value, self.stock_portions.value
        if (status == "AVAILABLE" and (stock is None or stock <= 0)) or (status == "SOLD_OUT" and stock != 0):
            raise ValueError("available dishes need positive stock and sold-out dishes need zero stock")
        return self


class SupplyPlan(Model):
    run_id: ID
    window_id: ID
    dish_id: ID
    base_planned_portions: Count
    confirmed_extra_portions: Count
    max_served_portions: Count

    @model_validator(mode="after")
    def within_ceiling(self):
        if self.base_planned_portions + self.confirmed_extra_portions > self.max_served_portions:
            raise ValueError("planned supply exceeds dish ceiling")
        return self


class PrepResources(Model):
    run_id: ID
    window_id: ID
    venue_id: ID
    extra_prep_capacity_portions: Count
    reserved_prep_portions: Count

    @model_validator(mode="after")
    def within_capacity(self):
        if self.reserved_prep_portions > self.extra_prep_capacity_portions:
            raise ValueError("prep reservation exceeds capacity")
        return self


class IngredientPool(Model):
    run_id: ID
    window_id: ID
    dish_id: ID
    extra_ingredient_portions: Count
    reserved_ingredient_portions: Count

    @model_validator(mode="after")
    def within_capacity(self):
        if self.reserved_ingredient_portions > self.extra_ingredient_portions:
            raise ValueError("ingredient reservation exceeds capacity")
        return self


class SnapshotRef(Model):
    run_id: ID
    snapshot_id: ID
    revision: Positive
    clock_at: Timestamp
    data_mode: Mode
    catalog_version: ID
    schema_version: Literal["0.3"] = "0.3"


class SupplySnapshot(Model):
    ref: SnapshotRef
    active_window_id: ID | None
    venue_states: tuple[VenueState, ...]
    dish_states: tuple[DishState, ...]

    @model_validator(mode="after")
    def state_keys(self):
        if len({row.venue_id for row in self.venue_states}) != len(self.venue_states):
            raise ValueError("duplicate venue state")
        if len({row.dish_id for row in self.dish_states}) != len(self.dish_states):
            raise ValueError("duplicate dish state")
        if any(row.window_id != self.active_window_id for row in self.dish_states):
            raise ValueError("dish state outside its snapshot window")
        return self


class Sale(Model):
    dish_id: ID
    portions: Positive


class SoldOut(Model):
    dish_id: ID
    reason: Literal["LOSS", "DEPLETION"] = "DEPLETION"


class Queue(Model):
    venue_id: ID
    queue_depth: Annotated[int, Field(ge=0, le=10000)]
    throughput_per_min: Annotated[int, Field(ge=1, le=1000)]

    def estimate(self) -> IntRange:
        base = (self.queue_depth + self.throughput_per_min - 1) // self.throughput_per_min
        return IntRange(min=max(0, base - 1), max=base + 1)


class VenueControl(Model):
    venue_id: ID
    service_status: VenueStatus


class ActivateWindow(Model):
    window_id: ID | None


class AdvanceClock(Model):
    to_clock_at: Timestamp


EventPayload = Sale | SoldOut | Queue | VenueControl | ActivateWindow | AdvanceClock
PAYLOAD_FOR = {
    "SIM_SALE": Sale,
    "SET_SOLD_OUT": SoldOut,
    "SET_QUEUE": Queue,
    "SET_VENUE_STATUS": VenueControl,
    "ACTIVATE_WINDOW": ActivateWindow,
    "ADVANCE_CLOCK": AdvanceClock,
}


def event_target(payload: EventPayload) -> str | None:
    if isinstance(payload, (Sale, SoldOut)):
        return payload.dish_id
    if isinstance(payload, (Queue, VenueControl)):
        return payload.venue_id
    if isinstance(payload, ActivateWindow):
        return payload.window_id
    return None


class SimulationEvent(Model):
    event_id: ID
    run_id: ID
    scheduled_at: Timestamp
    sequence: Count
    event_type: EventType
    target_id: ID | None
    payload: EventPayload

    @model_validator(mode="after")
    def matching_payload(self):
        if type(self.payload) is not PAYLOAD_FOR[self.event_type] or self.target_id != event_target(self.payload):
            raise ValueError("event payload or target does not match event type")
        return self


class AuditRecord(Model):
    run_id: ID
    audit_id: ID
    action: Literal["SIM_CONTROL_APPLIED", "OPS_CONFIRMED", "OPS_APPLIED"]
    actor_type: Literal["OPS", "SYSTEM"]
    actor_id: ID
    result_code: ID
    at: Timestamp
    venue_id: ID | None
    snapshot_id: ID
    proposal_id: ID | None = None


class SnapshotFrame(Model):
    """Persist one supply snapshot with its same-version private simulation state."""

    supply: SupplySnapshot
    queues: tuple[Queue, ...]
    plans: tuple[SupplyPlan, ...]
    prep: tuple[PrepResources, ...]
    ingredients: tuple[IngredientPool, ...]


class SeedSession(Model):
    principal_id: ID
    run_id: ID
    role: Literal["STUDENT"] = "STUDENT"
    session_kind: Literal["DEMO"] = "DEMO"
    share_for_ops: Literal[True] = True
    consent_revision: Literal[1] = 1
    updated_at: Timestamp


class SeedRecommendation(Model):
    """Fixture input and selectable references for later recommendation replay."""

    recommendation_id: ID
    run_id: ID
    principal_id: ID
    snapshot: SnapshotRef
    origin_window_id: ID
    requested_dish_id: ID
    selectable_bundle_ids: tuple[ID, ...]
    rules_version: ID
    config_version: ID


class SeedFeedback(Model):
    event_id: ID
    run_id: ID
    principal_id: ID
    window_id: ID
    occurred_at: Timestamp
    commit_seq: Positive
    stream: Literal["SIMULATED"] = "SIMULATED"
    event_type: Literal["PREFERENCE", "INTENT_SELECTED", "DINED"]
    consent_revision: Literal[1] = 1
    recommendation_id: ID | None = None
    bundle_id: ID | None = None
    dish_id: ID | None = None

    @model_validator(mode="after")
    def references(self):
        if self.event_type == "PREFERENCE":
            valid = self.dish_id is not None and self.recommendation_id is None and self.bundle_id is None
        else:
            valid = self.dish_id is None and self.recommendation_id is not None and self.bundle_id is not None
        if not valid:
            raise ValueError("invalid fixture signal references")
        return self


class SeedState(Model):
    sessions: tuple[SeedSession, ...]
    recommendations: tuple[SeedRecommendation, ...]
    feedback: tuple[SeedFeedback, ...]

    @property
    def signals_revision(self) -> int:
        return len(self.feedback)
