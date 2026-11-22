"""Pure decision and replan functions. No HTTP, simulator, or persistence dependencies."""

from dataclasses import dataclass
from datetime import datetime

from pydantic import TypeAdapter, ValidationError

from .decision_constraints import (
    Assessment, ConstraintEvaluator, FactUse, deadline_for, distinct, minutes_remaining, notice, sorted_conflicts,
)
from .decision_models import (
    DecisionBody, DecisionInput, Evidence, MealOption, OptionLimit, ReplanResult, Selection,
)
from .decision_ranking import (
    CONFIG_VERSION, CONFIDENCE_ORDER, RULES_VERSION, RankKey, ScoreBreakdown, confidence, fact_confidence,
    rank_options, score_candidate,
)
from .errors import Fault
from .models import Catalog, IntRange, ServiceWindow, SupplySnapshot


@dataclass(frozen=True)
class CandidateTrace:
    """Internal trace. Underlying dietary facts are never included in the public response."""

    facts: Assessment
    score: ScoreBreakdown | None


@dataclass(frozen=True)
class Evaluation:
    inputs: DecisionInput
    deadline_at: datetime | None
    explicit_max_options: int | None
    body: DecisionBody
    candidates: tuple[CandidateTrace, ...]

    def trace(self, bundle_id: str | None) -> CandidateTrace | None:
        return next((row for row in self.candidates if row.facts.bundle.bundle_id == bundle_id), None)


def _major(facts: tuple[FactUse, ...]) -> FactUse:
    return min(facts, key=lambda row: (CONFIDENCE_ORDER.index(fact_confidence(row)), row.entity, row.field))


