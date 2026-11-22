import unittest
from datetime import timedelta
from fractions import Fraction

from forkcast.decision_engine import evaluate, recheck_selection, replan
from forkcast.decision_models import Constraints, DecisionRequest, DecisionResult, Preferences, Selection
from forkcast.errors import Fault
from forkcast.models import AdvanceClock, Fact, IntRange, Queue, SimulationEvent, SoldOut, event_target

from tests.test_decision_constraints import DUCK, patch_dish, scene


def shown(body):
    return body.primary, *body.alternatives


def report_codes(rows):
    return tuple(row.code for row in rows)


def command(sim, event_type, payload, at):
    return SimulationEvent(
        event_id=f"{sim.run_id}.testcommand", run_id=sim.run_id, scheduled_at=at,
        sequence=20, event_type=event_type, target_id=event_target(payload), payload=payload,
    )


def advance(sim, frame, at):
    event = command(sim, "ADVANCE_CLOCK", AdvanceClock(to_clock_at=at), at)
    scheduled = sim.fixture.boundaries(sim.run_id, sim.windows[-1])
    return sim.transition(frame, event, scheduled)[0]


class DecisionEngineTests(unittest.TestCase):
    def test_d01_defaults_queue_evidence_and_public_contract(self):
        _, data = scene()
        result = evaluate(data)
        self.assertEqual(result.body.status, "OK")
        self.assertEqual((result.body.primary.bundle_id, result.body.alternatives[0].bundle_id),
                         ("B_A_DUCK", "B_B_CHICKEN"))
        self.assertEqual(report_codes(result.body.warnings),
                         ("RESTRICTIONS_NOT_DECLARED", "ORIGIN_ASSUMED", "SIMULATION_DATA"))
        self.assertEqual([row.entity_id for row in result.body.primary.evidence], ["V_A", "V_B"])
        self.assertEqual(report_codes(result.body.primary.evidence), ("LOWER_QUEUE", "LOWER_QUEUE"))
        self.assertEqual([row.value.max for row in result.body.primary.evidence], [3, 5])
        self.assertNotIn("LOWER_QUEUE", report_codes(result.body.alternatives[0].evidence))
        self.assertIn("LOW_STOCK", report_codes(result.body.primary.warnings))
        wire = result.body.bind("service-owned-id")
        self.assertEqual(DecisionResult.model_validate_json(wire.model_dump_json()), wire)
        self.assertEqual(wire.selectable_bundle_ids, ("B_A_DUCK", "B_B_CHICKEN"))

    def test_e01_personalized_approved_scores_and_truthful_reasons(self):
        request = DecisionRequest(constraints=Constraints(budget_max_cents=2000, time_budget_min=45),
                                  preferences=Preferences(requested_dish_id=DUCK))
        _, data = scene(request=request)
        result = evaluate(data)
        self.assertEqual([row.match_score for row in shown(result.body)], [644, 480])
        self.assertEqual(result.body.requested_option, result.body.primary)
        self.assertEqual(report_codes(result.body.primary.evidence), ("BUDGET_FIT", "TIME_FIT"))
        self.assertEqual([row.value for row in result.body.primary.evidence], [1600, IntRange(min=12, max=20)])
        breakdown = result.trace("B_A_DUCK").score
        self.assertEqual(tuple(factor.value for factor in breakdown.factors),
                         (Fraction(1), Fraction(25, 45), Fraction(1, 5), Fraction(1, 2), Fraction(1, 2)))
        self.assertEqual(breakdown.match_score, result.body.primary.match_score)
        self.assertIsNone(result.trace("B_A_TOFU").score)
        self.assertIn("UNAVAILABLE", report_codes(result.trace("B_A_TOFU").facts.conflicts))

    def test_r02_unknown_nutrition_is_neutral_and_does_not_lower_confidence(self):
        _, data = scene(request=DecisionRequest(preferences=Preferences(nutrition_goal="LIGHT")))
        result = evaluate(data)
        duck = result.trace("B_A_DUCK")
        factor = duck.score.factors[-1]
        self.assertEqual(factor.value, Fraction(1, 2))
        self.assertTrue(factor.neutral_default)
        self.assertIsNone(duck.facts.nutrition[0].current)
        self.assertEqual(result.body.primary.data_confidence, "MEDIUM")

    def test_r06_known_optional_nutrition_affects_only_enabled_scoring(self):
        sim, data = scene()
        calories = Fact[IntRange](**sim.fixture.fact(IntRange(min=350, max=400))).model_copy(update={"confidence": "LOW"})
        catalog = data.catalog.model_copy(update={"dishes": tuple(
            dish.model_copy(update={"kcal_range": calories}) if dish.dish_id == DUCK else dish
            for dish in data.catalog.dishes
        )})
        known = data.model_copy(update={"catalog": catalog})
        self.assertEqual(evaluate(known).body.primary.data_confidence, "MEDIUM")
        light = known.model_copy(update={"request": DecisionRequest(preferences=Preferences(nutrition_goal="LIGHT"))})
        result = evaluate(light)
        self.assertEqual(result.trace("B_A_DUCK").score.factors[-1].value, Fraction(1))
        self.assertEqual(result.body.primary.data_confidence, "LOW")
        self.assertEqual(result.body.primary.match_score - evaluate(known).body.primary.match_score, 50)

    def test_d08_expired_or_unknown_supply_facts_do_not_become_current(self):
        _, data = scene(request=DecisionRequest(preferences=Preferences(requested_dish_id=DUCK)))
        stock = data.snapshot.dish_states[0].stock_portions
        for evidence, expected in (
            (stock.model_copy(update={"valid_until": data.snapshot.ref.clock_at}), "STALE"),
            (stock.model_copy(update={"valid_until": None}), "UNKNOWN"),
            (stock.model_copy(update={"as_of": data.snapshot.ref.clock_at + timedelta(minutes=1)}), "UNKNOWN"),
        ):
            with self.subTest(expected=expected):
                result = evaluate(patch_dish(data, DUCK, stock_portions=evidence))
                self.assertEqual(result.body.primary.bundle_id, "B_B_CHICKEN")
                self.assertIsNone(result.trace("B_A_DUCK").score)
                self.assertEqual([(row.code, row.freshness, row.as_of) for row in result.body.conflicts],
                                 [("SUPPLY_UNVERIFIED", expected, evidence.as_of)])

    def test_r06_stale_price_excluded_from_value_but_preserves_provenance(self):
        _, data = scene()
        price = data.snapshot.dish_states[0].price_cents
        stale = price.model_copy(update={"valid_until": data.snapshot.ref.clock_at})
        result = evaluate(patch_dish(data, DUCK, price_cents=stale))
        duck = next(row for row in shown(result.body) if row.bundle_id == "B_A_DUCK")
        self.assertIsNone(duck.price_cents)
        self.assertEqual(duck.data_confidence, "LOW")
        self.assertEqual(result.trace("B_A_DUCK").score.factors[2].value, Fraction(1, 2))
        self.assertIn("PRICE_UNKNOWN", report_codes(duck.warnings))
        self.assertIn("STALE_FACT", report_codes(duck.warnings))

    def test_d08_lower_confidence_alone_keeps_the_match_score(self):
        _, data = scene()
        original = evaluate(data)
        duck = data.snapshot.dish_states[0]
        price = duck.price_cents.model_copy(update={"confidence": "LOW"})
        result = evaluate(patch_dish(data, DUCK, price_cents=price))
        self.assertEqual(result.trace("B_A_DUCK").score, original.trace("B_A_DUCK").score)
        self.assertEqual(result.body.primary.data_confidence, "LOW")
        self.assertEqual(original.body.primary.data_confidence, "MEDIUM")

    def test_d07_long_queue_requested_option_does_not_consume_limit(self):
        request = DecisionRequest(preferences=Preferences(requested_dish_id=DUCK, profile="FAST"), max_options=1)
        _, data = scene("PREFERENCE_CONFLICT", request)
        result = evaluate(data).body
        self.assertEqual(result.primary.bundle_id, "B_B_CHICKEN")
        self.assertEqual(result.alternatives, ())
        self.assertEqual(result.requested_option.bundle_id, "B_A_DUCK")
        self.assertIn("LONG_QUEUE", report_codes(result.requested_option.warnings))
        self.assertEqual(result.selectable_bundle_ids, ("B_B_CHICKEN", "B_A_DUCK"))

    def test_r01_r04_unregistered_and_unknown_request_are_distinct(self):
        request = DecisionRequest(preferences=Preferences(requested_dish_id=DUCK))
        _, data = scene(request=request)
        catalog = data.catalog.model_copy(update={"bundles": tuple(row for row in data.catalog.bundles
                                                                   if row.anchor_dish_id != DUCK)})
        body = evaluate(data.model_copy(update={"catalog": catalog})).body
        self.assertIsNone(body.requested_option)
        self.assertEqual(report_codes(body.conflicts), ("REQUESTED_BUNDLE_NOT_REGISTERED",))
        bad_requests = (
            DecisionRequest(preferences=Preferences(requested_dish_id="unknown")),
            DecisionRequest(preferences=Preferences(taste_tags=("unknown",))),
            DecisionRequest(origin_zone_id="unknown"),
            DecisionRequest(constraints=Constraints(avoid_allergens=("unknown",))),
        )
        for wrong in bad_requests:
            with self.subTest(wrong=wrong), self.assertRaises(Fault) as rejected:
                evaluate(data.model_copy(update={"request": wrong}))
            self.assertEqual(rejected.exception.code, "INVALID_INPUT")

    def test_d04_high_preference_cannot_override_allergen(self):
        req = DecisionRequest(preferences=Preferences(requested_dish_id=DUCK, taste_tags=("rice",)),
                              constraints=Constraints(avoid_allergens=("peanut",)))
        _, data = scene("PREFERENCE_CONFLICT", req)
        result = evaluate(data)
        self.assertEqual(result.body.primary.bundle_id, "B_B_CHICKEN")
        self.assertEqual(report_codes(result.body.conflicts), ("RESTRICTION_UNVERIFIED",))
        self.assertIsNone(result.trace("B_A_DUCK").score)
        self.assertNotIn("peanut", result.body.bind("safe").model_dump_json())

    def test_d07_no_feasible_returns_distinct_prioritized_diagnostics(self):
        request = DecisionRequest(constraints=Constraints(time_budget_min=21),
                                  preferences=Preferences(requested_dish_id=DUCK))
        _, data = scene("TIME_PRESSURE", request)
        result = evaluate(data).body
        self.assertEqual(result.status, "NO_FEASIBLE_OPTION")
        self.assertIsNone(result.primary)
        self.assertEqual(result.alternatives, ())
        self.assertIsNone(result.requested_option)
        self.assertEqual(report_codes(result.conflicts), ("OVER_TIME", "UNAVAILABLE"))

    def test_d07_closed_venues_and_unknown_handoff_block_recommendations(self):
        _, data = scene(request=DecisionRequest(preferences=Preferences(requested_dish_id=DUCK)))
        venue_states = tuple(row.model_copy(update={"service_status": row.service_status.model_copy(
            update={"value": "CLOSED"},
        )}) for row in data.snapshot.venue_states)
        closed = data.model_copy(update={"snapshot": data.snapshot.model_copy(update={"venue_states": venue_states})})
        result = evaluate(closed).body
        self.assertEqual(result.status, "NO_FEASIBLE_OPTION")
        self.assertEqual(result.conflicts[0].code, "VENUE_CLOSED")
        duck = data.snapshot.venue_states[0]
        unknown_queue = duck.queue_min.model_copy(update={"value": None, "confidence": "UNKNOWN"})
        states = (duck.model_copy(update={"queue_min": unknown_queue}), data.snapshot.venue_states[1])
        unknown = data.model_copy(update={"snapshot": data.snapshot.model_copy(update={"venue_states": states})})
        result = evaluate(unknown).body
        self.assertEqual(result.primary.bundle_id, "B_B_CHICKEN")
        self.assertEqual(report_codes(result.conflicts), ("HANDOFF_TIME_UNKNOWN",))

    def test_same_snapshot_recomputes_exact_scores_order_and_explanations(self):
        _, data = scene()
        first = evaluate(data)
        self.assertEqual(evaluate(data), first)
        reverse = data.catalog.model_copy(update={"bundles": tuple(reversed(data.catalog.bundles))})
        repeat = evaluate(data.model_copy(update={"catalog": reverse}))
        self.assertEqual(repeat.body.bind("same").model_dump_json(), first.body.bind("same").model_dump_json())
        self.assertEqual(repeat.candidates, first.candidates)


