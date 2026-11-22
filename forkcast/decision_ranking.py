"""Exact R02 scoring and R04 ranking with R06 confidence kept separate."""

from dataclasses import dataclass
from fractions import Fraction
from types import MappingProxyType

from .decision_constraints import Assessment, FactUse
from .decision_models import DecisionRequest, Profile
from .models import Confidence

RULES_VERSION = "rules.v03.1"
CONFIG_VERSION = "config.v03.1"
NEAR_OPTIMAL = 50
FACTOR_NAMES = ("taste", "time", "price", "exploration", "nutrition")
WEIGHTS = MappingProxyType({
    "DEFAULT": (35, 25, 15, 15, 10),
    "FAST": (25, 45, 10, 10, 10),
    "VALUE": (25, 20, 35, 10, 10),
    "EXPLORE": (25, 20, 10, 35, 10),
})
CONFIDENCE_ORDER: tuple[Confidence, ...] = ("UNKNOWN", "LOW", "MEDIUM", "HIGH")
NEUTRAL = Fraction(1, 2)


@dataclass(frozen=True)
class Factor:
    name: str
    value: Fraction
    weight: int
    neutral_default: bool


@dataclass(frozen=True)
class ScoreBreakdown:
    factors: tuple[Factor, ...]
    match_score: int


def score_factors(profile: Profile, values: tuple[Fraction, ...],
                  neutral: frozenset[str] = frozenset()) -> ScoreBreakdown:
    factors = tuple(Factor(name, min(Fraction(1), max(Fraction(0), value)), weight, name in neutral)
                    for name, value, weight in zip(FACTOR_NAMES, values, WEIGHTS[profile], strict=True))
    total = 10 * sum(part.value * part.weight for part in factors)
    return ScoreBreakdown(factors, int(total + NEUTRAL))


def score_candidate(candidate: Assessment, request: DecisionRequest, remaining: int | None) -> ScoreBreakdown:
    prefs = request.preferences
    if candidate.conflicts or candidate.total_time is None:
        raise ValueError("Only feasible bundles may be scored.")
    requested = any(item.dish_id == prefs.requested_dish_id for item in candidate.bundle.items)
    tags = frozenset(prefs.taste_tags)
    taste = Fraction(1) if requested else Fraction(len(tags & candidate.taste_tags), len(tags)) if tags else NEUTRAL
    minutes = 45 if remaining is None else max(1, remaining)
    time = Fraction(minutes - candidate.total_time.max, minutes)
    budget = request.constraints.budget_max_cents
    cents = 3000 if budget is None else max(1, budget)
    price = NEUTRAL if candidate.price is None else Fraction(cents - candidate.price, cents)
    recent = prefs.recent_family_ids
    exploration = Fraction(candidate.family_id not in recent) if recent else NEUTRAL
    nutrition = NEUTRAL if candidate.kcal is None else Fraction(900 - candidate.kcal.max, 500)
    neutral = frozenset(name for name, used_default in (
        ("taste", not requested and not tags), ("price", candidate.price is None),
        ("exploration", not recent), ("nutrition", candidate.kcal is None),
    ) if used_default)
    return score_factors(prefs.profile, (taste, time, price, exploration, nutrition), neutral)


def fact_confidence(use: FactUse) -> Confidence:
    if use.fact is None or use.freshness == "UNKNOWN":
        return "UNKNOWN"
    if use.freshness == "STALE" and use.fact.confidence in ("MEDIUM", "HIGH"):
        return "LOW"
    return use.fact.confidence


def confidence(facts: tuple[FactUse, ...]) -> Confidence:
    return min(map(fact_confidence, facts), key=CONFIDENCE_ORDER.index, default="UNKNOWN")


@dataclass(frozen=True)
class RankKey:
    bundle_id: str
    venue_id: str
    family_id: str
    score: int
    queue_upper: int
    confidence: Confidence
    includes_requested: bool = False

    @property
    def utility_order(self):
        return -self.score, -CONFIDENCE_ORDER.index(self.confidence), self.bundle_id


@dataclass(frozen=True)
class Ranked:
    primary: RankKey | None
    alternatives: tuple[RankKey, ...]
    requested: RankKey | None
    queue_comparator: RankKey | None


def rank_options(candidates: tuple[RankKey, ...], limit: int) -> Ranked:
    if not 1 <= limit <= 4:
        raise ValueError("option limit must be between one and four")
    if not candidates:
        return Ranked(None, (), None, None)
    ordered = sorted(candidates, key=lambda row: row.utility_order)
    threshold = ordered[0].score - NEAR_OPTIMAL
    near = [row for row in ordered if row.score >= threshold]
    primary = min(near, key=lambda row: (row.queue_upper, *row.utility_order))
    comparator = next((row for row in near if row.bundle_id != primary.bundle_id), None)
    requested = next((row for row in ordered if row.includes_requested), None)

    seen = {(primary.venue_id, primary.family_id)}
    alternatives = []
    for row in ordered:
        if len(alternatives) == limit - 1:
            break
        key = row.venue_id, row.family_id
        if key not in seen:
            alternatives.append(row)
            seen.add(key)
    return Ranked(primary, tuple(alternatives), requested, comparator)