def _explain(trace: CandidateTrace, inputs: DecisionInput, comparator: CandidateTrace | None = None) -> MealOption:
    assessed = trace.facts
    bundle = assessed.bundle
    request = inputs.request
    prefs, limits = request.preferences, request.constraints
    reasons = []
    if limits.budget_max_cents is not None:
        reasons.append((_major(assessed.prices).evidence("BUDGET_FIT", assessed.price),))
    if limits.time_budget_min is not None:
        reasons.append((_major(assessed.time_facts).evidence("TIME_FIT", assessed.total_time),))
    matched_tags = tuple(sorted(set(prefs.taste_tags) & assessed.taste_tags))
    requested = next((item.dish_id for item in bundle.items if item.dish_id == prefs.requested_dish_id), None)
    if requested is not None:
        reasons.append((notice("TASTE_MATCH", requested, "dish_id", requested),))
    elif matched_tags:
        reasons.append((notice("TASTE_MATCH", bundle.bundle_id, "taste_tags", matched_tags),))
    if prefs.recent_family_ids and assessed.family_id not in prefs.recent_family_ids:
        reasons.append((notice("NEW_FAMILY", bundle.anchor_dish_id, "family_id", assessed.family_id),))
    queue = assessed.queue.current
    if comparator is not None and queue.max < comparator.facts.queue.current.max:
        other = comparator.facts.queue
        reasons.append((assessed.queue.evidence("LOWER_QUEUE", queue),
                        other.evidence("LOWER_QUEUE", other.current)))

    warnings = []
    if inputs.snapshot.ref.data_mode == "SIMULATION":
        warnings.append(notice("SIMULATION_DATA"))
    if queue.max >= 12:
        warnings.append(assessed.queue.evidence("LONG_QUEUE", queue))
    if assessed.portions is not None and assessed.portions <= 2:
        stock = {use.entity: use for use in assessed.critical if use.field == "stock_portions"}
        limiting = min(bundle.items, key=lambda part: (stock[part.dish_id].current // part.portions, part.dish_id))
        warnings.append(stock[limiting.dish_id].evidence("LOW_STOCK", assessed.portions))
    if assessed.price is None:
        warnings.extend(use.evidence("PRICE_UNKNOWN") for use in assessed.prices if use.current is None)
    warnings.extend(use.evidence("STALE_FACT")
                    for use in assessed.critical + assessed.nutrition if use.freshness == "STALE")

    return MealOption(
        bundle_id=bundle.bundle_id, venue_id=bundle.venue_id, anchor_dish_id=bundle.anchor_dish_id,
        label=bundle.label, items=bundle.items, price_cents=assessed.price, queue_min=queue,
        total_time_min=assessed.total_time, available_bundle_portions=assessed.portions,
        match_score=trace.score.match_score, data_confidence=confidence(assessed.critical),
        evidence=tuple(evidence for reason in reasons[:2] for evidence in reason), warnings=distinct(tuple(warnings)),
    )


def _requested_conflicts(rows: tuple[CandidateTrace, ...], requested_dish_id: str | None,
                         requested_feasible: bool) -> tuple[Evidence, ...]:
    if requested_dish_id is None or requested_feasible:
        return ()
    matching = [row for row in rows if any(item.dish_id == requested_dish_id for item in row.facts.bundle.items)]
    if not matching:
        return (notice("REQUESTED_BUNDLE_NOT_REGISTERED", requested_dish_id, "dish_id"),)
    return sorted_conflicts(tuple(evidence for row in matching for evidence in row.facts.conflicts))


def _diagnostics(primary: tuple[Evidence, ...], secondary: tuple[Evidence, ...]) -> tuple[Evidence, ...]:
    by_code = {}
    for item in primary + secondary:
        by_code.setdefault(item.code, item)
    return tuple(by_code.values())[:3]


def _evaluate(inputs: DecisionInput, deadline_at: datetime | None, explicit_limit: int | None,
              max_options: int, selected: Selection | None = None) -> Evaluation:
    request = inputs.request
    remaining = minutes_remaining(inputs.snapshot.ref.clock_at, deadline_at)
    rows = tuple(CandidateTrace(row, None if row.conflicts else score_candidate(row, request, remaining))
                 for row in ConstraintEvaluator(inputs, remaining).candidates())
    feasible = {row.facts.bundle.bundle_id: row for row in rows if row.score is not None}
    wanted = request.preferences.requested_dish_id
    keys = tuple(RankKey(
        bundle_id=row.facts.bundle.bundle_id, venue_id=row.facts.bundle.venue_id,
        family_id=row.facts.family_id, score=row.score.match_score, queue_upper=row.facts.queue.current.max,
        confidence=confidence(row.facts.critical),
        includes_requested=any(item.dish_id == wanted for item in row.facts.bundle.items),
    ) for row in feasible.values())
    ranked = rank_options(keys, max_options)
    conflicts = _requested_conflicts(rows, wanted, ranked.requested is not None)
    global_warnings = []
    if not request.constraints.avoid_ingredients and not request.constraints.avoid_allergens:
        global_warnings.append(notice("RESTRICTIONS_NOT_DECLARED"))
    if request.origin_assumed and inputs.snapshot.ref.data_mode == "SIMULATION":
        global_warnings.append(notice("ORIGIN_ASSUMED"))
    if inputs.snapshot.ref.data_mode == "SIMULATION":
        global_warnings.append(notice("SIMULATION_DATA"))

    primary = None
    alternatives = ()
    requested_option = None
    if ranked.primary is not None:
        peer = feasible[ranked.queue_comparator.bundle_id] if ranked.queue_comparator else None
        primary = _explain(feasible[ranked.primary.bundle_id], inputs, peer)
        alternatives = tuple(_explain(feasible[row.bundle_id], inputs) for row in ranked.alternatives)
        if ranked.requested is not None:
            existing = next((option for option in (primary, *alternatives)
                             if option.bundle_id == ranked.requested.bundle_id), None)
            requested_option = existing or _explain(feasible[ranked.requested.bundle_id], inputs)
    else:
        selected_conflicts = tuple(
            e for row in rows if selected is not None and row.facts.bundle.bundle_id == selected.bundle_id
            for e in row.facts.conflicts
        )
        general = sorted_conflicts(tuple(e for row in rows for e in row.facts.conflicts))
        conflicts = _diagnostics(selected_conflicts + conflicts, general)

    body = DecisionBody(
        snapshot=inputs.snapshot.ref, rules_version=RULES_VERSION, config_version=CONFIG_VERSION,
        status="OK" if primary is not None else "NO_FEASIBLE_OPTION",
        primary=primary, alternatives=alternatives, requested_option=requested_option,
        conflicts=conflicts, warnings=tuple(global_warnings),
    )
    return Evaluation(inputs, deadline_at, explicit_limit, body, rows)


def evaluate(inputs: DecisionInput) -> Evaluation:
    """Return a deterministic business result and the actual trace for each considered bundle."""
    request = inputs.request
    deadline = deadline_for(inputs.snapshot.ref.clock_at, request.constraints.time_budget_min)
    return _evaluate(inputs, deadline, request.max_options if request.explicit_max_options else None,
                     request.max_options)


def restore_evaluation(inputs: DecisionInput, body: DecisionBody, *, deadline_at: datetime | None,
                       explicit_max_options: int | None, effective_max_options: int) -> Evaluation:
    """Replay the original facts while preserving the business result actually shown."""
    if (body.snapshot != inputs.snapshot.ref or body.rules_version != RULES_VERSION
            or body.config_version != CONFIG_VERSION):
        raise Fault("INVALID_INPUT", "The saved decision does not match this replay context.")
    replay = _evaluate(inputs, deadline_at, explicit_max_options, effective_max_options)
    return Evaluation(inputs, deadline_at, explicit_max_options, body, replay.candidates)


def _public_signature(body: DecisionBody):
    def evidence(rows):
        return tuple((e.code, e.value, e.freshness, e.source_kind) for e in rows)

    def card(item):
        if item is None:
            return None
        return (item.bundle_id, item.match_score, item.data_confidence, item.price_cents, item.queue_min,
                item.total_time_min, item.available_bundle_portions, evidence(item.evidence), evidence(item.warnings))

    return (card(body.primary), tuple(map(card, body.alternatives)), card(body.requested_option),
            evidence(body.conflicts), evidence(body.warnings))


@dataclass(frozen=True)
class Replanning:
    evaluation: Evaluation
    outcome: str
    change_reasons: tuple[Evidence, ...]
    selected_bundle_id: str | None
    selection_state: str

    def bind(self, previous_recommendation_id: str, recommendation_id: str) -> ReplanResult:
        return ReplanResult(
            previous_recommendation_id=previous_recommendation_id, result=self.evaluation.body.bind(recommendation_id),
            outcome=self.outcome, change_reasons=self.change_reasons, selected_bundle_id=self.selected_bundle_id,
            selection_state=self.selection_state,
        )


def _latest(previous: Evaluation, snapshot: SupplySnapshot, window: ServiceWindow | None,
            catalog: Catalog | None) -> DecisionInput:
    before, after = previous.inputs.snapshot.ref, snapshot.ref
    if (before.run_id != after.run_id or before.data_mode != after.data_mode
            or after.revision < before.revision or after.clock_at < before.clock_at
            or (after.revision == before.revision and snapshot != previous.inputs.snapshot)
            or (after.revision > before.revision and after.snapshot_id == before.snapshot_id)):
        raise Fault("INVALID_INPUT", "Replanning requires a consistent snapshot from the same running scenario.")
    try:
        return DecisionInput(request=previous.inputs.request, catalog=catalog or previous.inputs.catalog,
                             snapshot=snapshot, window=window)
    except ValidationError as error:
        raise Fault("INVALID_INPUT", "Replanning requires the catalog and window of the supplied snapshot.") from error


def _with_prior(reasons: tuple[Evidence, ...], prior: CandidateTrace | None) -> tuple[Evidence, ...]:
    if prior is None:
        return reasons
    known = {(row.entity, row.field): row for row in prior.facts.critical}
    history = []
    for reason in reasons:
        history.append(reason)
        old = known.get((reason.entity_id, reason.field))
        if old is not None and old.fact is not None and old.fact.as_of is not None:
            # Preserve the earlier evidence time without copying ingredient/allergen values.
            safe_value = old.current if isinstance(old.current, (int, str, IntRange)) else None
            evidence = old.evidence(reason.code, safe_value)
            if evidence != reason:
                history.append(evidence)
    return distinct(tuple(history))


def _change_reasons(previous: Evaluation, current: Evaluation, selected: Selection | None,
                    changed: bool) -> tuple[Evidence, ...]:
    new_window = current.inputs.snapshot.active_window_id
    old_window = previous.inputs.snapshot.active_window_id
    reasons = []
    if selected is not None and selected.window_id != new_window:
        reasons.append(notice("SERVICE_WINDOW_CHANGED", new_window, "active_window_id", new_window))
    elif selected is not None:
        candidate = current.trace(selected.bundle_id)
        if candidate is not None:
            reasons.extend(_with_prior(candidate.facts.conflicts, previous.trace(selected.bundle_id)))

    if previous.inputs.request.preferences.requested_dish_id is not None and current.body.requested_option is None:
        requested = previous.body.requested_option
        wanted = previous.inputs.request.preferences.requested_dish_id
        requested_codes = _requested_conflicts(current.candidates, wanted, False)
        reasons.extend(_with_prior(requested_codes, previous.trace(requested.bundle_id) if requested else None))

    for option in (previous.body.primary, *previous.body.alternatives):
        if option is not None:
            old, new = previous.trace(option.bundle_id), current.trace(option.bundle_id)
            if new is not None:
                reasons.extend(_with_prior(new.facts.conflicts, old))

    if changed and new_window != old_window:
        reasons.append(notice("SERVICE_WINDOW_CHANGED", new_window, "active_window_id", new_window))
    if changed:
        for bundle_id in dict.fromkeys((*previous.body.selectable_bundle_ids, *current.body.selectable_bundle_ids)):
            old, new = previous.trace(bundle_id), current.trace(bundle_id)
            if old is None or new is None:
                continue
            if old.facts.queue.current != new.facts.queue.current:
                reasons.extend((new.facts.queue.evidence("QUEUE_CHANGED", new.facts.queue.current),
                                old.facts.queue.evidence("QUEUE_CHANGED", old.facts.queue.current)))
            if old.score is None and new.score is not None:
                restored_ids = {e.entity_id for e in old.facts.conflicts
                                if e.code in {"SOLD_OUT", "UNAVAILABLE", "INSUFFICIENT_STOCK", "SUPPLY_UNVERIFIED"}}
                restored = tuple(use.evidence("SUPPLY_RESTORED", use.current)
                                 for use in new.facts.critical
                                 if use.field == "stock_portions" and use.entity in restored_ids)
                reasons.extend(_with_prior(restored, old))
    if changed and not reasons and current.body.primary is not None:
        item = current.body.primary
        reasons.append(notice("RANKING_CHANGED", item.bundle_id, "match_score", item.match_score))
    return distinct(tuple(reasons))


def replan(previous: Evaluation, snapshot: SupplySnapshot, window: ServiceWindow | None, *,
           selected: Selection | None = None, max_options: int | None = None,
           catalog: Catalog | None = None) -> Replanning:
    """Re-evaluate the original request using its absolute cutoff and the new snapshot."""
    data = _latest(previous, snapshot, window, catalog)
    limit = previous.explicit_max_options
    if max_options is not None:
        try:
            limit = TypeAdapter(OptionLimit).validate_python(max_options, strict=True)
        except ValidationError as error:
            raise Fault("INVALID_INPUT", "max_options must be an integer from 1 to 4.") from error
    result = _evaluate(data, previous.deadline_at, limit, limit if limit is not None else 4, selected)
    changed = _public_signature(result.body) != _public_signature(previous.body)
    outcome = ("NO_FEASIBLE_OPTION" if result.body.status == "NO_FEASIBLE_OPTION" else
               "UPDATED" if changed else "UNCHANGED")
    selection_state, selected_id = "NONE", None
    if selected is not None:
        selected_id = selected.bundle_id
        trace = result.trace(selected_id)
        selection_state = ("FEASIBLE" if selected.window_id == snapshot.active_window_id
                           and trace is not None and trace.score is not None else "NEEDS_RESELECT")
    return Replanning(result, outcome, _change_reasons(previous, result, selected, changed), selected_id, selection_state)


def recheck_selection(previous: Evaluation, bundle_id: str, snapshot: SupplySnapshot,
                      window: ServiceWindow | None, *, catalog: Catalog | None = None) -> None:
    """Pure preflight for an authorized selection; caller owns the transaction and intent write."""
    if bundle_id not in previous.body.selectable_bundle_ids:
        raise Fault("INVALID_INPUT", "The selected bundle was not offered by this recommendation.")
    data = _latest(previous, snapshot, window, catalog)
    if snapshot.active_window_id is not None and snapshot.active_window_id == previous.inputs.snapshot.active_window_id:
        remaining = minutes_remaining(snapshot.ref.clock_at, previous.deadline_at)
        evaluator = ConstraintEvaluator(data, remaining)
        candidate = next((item for item in data.catalog.bundles if item.bundle_id == bundle_id), None)
        if candidate is not None and not evaluator.assess(candidate).conflicts:
            return
    raise Fault("STALE_SELECTION", "The previously offered bundle is no longer actionable.", snapshot.ref)
