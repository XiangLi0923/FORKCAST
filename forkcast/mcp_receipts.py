"""Human-granted receipts consumed by the shared application write transaction."""

import json
from datetime import UTC, datetime
from secrets import token_urlsafe
from time import time
from typing import Literal

from .errors import Fault
from .mcp_access import OPS_WRITE, STUDENT_WRITE, digest_token
from .models import Model
from .ops_execution import OperationsExecutionService
from .ops_execution_models import ConfirmOpsRequest
from .student_models import FeedbackRequest, RunRequest
from .student_service import StudentService

ACTION_INPUTS = {STUDENT_WRITE: FeedbackRequest, OPS_WRITE: ConfirmOpsRequest}


class InteractionRequest(RunRequest):
    tool: Literal["submit_feedback", "confirm_ops_action"]
    action_arguments: dict


def action_digest(request: Model) -> str:
    return digest_token(json.dumps(request.model_dump(mode="json", exclude={"idempotency_key"}),
                                   sort_keys=True, separators=(",", ":")))


class InteractionReceipts:
    def __init__(self, store, *, now=time):
        self.store = store
        self.student = StudentService(store)
        self.now = now

    def issue(self, incoming: InteractionRequest, *, actor=None, cookie=None):
        request = ACTION_INPUTS[incoming.tool].model_validate(incoming.action_arguments)
        if request.run_id != incoming.run_id:
            raise Fault("INVALID_INPUT", "The action must use the same run as the receipt request.")
        with self.store.transaction(write=True) as db:
            if incoming.tool == OPS_WRITE:
                if actor is None:
                    raise Fault("UNAUTHENTICATED", "An operator is required to grant this action.")
                self.store._run(db, request.run_id)
                OperationsExecutionService.proposal_scope(db, request, actor)
                principal = actor.principal_id
            else:
                run, session = self.student._identity(db, request.run_id, cookie)
                principal = session["principal_id"]
                snapshot, catalog = self.student._supplies(db, run)
                self.student.feedback_scope(db, request, principal, snapshot, catalog)
            now = self.now()
            db.execute("DELETE FROM mcp_interactions WHERE run_id=? AND expires_at<=?", (request.run_id, now))
            token = token_urlsafe(32)
            expires = now + 120
            db.execute("INSERT INTO mcp_interactions VALUES (?,?,?,?,?,?,?)",
                       (digest_token(token), request.run_id, principal, incoming.tool, action_digest(request),
                        request.idempotency_key, expires))
            ref = self.store._frame(db, request.run_id).supply.ref
            return ref, {"interaction_receipt": token, "expires_at": datetime.fromtimestamp(expires, UTC).isoformat()}

    def approval(self, tool, receipt):
        def consume(db, principal, request):
            if receipt is None:
                raise Fault("CONFIRMATION_REQUIRED", "A trusted Web action receipt is required.")
            result = db.execute(
                """DELETE FROM mcp_interactions WHERE token_digest=? AND run_id=? AND principal_id=? AND tool=?
                   AND argument_digest=? AND idempotency_key=? AND expires_at>?""",
                (digest_token(receipt), request.run_id, principal, tool, action_digest(request),
                 request.idempotency_key, self.now()),
            )
            if not result.rowcount:
                raise Fault("CONFIRMATION_REQUIRED", "The action receipt is invalid, expired or already used.")
        return consume
