"""R01–R06 request, evidence, and result contracts for the decision core."""

from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, field_validator, model_validator

from .models import (
    ID, BundleItem, Catalog, Confidence, Count, Freshness, IntRange, Model,
    ServiceWindow, SnapshotRef, Source, SupplySnapshot, Timestamp,
)

Profile = Literal["DEFAULT", "FAST", "VALUE", "EXPLORE"]
OptionLimit = Annotated[int, Field(ge=1, le=4)]
Score = Annotated[int, Field(ge=0, le=1000)]
RequestKeys = Annotated[tuple[ID, ...], BeforeValidator(lambda value: tuple(value) if isinstance(value, list) else value)]
EvidenceCode = Literal[
    "BUDGET_FIT", "TIME_FIT", "TASTE_MATCH", "NEW_FAMILY", "LOWER_QUEUE",
    "RESTRICTIONS_NOT_DECLARED", "ORIGIN_ASSUMED", "SIMULATION_DATA", "LONG_QUEUE",
    "LOW_STOCK", "PRICE_UNKNOWN", "STALE_FACT", "VENUE_CLOSED", "OUTSIDE_SERVICE_WINDOW",
    "HANDOFF_TIME_UNKNOWN", "OVER_BUDGET", "OVER_TIME", "RESTRICTED_INGREDIENT",
    "RESTRICTED_ALLERGEN", "RESTRICTION_UNVERIFIED", "SOLD_OUT", "UNAVAILABLE",
    "INSUFFICIENT_STOCK", "SUPPLY_UNVERIFIED", "REQUESTED_BUNDLE_NOT_REGISTERED",
    "QUEUE_CHANGED", "SUPPLY_RESTORED", "SERVICE_WINDOW_CHANGED", "RANKING_CHANGED",
]
PublicEvidenceValue = bool | int | float | str | tuple[str, ...] | IntRange


class RequestFields(Model):
    @model_validator(mode="before")
    @classmethod
    def omit_instead_of_null(cls, data):
        if isinstance(data, dict) and any(value is None for value in data.values()):
            raise ValueError("optional request fields must be omitted instead of null")
        return data


class Constraints(RequestFields):
    budget_max_cents: Annotated[int, Field(ge=0, le=100000)] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    time_budget_min: Annotated[int, Field(ge=0, le=180)] | None = Field(
        default=None, exclude_if=lambda value: value is None,
    )
    avoid_ingredients: Annotated[RequestKeys, Field(max_length=20)] = ()
    avoid_allergens: Annotated[RequestKeys, Field(max_length=20)] = ()

    @field_validator("avoid_ingredients", "avoid_allergens")
    @classmethod
    def canonical_keys(cls, values):
        return tuple(sorted(set(values)))


class Preferences(RequestFields):
    taste_tags: Annotated[RequestKeys, Field(max_length=10)] = ()
    requested_dish_id: ID | None = Field(default=None, exclude_if=lambda value: value is None)
    profile: Profile = "DEFAULT"
    recent_family_ids: Annotated[RequestKeys, Field(max_length=5)] = ()
    nutrition_goal: Literal["LIGHT"] | None = Field(default=None, exclude_if=lambda value: value is None)

    @field_validator("taste_tags", "recent_family_ids")
    @classmethod
    def canonical_keys(cls, values):
        return tuple(sorted(set(values)))


class DecisionRequest(RequestFields):
    constraints: Constraints = Field(default_factory=Constraints)
    preferences: Preferences = Field(default_factory=Preferences)
    origin_zone_id: ID = "DEMO_DEFAULT"
    max_options: OptionLimit = 2

    @property
    def explicit_max_options(self) -> bool:
        return "max_options" in self.model_fields_set

    @property
    def origin_assumed(self) -> bool:
        return "origin_zone_id" not in self.model_fields_set


class DecisionInput(Model):
    """One resolved catalog, supply snapshot, and its associated service window."""

    request: DecisionRequest
    catalog: Catalog
    snapshot: SupplySnapshot
    window: ServiceWindow | None

    @model_validator(mode="after")
    def aligned_inputs(self):
        if self.catalog.catalog_version != self.snapshot.ref.catalog_version:
            raise ValueError("catalog version does not match the snapshot")
        if self.snapshot.active_window_id != (self.window.window_id if self.window is not None else None):
            raise ValueError("service window does not match the snapshot")
        return self


class Evidence(Model):
    code: EvidenceCode
    entity_id: ID | None
    field: str | None
    source_kind: Source
    source_ref: str | None
    as_of: Timestamp | None
    freshness: Freshness
    value: PublicEvidenceValue | None = Field(default=None, exclude_if=lambda value: value is None)


class MealOption(Model):
    bundle_id: ID
    venue_id: ID
    anchor_dish_id: ID
    label: str
    items: Annotated[tuple[BundleItem, ...], Field(min_length=1, max_length=3)]
    price_cents: Count | None
    queue_min: IntRange | None
    total_time_min: IntRange | None
    available_bundle_portions: Count | None
    match_score: Score
    data_confidence: Confidence
    evidence: tuple[Evidence, ...]
    warnings: tuple[Evidence, ...]


class DecisionBody(Model):
    """Business content returned by evaluate before service-owned identity is attached."""

    snapshot: SnapshotRef
    rules_version: ID
    config_version: ID
    status: Literal["OK", "NO_FEASIBLE_OPTION"]
    primary: MealOption | None
    alternatives: tuple[MealOption, ...]
    requested_option: MealOption | None
    conflicts: tuple[Evidence, ...]
    warnings: tuple[Evidence, ...]

    @property
    def selectable_bundle_ids(self) -> tuple[str, ...]:
        options = (self.primary, *self.alternatives, self.requested_option)
        return tuple(dict.fromkeys(option.bundle_id for option in options if option is not None))

    def bind(self, recommendation_id: str) -> "DecisionResult":
        return DecisionResult(recommendation_id=recommendation_id, **self.model_dump(exclude={"recommendation_id"}))


class DecisionResult(DecisionBody):
    recommendation_id: ID


class Selection(Model):
    bundle_id: ID
    window_id: ID


class ReplanResult(Model):
    previous_recommendation_id: ID
    result: DecisionResult
    outcome: Literal["UNCHANGED", "UPDATED", "NO_FEASIBLE_OPTION"]
    change_reasons: tuple[Evidence, ...]
    selected_bundle_id: ID | None
    selection_state: Literal["NONE", "FEASIBLE", "NEEDS_RESELECT"]
