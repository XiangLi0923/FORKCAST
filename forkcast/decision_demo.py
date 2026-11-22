"""Run the decision engine and supply simulation against an isolated synthetic scenario."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from .decision_engine import evaluate
from .decision_models import DecisionInput, DecisionRequest
from .decision_ranking import NEAR_OPTIMAL
from .demand_insights import DemandInsight, DemandVisibility, MIN_VISIBLE_SUBJECTS
from .errors import Fault
from .fixture import FixtureDish, FixtureVenue, load_fixture
from .models import ID, AdvanceClock, Model, SimulationEvent, SoldOut
from .ops_analysis import analyze_supply
from .ops_execution import reserve
from .simulator import Simulator


class DemoRequest(Model):
    request: DecisionRequest
    duck_sold_out: bool = False
    stage: Literal["CURRENT", "W2_BASE", "W2_APPROVED"] = "CURRENT"


class DemoOperationsRequest(Model):
    action: Literal["SUMMARIZE", "PLAN"]
    chosen_dish_id: ID
    share_intent: bool = False


class ExplorationMenu(Model):
    venues: tuple[FixtureVenue, ...]
    dishes: tuple[FixtureDish, ...]


@lru_cache(maxsize=1)
def _demo():
    fixture = load_fixture()
    menu = ExplorationMenu.model_validate_json(
        (Path(__file__).with_name("data") / "exploration.v2.json").read_bytes()
    )
    original_dishes = tuple(
        dish.model_copy(update={"active": True, "w1": dish.w1 or 8,
                                "w2": dish.w2 or (0 if dish.dish_id == fixture.demand_seed.dish_id else 8)})
        if dish.family_id not in {"fish_rice", "egg_rice"} else dish
        for dish in fixture.dishes
    )
    fixture = fixture.model_copy(update={
        "source_ref": "exploration-demo.v2",
        "venues": fixture.venues + menu.venues,
        "dishes": original_dishes + menu.dishes,
    })
    scenario = fixture.scenario("CLOSED_LOOP")
    simulator = Simulator(fixture, scenario, "demo.exploration")

    current = simulator.initial_frame()
    seed = fixture.demand_seed
    venue = next(dish.venue_id for dish in fixture.dishes if dish.dish_id == seed.dish_id)
    w1, w2 = simulator.windows
    channels = {"preferred_sessions": seed.preferred, "intended_sessions": seed.intended,
                "dined_sessions": seed.dined}
    visible = {key: "VISIBLE" if count >= MIN_VISIBLE_SUBJECTS else "SUPPRESSED"
               for key, count in channels.items()}
    demand = DemandInsight(
        run_id=simulator.run_id, reference_window_id=w1.window_id, venue_id=venue, dish_id=seed.dish_id,
        observed_until=fixture.starts_at, signals_revision=0, visibility=DemandVisibility(**visible),
        **{key: count if visible[key] == "VISIBLE" else None for key, count in channels.items()},
    )
    plan = next(row for row in current.plans if row.window_id == w2.window_id and row.dish_id == seed.dish_id)
    prep = next(row for row in current.prep if row.window_id == w2.window_id and row.venue_id == venue)
    ingredient = next(row for row in current.ingredients if row.window_id == w2.window_id and row.dish_id == seed.dish_id)
    analysis = analyze_supply(demand, plan, prep, ingredient)
    amount = analysis.feasible_extra_portions
    approved = reserve(current, w2.window_id, venue, seed.dish_id, amount) if amount else current
    advance = SimulationEvent(
        event_id="demo.exploration.advance", run_id=simulator.run_id, scheduled_at=fixture.starts_at,
        sequence=0, event_type="ADVANCE_CLOCK", target_id=None, payload=AdvanceClock(to_clock_at=w2.starts_at),
    )
    scheduled = fixture.boundaries(simulator.run_id, w2)
    stages = {
        "CURRENT": (current.supply, w1),
        "W2_BASE": (simulator.transition(current, advance, scheduled)[0].supply, w2),
        "W2_APPROVED": (simulator.transition(approved, advance, scheduled)[0].supply, w2),
    }
    sale = SimulationEvent(
        event_id="demo.exploration.soldout", run_id=simulator.run_id, scheduled_at=fixture.starts_at,
        sequence=0, event_type="SET_SOLD_OUT", target_id=seed.dish_id, payload=SoldOut(dish_id=seed.dish_id),
    )
    sold_out = simulator.transition(current, sale, ())[0].supply
    return fixture.catalog(scenario), stages, sold_out, demand, analysis, (plan, prep, ingredient)


def demonstrate(payload: DemoRequest) -> dict:
    catalog, stages, sold_out, demand, analysis, _ = _demo()
    snapshot, window = stages[payload.stage]
    if payload.stage == "CURRENT" and payload.duck_sold_out:
        snapshot = sold_out
    result = evaluate(DecisionInput(request=payload.request, catalog=catalog, snapshot=snapshot, window=window))
    venues = {venue.venue_id: venue.name.value for venue in catalog.venues}
    supply = {row.dish_id: row for row in snapshot.dish_states}
    rows = []
    for trace in result.candidates:
        facts, score = trace.facts, trace.score
        rows.append({
            "id": facts.bundle.bundle_id,
            "dish_id": facts.bundle.anchor_dish_id,
            "name": facts.bundle.label,
            "venue": venues[facts.bundle.venue_id],
            "available": all(supply[item.dish_id].service_status.value == "AVAILABLE"
                             for item in facts.bundle.items),
            "price_cents": facts.price,
            "time": facts.total_time.model_dump() if facts.total_time else None,
            "queue": facts.queue.current.model_dump() if facts.queue.current else None,
            "score": score.match_score if score else None,
            "blocks": list(dict.fromkeys(reason.code for reason in facts.conflicts)),
            "factors": [
                {"name": item.name, "weight": item.weight,
                 "points": round(float(item.value * item.weight), 2), "estimated": item.neutral_default}
                for item in (score.factors if score else ())
            ],
        })
    return {
        "result": result.body.model_dump(mode="json"),
        "near_optimal": NEAR_OPTIMAL,
        "candidates": rows,
        "venue_count": len(venues),
        "stage": payload.stage,
        "supply_comparison": {
            "preference": demand.preferred_sessions,
            "intent": demand.intended_sessions,
            "reported_meal": demand.dined_sessions,
            "proposal_portions": analysis.feasible_extra_portions,
            "current_duck_stock": supply[demand.dish_id].stock_portions.value,
        },
    }


def demonstrate_operations(payload: DemoOperationsRequest) -> dict:
    """Calculate a rehearsal summary and plan without writing to the contest run."""
    catalog, _, _, demand, _, resources = _demo()
    dishes = {row.dish_id: row for row in catalog.dishes}
    if payload.chosen_dish_id not in dishes:
        raise Fault("INVALID_INPUT", "Unknown demo dish.")
    counts_toward_duck = payload.share_intent and payload.chosen_dish_id == demand.dish_id
    if counts_toward_duck:
        demand = demand.model_copy(update={"intended_sessions": demand.intended_sessions + 1})
    data = {
        "dish": dishes[demand.dish_id].name.value,
        "chosen_dish": dishes[payload.chosen_dish_id].name.value,
        "included": counts_toward_duck,
        "signals": {
            "preference": demand.preferred_sessions,
            "intent": demand.intended_sessions,
            "reported_meal": demand.dined_sessions,
        },
    }
    if payload.action == "PLAN":
        data["plan"] = analyze_supply(demand, *resources).model_dump(mode="json")
    return data
