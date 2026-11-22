"""The seven published MCP tool schemas, derived from shared application models."""

from dataclasses import dataclass
from typing import Literal

from .decision_models import DecisionResult, Evidence, ReplanResult
from .demand_insights import DemandInsights, DemandInsightsQuery
from .models import ID, BundleItem, Count, Fact, IntRange, Model, SnapshotRef, TimeRange, VenueStatus
from .ops_analysis import OpsProposalsRequest, OpsReport
from .ops_execution_models import ConfirmOpsRequest, OpsConfirmation
from .student_models import (
    FEEDBACK_FIELDS, DiningQuery, FeedbackRequest, FeedbackType, RecommendRequest, ReplanRequest, Token,
)

ERROR_CODES = (
    "INVALID_INPUT", "UNAUTHENTICATED", "FORBIDDEN", "CONFIRMATION_REQUIRED", "MODE_DISABLED", "NOT_FOUND",
    "RUN_REPLACED", "STALE_SELECTION", "SNAPSHOT_CONFLICT", "STALE_PROPOSAL", "OPS_ALREADY_CONFIRMED",
    "IDEMPOTENCY_CONFLICT", "RATE_LIMITED", "DEPENDENCY_UNAVAILABLE", "INTERNAL_ERROR",
)


class VenueProjection(Model):
    venue_id: ID
    name: Fact[str]
    location_label: Fact[str]
    opening_hours: Fact[tuple[TimeRange, ...]]
    service_status: Fact[VenueStatus]
    queue_min: Fact[IntRange]


class BundleProjection(Model):
    bundle_id: ID
    venue_id: ID
    label: str
    items: tuple[BundleItem, ...]
    anchor_dish_id: ID
    price_cents: Count | None
    available_bundle_portions: Count | None
    supply_state: Literal["AVAILABLE", "SOLD_OUT", "UNAVAILABLE", "UNKNOWN"]
    evidence: tuple[Evidence, ...]


class DiningOptions(Model):
    venues: tuple[VenueProjection, ...]
    options: tuple[BundleProjection, ...]
    next_cursor: Token | None


class FeedbackReceipt(Model):
    event_id: ID
    event_type: FeedbackType
    window_id: ID
    share_applied: bool
    signals_revision: Count


class FeedbackWithReceipt(FeedbackRequest):
    interaction_receipt: Token


class ConfirmationWithReceipt(ConfirmOpsRequest):
    interaction_receipt: Token


@dataclass(frozen=True)
class Tool:
    request: type[Model]
    output: type[Model]
    description: str
    receipt: type[Model] | None = None

    def schema(self, name):
        incoming = (self.receipt or self.request).model_json_schema()
        for item in (incoming, *incoming.get("$defs", {}).values()):
            for field, prop in item.get("properties", {}).items():
                if field not in item.get("required", ()) and "anyOf" in prop:
                    variants = [p for p in prop.pop("anyOf") if p.get("type") != "null"]
                    prop.pop("default", None)
                    if len(variants) == 1:
                        prop.update(variants[0])
                    else:
                        prop["anyOf"] = variants
        if self.request is DiningQuery:
            incoming["properties"]["limit"]["default"] = 20
        if self.request is FeedbackRequest:
            refs = {"recommendation_id", "bundle_id", "dish_id"}
            incoming["allOf"] = [
                {"if": {"properties": {"event_type": {"const": event}}},
                 "then": {"required": sorted(required), "properties": {
                     field: False for field in sorted(refs - required - optional)
                 }}}
                for event, (required, optional) in FEEDBACK_FIELDS.items()
            ]
        return {"name": name, "description": self.description, "inputSchema": incoming,
                "outputSchema": output_envelope(self.output)}


def object_schema(**properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def output_envelope(model):
    data = model.model_json_schema(mode="serialization")
    definitions = data.pop("$defs", {})
    definitions.setdefault("SnapshotRef", SnapshotRef.model_json_schema(mode="serialization"))
    definitions["SnapshotRef"]["required"] = list(definitions["SnapshotRef"]["properties"])
    if "Evidence" in definitions:
        value = definitions["Evidence"]["properties"]["value"]
        value["anyOf"] = [item for item in value["anyOf"] if item.get("type") != "null"]
        value.pop("default", None)
    ref = {"$ref": "#/$defs/SnapshotRef"}
    mode = {"const": "SIMULATION"}
    version = {"const": "0.3"}
    error = object_schema(code={"enum": list(ERROR_CODES)}, message={"type": "string"}, retryable={"type": "boolean"})
    details = {"INVALID_INPUT": ("field", {"type": "string"}),
               "RATE_LIMITED": ("retry_after_seconds", {"type": "integer", "minimum": 1}),
               "INTERNAL_ERROR": ("trace_id", {"type": "string", "pattern": "^[A-Za-z0-9._-]{1,64}$"})}
    error["properties"]["details"] = {"type": "object"}
    error["allOf"] = [
        {"if": {"properties": {"code": {"const": code}}},
         "then": {"properties": {"details": object_schema(**{field: schema})}}}
        for code, (field, schema) in details.items()
    ]
    error["allOf"].append({"if": {"properties": {"code": {"not": {"enum": list(details)}}}},
                           "then": {"not": {"required": ["details"]}}})
    return {
        "type": "object", "$defs": definitions,
        "oneOf": [
            object_schema(schema_version=version, ok={"const": True}, data_mode=mode, snapshot=ref, data=data),
            object_schema(schema_version=version, ok={"const": False}, data_mode={"anyOf": [mode, {"type": "null"}]},
                          snapshot={"anyOf": [ref, {"type": "null"}]}, error=error),
        ],
    }


TOOLS = {
    "get_dining_options": Tool(DiningQuery, DiningOptions, "Read a visible SIMULATION menu and supply snapshot."),
    "recommend_meal": Tool(RecommendRequest, DecisionResult, "Compute and save a personal recommendation without feedback."),
    "replan_meal": Tool(ReplanRequest, ReplanResult, "Replan an owned decision without modifying the student's intent."),
    "submit_feedback": Tool(FeedbackRequest, FeedbackReceipt, "Write own feedback with a human-authorized receipt.",
                            FeedbackWithReceipt),
    "get_demand_insights": Tool(DemandInsightsQuery, DemandInsights, "Read permitted, privacy-filtered demand aggregates."),
    "generate_ops_recommendations": Tool(OpsProposalsRequest, OpsReport, "Calculate and save proposals without reserving stock."),
    "confirm_ops_action": Tool(ConfirmOpsRequest, OpsConfirmation, "Reserve simulated supply using an operator's Web receipt.",
                               ConfirmationWithReceipt),
}
