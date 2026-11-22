"""Approved request and response types for simulated operations execution."""

from typing import Literal

from .models import ID, Count, Key, Model, Positive, SnapshotRef, Timestamp
from .student_models import RunRequest


class ConfirmOpsRequest(RunRequest):
    proposal_id: ID
    approved_extra_portions: Positive
    expected_snapshot_id: ID
    idempotency_key: Key


class OpsConfirmation(Model):
    proposal_id: ID
    approved_extra_portions: Positive
    actor_id: ID
    confirmed_at: Timestamp
    confirmation_snapshot: SnapshotRef


class OpsStateQuery(RunRequest):
    target_window_id: ID
    venue_id: ID | None = None
    snapshot_id: ID | None = None


class PlanProjection(Model):
    venue_id: ID
    dish_id: ID
    base_planned_portions: Count
    confirmed_extra_portions: Count
    max_served_portions: Count


class ActionProjection(Model):
    proposal_id: ID
    venue_id: ID
    dish_id: ID
    status: Literal["CONFIRMED", "APPLIED"]
    approved_extra_portions: Positive
    confirmation_snapshot_id: ID
    application_snapshot_id: ID | None
    applied_at: Timestamp | None


class AuditProjection(Model):
    audit_id: ID
    venue_id: ID
    proposal_id: ID
    action: Literal["OPS_CONFIRMED", "OPS_APPLIED"]
    result_code: ID
    at: Timestamp
    snapshot_id: ID


class OpsState(Model):
    target_window_id: ID
    plans: tuple[PlanProjection, ...]
    actions: tuple[ActionProjection, ...]
    audit: tuple[AuditProjection, ...]