class ReplanEngineTests(unittest.TestCase):
    def test_r05_queue_change_on_old_primary_explains_switch(self):
        sim, data = scene()
        original = evaluate(data)
        queue = Queue(venue_id="V_A", queue_depth=16, throughput_per_min=2)
        event = command(sim, "SET_QUEUE", queue, data.snapshot.ref.clock_at)
        latest, _ = sim.transition(sim.initial_frame(), event, ())
        result = replan(original, latest.supply, sim.windows[0])
        self.assertEqual(original.body.primary.bundle_id, "B_A_DUCK")
        self.assertEqual(result.evaluation.body.primary.bundle_id, "B_B_CHICKEN")
        self.assertEqual([(e.code, e.entity_id, e.value.max) for e in result.change_reasons],
                         [("QUEUE_CHANGED", "V_A", 9), ("QUEUE_CHANGED", "V_A", 3)])

    def test_r05_default_replan_expands_to_four_when_enough_are_feasible(self):
        sim, data = scene()
        duck = data.snapshot.dish_states[0]
        for dish_id in ("D_A_TOFU", "D_A_EGG"):
            data = patch_dish(data, dish_id, service_status=duck.service_status, stock_portions=duck.stock_portions)
        previous = evaluate(data)
        self.assertEqual(len(shown(previous.body)), 2)
        result = replan(previous, data.snapshot, sim.windows[0])
        self.assertEqual(len(shown(result.evaluation.body)), 4)
        self.assertEqual(result.outcome, "UPDATED")

    def test_d09_sold_out_replan_and_preflight_keep_original_choice(self):
        request = DecisionRequest(constraints=Constraints(budget_max_cents=2000, time_budget_min=45),
                                  preferences=Preferences(requested_dish_id=DUCK))
        sim, data = scene(request=request)
        previous = evaluate(data)
        initial = sim.initial_frame()
        now = data.snapshot.ref.clock_at
        later = advance(sim, initial, now + timedelta(minutes=1))
        sold = command(sim, "SET_SOLD_OUT", SoldOut(dish_id=DUCK), later.supply.ref.clock_at)
        latest, _ = sim.transition(later, sold, ())
        selected = Selection(bundle_id="B_A_DUCK", window_id=sim.windows[0].window_id)
        response = replan(previous, latest.supply, sim.windows[0], selected=selected)
        self.assertEqual((response.outcome, response.selection_state, response.selected_bundle_id),
                         ("UPDATED", "NEEDS_RESELECT", "B_A_DUCK"))
        self.assertEqual(response.evaluation.body.primary.bundle_id, "B_B_CHICKEN")
        self.assertEqual(response.evaluation.deadline_at, previous.deadline_at)
        self.assertEqual(response.evaluation.trace("B_B_CHICKEN").score.factors[1].value, Fraction(22, 44))
        self.assertEqual([(row.code, row.as_of) for row in response.change_reasons],
                         [("SOLD_OUT", now + timedelta(minutes=1)), ("SOLD_OUT", now)])
        self.assertEqual(response.bind("old", "new").result.primary.bundle_id, "B_B_CHICKEN")
        with self.assertRaises(Fault) as error:
            recheck_selection(previous, "B_A_DUCK", latest.supply, sim.windows[0])
        self.assertEqual(error.exception.code, "STALE_SELECTION")
        self.assertEqual(error.exception.snapshot, latest.supply.ref)
        recheck_selection(previous, "B_B_CHICKEN", latest.supply, sim.windows[0])

    def test_e01_w2_approved_scores_with_confirmed_supply_input_only(self):
        req = DecisionRequest(constraints=Constraints(budget_max_cents=2000, time_budget_min=45),
                              preferences=Preferences(requested_dish_id=DUCK))
        sim, data = scene(request=req)
        previous = evaluate(data)
        w2 = sim.windows[1]
        second = advance(sim, sim.initial_frame(), w2.starts_at)
        control = replan(previous, second.supply, w2)
        self.assertEqual(control.evaluation.body.primary.bundle_id, "B_B_CHICKEN")
        self.assertEqual(control.evaluation.body.primary.match_score, 383)
        duck = next(row for row in second.supply.dish_states if row.dish_id == DUCK)
        w2_data = patch_dish(data.model_copy(update={"snapshot": second.supply}), DUCK,
                             service_status=duck.service_status.model_copy(update={"value": "AVAILABLE"}),
                             stock_portions=duck.stock_portions.model_copy(update={"value": 3}))
        result = replan(previous, w2_data.snapshot, w2,
                        selected=Selection(bundle_id="B_B_CHICKEN", window_id=sim.windows[0].window_id))
        self.assertEqual([row.match_score for row in shown(result.evaluation.body)], [555, 383])
        self.assertEqual(result.selection_state, "NEEDS_RESELECT")
        self.assertIn("SERVICE_WINDOW_CHANGED", report_codes(result.change_reasons))
        sold_event = command(sim, "SET_SOLD_OUT", SoldOut(dish_id=DUCK), data.snapshot.ref.clock_at)
        sold_frame, _ = sim.transition(sim.initial_frame(), sold_event, ())
        lost_supply = replan(previous, sold_frame.supply, sim.windows[0]).evaluation
        later_frame = advance(sim, sold_frame, w2.starts_at)
        restored = patch_dish(data.model_copy(update={"snapshot": later_frame.supply}), DUCK,
                              service_status=duck.service_status.model_copy(update={"value": "AVAILABLE"}),
                              stock_portions=duck.stock_portions.model_copy(update={"value": 3}))
        recovered = replan(lost_supply, restored.snapshot, w2)
        self.assertEqual([(e.entity_id, e.value) for e in recovered.change_reasons if e.code == "SUPPLY_RESTORED"],
                         [(DUCK, 3), (DUCK, 0)])
        with self.assertRaises(Fault) as stale:
            recheck_selection(previous, "B_B_CHICKEN", w2_data.snapshot, w2)
        self.assertEqual(stale.exception.code, "STALE_SELECTION")

    def test_r05_timestamp_only_and_selection_only_changes_are_unchanged(self):
        sim, data = scene(request=DecisionRequest(max_options=2))
        original = evaluate(data)
        latest = advance(sim, sim.initial_frame(), data.snapshot.ref.clock_at + timedelta(minutes=1))
        selection = Selection(bundle_id="B_A_DUCK", window_id=sim.windows[0].window_id)
        result = replan(original, latest.supply, sim.windows[0], selected=selection)
        self.assertEqual(result.outcome, "UNCHANGED")
        self.assertEqual(result.selection_state, "FEASIBLE")
        self.assertEqual(result.change_reasons, ())

    def test_r05_replan_option_override_and_rejects_older_or_foreign_snapshots(self):
        sim, data = scene(request=DecisionRequest(max_options=1))
        original = evaluate(data)
        first = replan(original, data.snapshot, sim.windows[0])
        self.assertEqual(first.evaluation.body.alternatives, ())
        override = replan(original, data.snapshot, sim.windows[0], max_options=2)
        self.assertEqual(len(override.evaluation.body.alternatives), 1)
        self.assertEqual(override.outcome, "UPDATED")
        latest = advance(sim, sim.initial_frame(), data.snapshot.ref.clock_at + timedelta(minutes=1))
        newer = replan(original, latest.supply, sim.windows[0]).evaluation
        foreign = data.snapshot.model_copy(update={"ref": data.snapshot.ref.model_copy(update={"run_id": "foreign"})})
        for last in (data.snapshot, foreign):
            with self.assertRaises(Fault) as rejected:
                replan(newer, last, sim.windows[0])
            self.assertEqual(rejected.exception.code, "INVALID_INPUT")
        with self.assertRaises(Fault) as invalid:
            replan(original, data.snapshot, sim.windows[0], max_options=True)
        self.assertEqual(invalid.exception.code, "INVALID_INPUT")
