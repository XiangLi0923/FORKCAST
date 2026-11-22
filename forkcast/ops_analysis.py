"""Supply gap and capacity analysis from authorized, disclosed demand."""

from typing import Literal

from .demand_insights import DemandInsight
from .models import ID, Count, IngredientPool, Model, Positive, PrepResources, SupplyPlan, Timestamp
from .student_models import RunRequest

EvidenceCode = Literal[
    "DEMAND_FROM_PREFERENCE", "DEMAND_FROM_INTENT", "DEMAND_FROM_SELF_REPORT", "INSUFFICIENT_SIGNAL",
    "DISH_CAPACITY_LIMIT", "PREP_CAPACITY_LIMIT", "INGREDIENT_LIMIT",
]
ProposalStatus = Literal["PROPOSED", "CONFIRMED", "APPLIED", "STALE"]
DEMAND_CHANNELS = (
    ("preferred_sessions", 2, "DEMAND_FROM_PREFERENCE"),
    ("intended_sessions", 1, "DEMAND_FROM_INTENT"),
    ("dined_sessions", 1, "DEMAND_FROM_SELF_REPORT"),
)


class OpsProposalsRequest(RunRequest):
    reference_window_id: ID
    target_window_id: ID
    venue_id: ID | None = None


class OpsAnalysis(Model):
    venue_id: ID
    dish_id: ID
    reference_window_id: ID
    target_window_id: ID
    demand_proxy_portions: Count | None
    planned_supply_portions: Count
    shortage_portions: Count | None
    feasible_extra_portions: Count | None
    uncovered_portions: Count | None
    ingredient_review_portions: Count | None
    evidence_codes: tuple[EvidenceCode, ...]


class OpsProposal(Model):
    proposal_id: ID
    run_id: ID
    venue_id: ID
    dish_id: ID
    reference_window_id: ID
    target_window_id: ID
    based_on_snapshot_id: ID
    signals_revision: Count
    expected_effective_at: Timestamp
    demand_proxy_portions: Count
    planned_supply_portions: Count
    shortage_portions: Count
    proposed_extra_portions: Positive
    uncovered_portions: Count
    evidence_codes: tuple[EvidenceCode, ...]
    status: ProposalStatus


class OpsReport(Model):
    signals_revision: Count
    analyses: tuple[OpsAnalysis, ...]
    proposals: tuple[OpsProposal, ...]


def demand_proxy(demand: DemandInsight) -> tuple[int | None, tuple[EvidenceCode, ...]]:
    visible = []
    for field, divisor, code in DEMAND_CHANNELS:
        count = getattr(demand, field)
        if getattr(demand.visibility, field) == "VISIBLE" and count is not None:
            visible.append((count // divisor, code))
    if not visible:
        return None, ("INSUFFICIENT_SIGNAL",)
    proxy = max(value for value, _ in visible)
    return proxy, tuple(code for value, code in visible if value == proxy)


def analyze_supply(
    demand: DemandInsight, plan: SupplyPlan, prep: PrepResources | None, ingredient: IngredientPool | None,
) -> OpsAnalysis:
    proxy, codes = demand_proxy(demand)
    planned = plan.base_planned_portions + plan.confirmed_extra_portions
    gap = feasible = uncovered = review = None
    if proxy is not None:
        gap = max(0, proxy - planned)
        dish_room = max(0, plan.max_served_portions - planned)
        prep_room = max(0, prep.extra_prep_capacity_portions - prep.reserved_prep_portions) if prep else 0
        ingredient_room = max(0, ingredient.extra_ingredient_portions - ingredient.reserved_ingredient_portions) if ingredient else 0
        feasible = min(gap, dish_room, prep_room, ingredient_room)
        uncovered = gap - feasible
        review = max(0, min(gap, dish_room, prep_room) - ingredient_room)
        if uncovered:
            codes += tuple(code for resource, code in (
                (dish_room, "DISH_CAPACITY_LIMIT"), (prep_room, "PREP_CAPACITY_LIMIT"),
                (ingredient_room, "INGREDIENT_LIMIT"),
            ) if resource == feasible)
    return OpsAnalysis(
        venue_id=demand.venue_id, dish_id=demand.dish_id, reference_window_id=demand.reference_window_id,
        target_window_id=plan.window_id, demand_proxy_portions=proxy, planned_supply_portions=planned,
        shortage_portions=gap, feasible_extra_portions=feasible, uncovered_portions=uncovered,
        ingredient_review_portions=review, evidence_codes=codes,
    )
