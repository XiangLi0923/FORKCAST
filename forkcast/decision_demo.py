"""A stateless synthetic showcase for the actual decision engine."""

from functools import lru_cache

from .decision_engine import evaluate
from .decision_models import DecisionInput, DecisionRequest
from .decision_ranking import NEAR_OPTIMAL
from .fixture import load_fixture
from .models import Model, SimulationEvent, SoldOut
from .simulator import Simulator


class DemoRequest(Model):
    request: DecisionRequest
    duck_sold_out: bool = False


@lru_cache(maxsize=1)
def _demo():
    fixture = load_fixture()
    dishes = tuple(
        dish.model_copy(update={"active": True, "w1": dish.w1 or 8})
        if dish.family_id not in {"fish_rice", "egg_rice"} else dish
        for dish in fixture.dishes
    )
    fixture = fixture.model_copy(update={"source_ref": "exploration-demo.v1", "dishes": dishes})
    scenario = fixture.scenario("CLOSED_LOOP")
    simulator = Simulator(fixture, scenario, "demo.exploration")
    return fixture.catalog(scenario), simulator, simulator.initial_frame()


def demonstrate(payload: DemoRequest) -> dict:
    catalog, simulator, frame = _demo()
    if payload.duck_sold_out:
        change = SimulationEvent(
            event_id="demo.exploration.soldout", run_id=simulator.run_id,
            scheduled_at=frame.supply.ref.clock_at, sequence=0,
            event_type="SET_SOLD_OUT", target_id="D_A_DUCK", payload=SoldOut(dish_id="D_A_DUCK"),
        )
        frame, _ = simulator.transition(frame, change, ())
    result = evaluate(DecisionInput(request=payload.request, catalog=catalog, snapshot=frame.supply,
                                    window=simulator.windows[0]))
    venues = {venue.venue_id: venue.name.value for venue in catalog.venues}
    supply = {row.dish_id: row.service_status.value == "AVAILABLE" for row in frame.supply.dish_states}
    rows = []
    for trace in result.candidates:
        facts, score = trace.facts, trace.score
        rows.append({
            "id": facts.bundle.bundle_id,
            "dish_id": facts.bundle.anchor_dish_id,
            "name": facts.bundle.label,
            "venue": venues[facts.bundle.venue_id],
            "available": all(supply.get(item.dish_id, False) for item in facts.bundle.items),
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
    }
