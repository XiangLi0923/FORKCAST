"""Versioned, offline fixture load and run-bound data materialization."""

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import model_validator

from .models import (
    ID, ActivateWindow, BundleItem, Catalog, Confidence, Count, Dish, Fact,
    IngredientPool, IntRange, MealBundle, Model, OriginWalk, PrepResources, Queue,
    ScenarioID, SeedFeedback, SeedRecommendation, SeedSession, SeedState,
    ServiceWindow, SimulationEvent, SnapshotRef, SupplyPlan, TimeRange, Timestamp, Venue,
)


class FixtureVenue(Queue):
    name: str
    location_label: str
    walk_min: IntRange
    service_min: IntRange


class FixtureDish(Model):
    venue_id: ID
    dish_id: ID
    name: str
    family_id: ID
    tag: ID
    w1: Count
    w2: Count
    price_cents: Count
    ingredients: tuple[ID, ...] | None
    active: bool


class FixtureWindow(TimeRange):
    label: Literal["W1", "W2"]


class Scenario(Model):
    scenario_id: ScenarioID
    queue_override: Queue | None
    unknown_allergens_dish_id: ID | None


class DemandSeed(Model):
    dish_id: ID
    preferred: Count
    intended: Count
    dined: Count


class Fixture(Model):
    fixture_version: Literal["v03.2"]
    seed: Literal[20261121]
    source_ref: str
    rules_version: ID
    config_version: ID
    dictionary_version: ID
    starts_at: Timestamp
    origin_zone_id: ID
    windows: tuple[FixtureWindow, FixtureWindow]
    venues: tuple[FixtureVenue, ...]
    dishes: tuple[FixtureDish, ...]
    ingredient_keys: tuple[ID, ...]
    allergen_keys: tuple[ID, ...]
    taste_tags: tuple[ID, ...]
    w2_duck_max_portions: Count
    w2_duck_ingredient_portions: Count
    w2_venue_a_prep_portions: Count
    demand_seed: DemandSeed
    scenarios: tuple[Scenario, ...]

    @model_validator(mode="after")
    def stable_keys(self):
        w1, w2 = self.windows
        if (w1.label, w2.label) != ("W1", "W2") or not (
            w1.starts_at <= self.starts_at < w1.ends_at == w2.starts_at
        ):
            raise ValueError("fixture requires consecutive W1 and W2 and initialization within W1")
        if len(self.scenarios) != 3 or len({scenario.scenario_id for scenario in self.scenarios}) != 3:
            raise ValueError("fixture must register all three distinct scenarios")
        if max(self.demand_seed.preferred, self.demand_seed.dined) > self.demand_seed.intended:
            raise ValueError("seed feedback requires an enrolled virtual session")
        return self

    def scenario(self, scenario_id: ScenarioID) -> Scenario:
        return next(row for row in self.scenarios if row.scenario_id == scenario_id)

    def fact(self, value, as_of: datetime | None = None, valid_until: datetime | None = None) -> dict:
        confidence: Confidence = "UNKNOWN" if value is None else "MEDIUM"
        return {
            "value": value,
            "source_kind": "SYNTHETIC",
            "source_ref": self.source_ref,
            "as_of": self.windows[0].starts_at if as_of is None else as_of,
            "valid_until": valid_until,
            "confidence": confidence,
        }

    def catalog(self, scenario: Scenario) -> Catalog:
        hours = TimeRange(starts_at=self.windows[0].starts_at, ends_at=self.windows[-1].ends_at)
        return Catalog(
            catalog_version=self.fixture_version,
            dictionary_version=self.dictionary_version,
            venues=tuple(Venue(
                venue_id=row.venue_id,
                name=Fact[str](**self.fact(row.name)),
                location_label=Fact[str](**self.fact(row.location_label)),
                opening_hours=Fact[tuple[TimeRange, ...]](**self.fact((hours,))),
                aliases=(),
            ) for row in self.venues),
            dishes=tuple(Dish(
                dish_id=row.dish_id,
                venue_id=row.venue_id,
                name=Fact[str](**self.fact(row.name)),
                family_id=row.family_id,
                taste_tags=(row.tag,),
                serving_label="标准份",
                ingredients=Fact[tuple[ID, ...]](**self.fact(row.ingredients)),
                allergens=Fact[tuple[ID, ...]](**self.fact(
                    () if row.ingredients is not None and row.dish_id != scenario.unknown_allergens_dish_id else None
                )),
                kcal_range=Fact[IntRange](**self.fact(None)),
            ) for row in self.dishes),
            bundles=tuple(MealBundle(
                bundle_id=bundle_id(row.dish_id),
                venue_id=row.venue_id,
                label=row.name,
                anchor_dish_id=row.dish_id,
                items=(BundleItem(dish_id=row.dish_id, portions=1),),
            ) for row in self.dishes),
            origin_walks=tuple(OriginWalk(
                origin_zone_id=self.origin_zone_id,
                venue_id=row.venue_id,
                walk_min=Fact[IntRange](**self.fact(row.walk_min)),
            ) for row in self.venues),
            ingredient_keys=self.ingredient_keys,
            allergen_keys=self.allergen_keys,
            taste_tags=self.taste_tags,
            family_ids=tuple(sorted({row.family_id for row in self.dishes})),
        )

    def scoped_windows(self, run_id: str) -> tuple[ServiceWindow, ...]:
        return tuple(ServiceWindow(window_id=f"{run_id}.{row.label}", **row.model_dump()) for row in self.windows)

    def supply(self, run_id: str, windows: tuple[ServiceWindow, ...]):
        plans, prep, ingredients = [], [], []
        for window in windows:
            for venue in self.venues:
                prep.append(PrepResources(
                    run_id=run_id, window_id=window.window_id, venue_id=venue.venue_id,
                    extra_prep_capacity_portions=self.w2_venue_a_prep_portions
                    if window.label == "W2" and venue.venue_id == "V_A" else 0,
                    reserved_prep_portions=0,
                ))
            for dish in self.dishes:
                baseline = dish.w1 if window.label == "W1" else dish.w2
                special = window.label == "W2" and dish.dish_id == self.demand_seed.dish_id
                plans.append(SupplyPlan(
                    run_id=run_id, window_id=window.window_id, dish_id=dish.dish_id,
                    base_planned_portions=baseline, confirmed_extra_portions=0,
                    max_served_portions=self.w2_duck_max_portions if special else baseline,
                ))
                ingredients.append(IngredientPool(
                    run_id=run_id, window_id=window.window_id, dish_id=dish.dish_id,
                    extra_ingredient_portions=self.w2_duck_ingredient_portions if special else 0,
                    reserved_ingredient_portions=0,
                ))
        return tuple(plans), tuple(prep), tuple(ingredients)

    def boundaries(self, run_id: str, w2: ServiceWindow) -> tuple[SimulationEvent, ...]:
        return tuple(SimulationEvent(
            event_id=f"{run_id}.boundary{index}", run_id=run_id, scheduled_at=when,
            sequence=index, event_type="ACTIVATE_WINDOW", target_id=target,
            payload=ActivateWindow(window_id=target),
        ) for index, (when, target) in enumerate(((w2.starts_at, w2.window_id), (w2.ends_at, None))))

    def seed_state(self, snapshot: SnapshotRef, window: ServiceWindow, real_now: datetime) -> SeedState:
        run = snapshot.run_id
        seed = self.demand_seed
        sessions = tuple(SeedSession(
            principal_id=f"{run}.demo{i}", run_id=run, updated_at=real_now,
        ) for i in range(1, seed.intended + 1))
        recommendations = tuple(SeedRecommendation(
            recommendation_id=f"{run}.recommend{i}", run_id=run, principal_id=session.principal_id,
            snapshot=snapshot, origin_window_id=window.window_id, requested_dish_id=seed.dish_id,
            selectable_bundle_ids=tuple(bundle_id(dish.dish_id) for dish in self.dishes if dish.active and dish.w1),
            rules_version=self.rules_version, config_version=self.config_version,
        ) for i, session in enumerate(sessions, 1))
        feedback = []
        for event_type, count in (("PREFERENCE", seed.preferred), ("INTENT_SELECTED", seed.intended), ("DINED", seed.dined)):
            for rec in recommendations[:count]:
                seq = len(feedback) + 1
                references = {"dish_id": seed.dish_id} if event_type == "PREFERENCE" else {
                    "recommendation_id": rec.recommendation_id, "bundle_id": bundle_id(seed.dish_id),
                }
                feedback.append(SeedFeedback(
                    event_id=f"{run}.signal{seq}", run_id=run, principal_id=rec.principal_id,
                    window_id=window.window_id, occurred_at=snapshot.clock_at, commit_seq=seq,
                    event_type=event_type, **references,
                ))
        return SeedState(sessions=sessions, recommendations=recommendations, feedback=tuple(feedback))


def bundle_id(dish_id: str) -> str:
    return f"B_{dish_id.removeprefix('D_')}"


def load_fixture() -> Fixture:
    return Fixture.model_validate_json((Path(__file__).with_name("data") / "v03.2.json").read_bytes())
