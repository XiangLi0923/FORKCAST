import unittest
from datetime import UTC, datetime

from forkcast.demand_insights import DemandInsight, DemandVisibility
from forkcast.models import IngredientPool, PrepResources, SupplyPlan
from forkcast.ops_analysis import analyze_supply, demand_proxy


def demand(p, i, a):
    counts = dict(preferred_sessions=p, intended_sessions=i, dined_sessions=a)
    return DemandInsight(
        run_id="test", reference_window_id="test.W1", venue_id="V_A", dish_id="D_A_DUCK",
        signals_revision=12, observed_until=datetime(2026, 11, 21, 3, 40, tzinfo=UTC),
        visibility={field: "VISIBLE" if value is not None else "SUPPRESSED" for field, value in counts.items()},
        **counts,
    )


class OpsAnalysisTests(unittest.TestCase):
    def test_visible_signals_only_and_tie_evidence(self):
        samples = (
            ((4, 6, 3), 6, ("DEMAND_FROM_INTENT",)),
            ((None, 6, None), 6, ("DEMAND_FROM_INTENT",)),
            ((5, None, None), 2, ("DEMAND_FROM_PREFERENCE",)),
            ((None, None, 3), 3, ("DEMAND_FROM_SELF_REPORT",)),
            ((None, None, None), None, ("INSUFFICIENT_SIGNAL",)),
            ((7, 3, 3), 3, ("DEMAND_FROM_PREFERENCE", "DEMAND_FROM_INTENT", "DEMAND_FROM_SELF_REPORT")),
        )
        for values, proxy, codes in samples:
            with self.subTest(values=values):
                self.assertEqual(demand_proxy(demand(*values)), (proxy, codes))
        inconsistent = demand(100, 3, None).model_copy(update={
            "visibility": DemandVisibility(preferred_sessions="SUPPRESSED", intended_sessions="VISIBLE",
                                           dined_sessions="SUPPRESSED"),
        })
        self.assertEqual(demand_proxy(inconsistent), (3, ("DEMAND_FROM_INTENT",)))

    def test_s07_capacity_limits_reservations_and_suppression(self):
        examples = (
            ("fixed", (4, 6, 3), (0, 0, 8), (4, 0), (3, 0), (6, 0, 6, 3, 3, 1), ("INGREDIENT_LIMIT",)),
            ("suppressed", (None, None, None), (0, 0, 8), (4, 0), (3, 0),
             (None, 0, None, None, None, None), ()),
            ("no gap", (4, 6, 3), (4, 3, 8), (4, 4), (3, 3), (6, 7, 0, 0, 0, 0), ()),
            ("zero dish limit", (4, 6, 3), (0, 0, 0), (8, 0), (8, 0), (6, 0, 6, 0, 6, 0),
             ("DISH_CAPACITY_LIMIT",)),
            ("zero prep", (4, 6, 3), (0, 0, 8), (0, 0), (8, 0), (6, 0, 6, 0, 6, 0), ("PREP_CAPACITY_LIMIT",)),
            ("zero ingredients", (4, 6, 3), (0, 0, 8), (4, 0), (0, 0), (6, 0, 6, 0, 6, 4),
             ("INGREDIENT_LIMIT",)),
            ("prep limited", (4, 6, 3), (0, 0, 8), (4, 2), (8, 0), (6, 0, 6, 2, 4, 0),
             ("PREP_CAPACITY_LIMIT",)),
            ("dish limited", (4, 6, 3), (1, 1, 4), (9, 0), (9, 0), (6, 2, 4, 2, 2, 0), ("DISH_CAPACITY_LIMIT",)),
            ("resources tied", (4, 6, 3), (0, 1, 3), (4, 2), (5, 3), (6, 1, 5, 2, 3, 0),
             ("DISH_CAPACITY_LIMIT", "PREP_CAPACITY_LIMIT", "INGREDIENT_LIMIT")),
            ("fully covered", (4, 6, 3), (1, 1, 9), (9, 1), (9, 1), (6, 2, 4, 4, 0, 0), ()),
        )
        for name, values, supply, prep, ingredients, quantities, limits in examples:
            with self.subTest(name=name):
                target = {"run_id": "test", "window_id": "test.W2"}
                plan = SupplyPlan(
                    **target, dish_id="D_A_DUCK", base_planned_portions=supply[0],
                    confirmed_extra_portions=supply[1], max_served_portions=supply[2],
                )
                labor = PrepResources(
                    **target, venue_id="V_A", extra_prep_capacity_portions=prep[0], reserved_prep_portions=prep[1],
                )
                materials = IngredientPool(
                    **target, dish_id="D_A_DUCK", extra_ingredient_portions=ingredients[0],
                    reserved_ingredient_portions=ingredients[1],
                )
                analysis = analyze_supply(demand(*values), plan, labor, materials)
                self.assertEqual((
                    analysis.demand_proxy_portions, analysis.planned_supply_portions, analysis.shortage_portions,
                    analysis.feasible_extra_portions, analysis.uncovered_portions, analysis.ingredient_review_portions,
                ), quantities)
                evidence = "INSUFFICIENT_SIGNAL" if quantities[0] is None else "DEMAND_FROM_INTENT"
                self.assertEqual(analysis.evidence_codes, (evidence, *limits))
