"""Closed M2.2 request and persistence types."""

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_serializer, model_validator

from .decision_models import DecisionRequest, DecisionResult, OptionLimit, RequestFields
from .models import ID, Key, Count, Model, SnapshotRef, Timestamp

Token = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{16,512}$")]
SessionKind = Literal["USABILITY_TEST", "DEMO"]
FeedbackType = Literal["PREFERENCE", "INTENT_SELECTED", "INTENT_CLEARED", "DINED", "NOT_DINED", "REJECTED"]

FEEDBACK_FIELDS = {
    "PREFERENCE": ({"dish_id"}, set()),
    "INTENT_SELECTED": ({"recommendation_id", "bundle_id"}, set()),
    "INTENT_CLEARED": ({"recommendation_id"}, set()),
    "DINED": ({"recommendation_id", "bundle_id"}, set()),
    "NOT_DINED": ({"recommendation_id"}, set()),
    "REJECTED": ({"recommendation_id"}, {"bundle_id"}),
}
FEEDBACK_CHANNELS = {
    "PREFERENCE": "PREFERENCE",
    "INTENT_SELECTED": "INTENT",
    "INTENT_CLEARED": "INTENT",
    "DINED": "MEAL",
    "NOT_DINED": "MEAL",
}


class RunRequest(RequestFields):
    run_id: ID


class OpenSession(RunRequest):
    demo_enrollment_token: Token | None = None


class DemoEnrollmentRequest(RunRequest):
    idempotency_key: Key


class SessionInfo(Model):
    session_kind: SessionKind
    share_for_ops: bool
    consent_revision: Count


class RecommendRequest(RunRequest):
    request: DecisionRequest
    snapshot_id: ID | None = None


class ReplanRequest(RunRequest):
    previous_recommendation_id: ID
    max_options: OptionLimit | None = None


class DiningQuery(RunRequest):
    snapshot_id: ID | None = None
    venue_id: ID | None = None
    cursor: Token | None = None
    limit: Annotated[int, Field(ge=1, le=50)] | None = None


class FeedbackRequest(RunRequest):
    event_type: FeedbackType
    recommendation_id: ID | None = None
    bundle_id: ID | None = None
    dish_id: ID | None = None
    idempotency_key: Key

    @model_validator(mode="after")
    def references_match_event(self):
        passed = set(self.model_fields_set) & {"recommendation_id", "dish_id", "bundle_id"}
        required, optional = FEEDBACK_FIELDS[self.event_type]
        if not required <= passed <= required | optional:
            raise ValueError("feedback reference fields do not match the event type")
        return self


class ConsentRequest(RunRequest):
    share_for_ops: bool
    expected_consent_revision: Count
    idempotency_key: Key


class RecommendationRecord(Model):
    recommendation_id: ID
    run_id: ID
    principal_id: ID
    snapshot: SnapshotRef
    origin_window_id: ID | None
    normalized_request: DecisionRequest
    explicit_max_options: bool
    effective_max_options: OptionLimit
    deadline_at: Timestamp | None
    selectable_bundle_ids: tuple[ID, ...]
    rules_version: ID
    config_version: ID
    result: DecisionResult
    previous_recommendation_id: ID | None

    @model_serializer(mode="wrap")
    def preserve_request_presence(self, handler):
        out = handler(self)
        # Defaults, including explicit K and origin assumptions, retain their original presence.
        out["normalized_request"] = self.normalized_request.model_dump(mode="json", exclude_unset=True)
        return out


class StudentReply(Model):
    snapshot: SnapshotRef
    data: dict
