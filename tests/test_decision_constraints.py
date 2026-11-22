import unittest
from datetime import timedelta

from pydantic import ValidationError

from forkcast.decision_constraints import ConstraintEvaluator
from forkcast.decision_models import Constraints, DecisionInput, DecisionRequest, Preferences
from forkcast.fixture import load_fixture
from forkcast.models import BundleItem, Fact, IntRange, MealBundle
from forkcast.simulator import Simulator


DUCK = "D_A_DUCK"
CHICKEN = "D_B_CHICKEN"
TOFU = "D_A_TOFU"


def scene(name="CLOSED_LOOP", request=None):
    fixture = load_fixture()
    simulator = Simulator(fixture, fixture.scenario(name), "decisiontest")
    return simulator, DecisionInput(
        request=request or DecisionRequest(), catalog=fixture.catalog(simulator.scenario),
        snapshot=simulator.initial_frame().supply, window=simulator.windows[0],
    )


def patch_dish(data, dish_id, **fields):
    dishes = tuple(row.model_copy(update=fields) if row.dish_id == dish_id else row
                   for row in data.snapshot.dish_states)
    return data.model_copy(update={"snapshot": data.snapshot.model_copy(update={"dish_states": dishes})})


def assessment(data, bundle_id):
    evaluator = ConstraintEvaluator(data, data.request.constraints.time_budget_min)
    return next(row for row in evaluator.candidates() if row.bundle.bundle_id == bundle_id)


def codes(assessed):
    return {item.code for item in assessed.conflicts}


class DecisionConstraintTests(unittest.TestCase):
    def test_d02_budget_boundary_and_unknown_price(self):
        for limit, expected in ((1600, set()), (1599, {"OVER_BUDGET"})):
            _, data = scene(request=DecisionRequest(constraints=Constraints(budget_max_cents=limit)))
            self.assertEqual(codes(assessment(data, "B_A_DUCK")), expected)
        duck = data.snapshot.dish_states[0]
        unknown = duck.price_cents.model_copy(update={"value": None, "confidence": "UNKNOWN"})
        self.assertEqual(codes(assessment(patch_dish(data, DUCK, price_cents=unknown), "B_A_DUCK")),
                         {"PRICE_UNKNOWN"})

    def test_d03_time_pressure_and_exact_upper_limit(self):
        for limit, feasible in ((25, {"B_B_CHICKEN"}), (21, set())):
            _, data = scene("TIME_PRESSURE", DecisionRequest(constraints=Constraints(time_budget_min=limit)))
            self.assertEqual({row.bundle.bundle_id for row in ConstraintEvaluator(data, limit).candidates()
                              if not row.conflicts}, feasible)
        state = data.snapshot.venue_states[0]
        adjusted = state.model_copy(update={"queue_min": state.queue_min.model_copy(
            update={"value": IntRange(min=6, max=8)},
        )})
        _, data = scene("TIME_PRESSURE", DecisionRequest(constraints=Constraints(time_budget_min=25)))
        snap = data.snapshot.model_copy(update={"venue_states": (adjusted, data.snapshot.venue_states[1])})
        result = assessment(data.model_copy(update={"snapshot": snap}), "B_A_DUCK")
        self.assertEqual(result.total_time.max, 25)
        self.assertEqual(result.conflicts, ())

    def test_d03_latest_pickup_equal_to_window_end_is_blocked(self):
        _, data = scene()
        late = data.snapshot.ref.clock_at + timedelta(minutes=12)
        snap = data.snapshot.model_copy(update={"ref": data.snapshot.ref.model_copy(update={"clock_at": late})})
        self.assertIn("OUTSIDE_SERVICE_WINDOW", codes(assessment(
            data.model_copy(update={"snapshot": snap}), "B_A_DUCK",
        )))

    def test_d04_known_unknown_and_expired_allergen_facts(self):
        request = DecisionRequest(constraints=Constraints(avoid_allergens=("peanut",)),
                                  preferences=Preferences(requested_dish_id=DUCK))
        sim, data = scene("PREFERENCE_CONFLICT", request)
        self.assertEqual(codes(assessment(data, "B_A_DUCK")), {"RESTRICTION_UNVERIFIED"})
        self.assertEqual(codes(assessment(data, "B_B_CHICKEN")), set())
        for allergen, expected in ((("peanut",), "RESTRICTED_ALLERGEN"), ((), "RESTRICTION_UNVERIFIED")):
            source = sim.fixture.fact(allergen, valid_until=data.snapshot.ref.clock_at)
            catalog = data.catalog.model_copy(update={"dishes": tuple(
                dish.model_copy(update={"allergens": Fact[tuple[str, ...]](**source)})
                if dish.dish_id == DUCK else dish for dish in data.catalog.dishes
            )})
            result = assessment(data.model_copy(update={"catalog": catalog}), "B_A_DUCK")
            self.assertEqual(codes(result), {expected})
            self.assertIsNone(result.conflicts[0].value)

        ingredients = data.model_copy(update={"request": DecisionRequest(
            constraints=Constraints(avoid_ingredients=("duck",)),
        )})
        self.assertEqual(codes(assessment(ingredients, "B_A_DUCK")), {"RESTRICTED_INGREDIENT"})

    def test_d06_approved_two_component_quantities_and_origin(self):
        sim, data = scene()
        available = data.snapshot.dish_states[0].service_status
        tofu = next(row for row in data.snapshot.dish_states if row.dish_id == TOFU)
        data = patch_dish(data, TOFU, service_status=available, stock_portions=tofu.stock_portions.model_copy(
            update={"value": 3},
        ), price_cents=tofu.price_cents.model_copy(update={"value": 200}))
        bundle = MealBundle(bundle_id="combo", venue_id="V_A", anchor_dish_id=DUCK, label="Combo",
                            items=(BundleItem(dish_id=DUCK, portions=1), BundleItem(dish_id=TOFU, portions=2)))
        catalog = data.catalog.model_copy(update={"bundles": (bundle,)})
        data = data.model_copy(update={"catalog": catalog})
        result = assessment(data, "combo")
        self.assertEqual((result.price, result.portions, result.conflicts), (2000, 1, ()))
        self.assertEqual(codes(assessment(patch_dish(data, TOFU, stock_portions=tofu.stock_portions.model_copy(
            update={"value": 1},
        )), "combo")), {"INSUFFICIENT_STOCK"})
        unavailable = available.model_copy(update={"value": "SOLD_OUT"})
        self.assertEqual(codes(assessment(patch_dish(data, TOFU, service_status=unavailable,
                                                    stock_portions=tofu.stock_portions), "combo")), {"SOLD_OUT"})
        raw = data.catalog.model_dump()
        raw["bundles"] = (bundle.model_copy(update={"items": (bundle.items[0], BundleItem(
            dish_id=CHICKEN, portions=2,
        ))}).model_dump(),)
        with self.assertRaises(ValidationError):
            type(data.catalog).model_validate(raw)
