"""Bind authenticated tool requests to shared FORKCAST application services."""

import json
import logging
import sqlite3
from uuid import uuid4

from jsonschema import Draft202012Validator
from pydantic import ValidationError

from .demand_insights import DemandAggregationService
from .errors import Fault
from .mcp_access import OPS_WRITE, STUDENT_WRITE, CallLimit, HostAccess
from .mcp_contracts import TOOLS
from .mcp_receipts import InteractionReceipts
from .ops_execution import OperationsExecutionService
from .ops_service import OperationsAnalysisService
from .student_service import StudentService


def validator_for(name, tool):
    schema = tool.schema(name)["inputSchema"]
    if tool.receipt:
        schema["required"].remove("interaction_receipt")
    return Draft202012Validator(schema)


INPUTS = {name: validator_for(name, tool) for name, tool in TOOLS.items()}


def success(ref, data):
    return {"schema_version": "0.3", "ok": True, "data_mode": ref.data_mode,
            "snapshot": ref.model_dump(mode="json"), "data": data}


def failure(fault):
    ref = fault.snapshot
    error = {"code": fault.code, "message": str(fault), "retryable": fault.code in (
        "SNAPSHOT_CONFLICT", "STALE_PROPOSAL", "STALE_SELECTION", "RUN_REPLACED", "RATE_LIMITED", "DEPENDENCY_UNAVAILABLE",
    )}
    if fault.details is not None:
        error["details"] = fault.details
    return {"schema_version": "0.3", "ok": False, "data_mode": ref.data_mode if ref else None,
            "snapshot": ref.model_dump(mode="json") if ref else None, "error": error}


class Gateway:
    def __init__(self, store, token, startup_fault=None):
        self.store = store
        self.token = token
        self.startup_fault = startup_fault
        self.access = HostAccess(store)
        self.limit = CallLimit(self.access, token)
        self.student = StudentService(store)
        self.receipts = InteractionReceipts(store)
        self.demand = DemandAggregationService(store)
        self.operations = OperationsAnalysisService(store)
        self.execution = OperationsExecutionService(store)

    def discover(self):
        if self.startup_fault is not None:
            return []
        with self.store.transaction() as db:
            grant = self.access.lookup(db, self.token)
            return [TOOLS[name].schema(name) for name in json.loads(grant["tools_json"])] if grant else []

    def count_unknown_call(self):
        if self.startup_fault is None:
            try:
                self.limit.charge()
            except (Fault, sqlite3.Error):
                pass  # This request still receives its protocol-level unknown-tool error.

    def invoke(self, name, arguments):
        try:
            if self.startup_fault is not None:
                raise self.startup_fault
            grant = self.limit.charge()
            if grant is None:
                raise Fault("UNAUTHENTICATED", "Bind this local host to an active identity.")
            if name not in json.loads(grant["tools_json"]):
                raise Fault("FORBIDDEN", "The host is not authorized for this tool.")
            if not INPUTS[name].is_valid(arguments):
                raise Fault("INVALID_INPUT", "Tool arguments do not match the public schema.")
            args = dict(arguments)
            receipt = args.pop("interaction_receipt", None)
            request = TOOLS[name].request.model_validate_json(json.dumps(args))
            envelope = self.dispatch(name, request, receipt, grant["role"])
        except ValidationError:
            envelope = failure(Fault("INVALID_INPUT", "Tool arguments do not match the public schema."))
        except Fault as fault:
            envelope = failure(fault)
        except sqlite3.Error as exc:
            if getattr(exc, "sqlite_errorcode", 0) & 255 in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                envelope = failure(Fault("DEPENDENCY_UNAVAILABLE", "The local store is busy."))
            else:
                envelope = failure(internal_fault())
        except Exception:
            envelope = failure(internal_fault())
        return {"isError": not envelope["ok"], "structuredContent": envelope,
                "content": [{"type": "text", "text": json.dumps(envelope, ensure_ascii=False, allow_nan=False)}]}

    def dispatch(self, name, request, receipt, role):
        if role == "OPS":
            actor = self.access.operator_authority(self.token, name)
            if name == "get_demand_insights":
                ref, result = self.demand.read(request, actor)
                return success(ref, result.model_dump(mode="json"))
            if name == "generate_ops_recommendations":
                ref, result = self.operations.generate(request, actor)
                return success(ref, result.model_dump(mode="json"))
            if name == OPS_WRITE:
                result = self.execution.confirm(request, actor, approval=self.receipts.approval(name, receipt))
                return success(result.confirmation_snapshot, result.model_dump(mode="json"))
            result = self.student.dining(request, None, actor)
        else:
            student = self.access.student_authority(self.token, name)
            if name == STUDENT_WRITE:
                result = self.student.feedback(request, student, approval=self.receipts.approval(name, receipt))
            else:
                handler = {"get_dining_options": self.student.dining, "recommend_meal": self.student.recommend,
                           "replan_meal": self.student.replan}[name]
                result = handler(request, student)
        return success(result.snapshot, result.data)


def internal_fault():
    trace = f"mcp{uuid4().hex}"
    logging.getLogger(__name__).error("MCP call failed (trace %s)", trace)
    return Fault("INTERNAL_ERROR", "The local tool could not complete this request.", details={"trace_id": trace})
