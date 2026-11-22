"""HTTP entry points for the FORKCAST simulation service."""

import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from hmac import compare_digest
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from .demand_insights import DemandAggregationService, DemandInsightsQuery
from .errors import Fault
from .models import ID, Model, SnapshotRef
from .mcp_access import OPS_WRITE
from .mcp_receipts import InteractionReceipts, InteractionRequest
from .ops_analysis import OpsProposalsRequest
from .ops_execution import OperationsExecutionService
from .ops_execution_models import ConfirmOpsRequest, OpsStateQuery
from .ops_service import OperationsAnalysisService
from .store import ControlRequest, DataProvider, MockProvider, OpsActor, ResetRequest, SimulationStore
from .student_models import (
    ConsentRequest, DemoEnrollmentRequest, DiningQuery, FeedbackRequest, OpenSession, RecommendRequest, ReplanRequest,
)
from .student_service import StudentService

VERSION = "0.3.0-m3.3"
STUDENT_COOKIE = "forkcast_student"
WEB_ROOT = Path(__file__).with_name("web")
WEB_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}
MODES = ("SIMULATION", "LIVE")
STATUS = {
    "INVALID_INPUT": 400, "UNAUTHENTICATED": 401, "FORBIDDEN": 403, "MODE_DISABLED": 403,
    "NOT_FOUND": 404, "RUN_REPLACED": 409, "STALE_SELECTION": 409, "SNAPSHOT_CONFLICT": 409,
    "IDEMPOTENCY_CONFLICT": 409, "INTERNAL_ERROR": 500, "DEPENDENCY_UNAVAILABLE": 503,
    "STALE_PROPOSAL": 409, "OPS_ALREADY_CONFIRMED": 409,
    "CONFIRMATION_REQUIRED": 403, "RATE_LIMITED": 429,
}


class SnapshotQuery(Model):
    run_id: ID


@dataclass(frozen=True)
class OperatorCredential:
    token: str
    actor: OpsActor


@dataclass(frozen=True)
class PublicDeployment:
    origin: str
    upstream_host: str

    def __post_init__(self):
        try:
            origin = urlsplit(self.origin)
            hostname = origin.hostname or ""
            upstream_host = self.upstream_host
        except ValueError:
            raise ValueError("Invalid FORKCAST public origin or upstream host.") from None
        dns_name = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+"
        if (not re.fullmatch(dns_name, hostname) or self.origin != f"https://{hostname}"
                or not re.fullmatch(dns_name, upstream_host)):
            raise ValueError("FORKCAST requires an exact HTTPS public origin and an exact upstream hostname.")

    @property
    def hosts(self) -> list[str]:
        return [urlsplit(self.origin).hostname, self.upstream_host]


def success(ref: SnapshotRef, data: dict) -> JSONResponse:
    return JSONResponse({
        "schema_version": "0.3", "ok": True, "data_mode": ref.data_mode,
        "snapshot": ref.model_dump(mode="json"), "data": data,
    }, headers={"Cache-Control": "no-store"})


def failure(error: Fault) -> JSONResponse:
    ref = error.snapshot
    return JSONResponse({
        "schema_version": "0.3", "ok": False, "data_mode": ref.data_mode if ref else None,
        "snapshot": ref.model_dump(mode="json") if ref else None,
        "error": {"code": error.code, "message": str(error),
                  "retryable": error.code in ("SNAPSHOT_CONFLICT", "DEPENDENCY_UNAVAILABLE")},
    }, status_code=STATUS[error.code], headers={"Cache-Control": "no-store"})


def expected_origin(request: Request) -> str:
    return request.app.state.public_origin or f"{request.url.scheme}://{request.url.netloc}"


def check_origin(request: Request, *, required: bool = False):
    origin = request.headers.get("origin")
    if ((required or origin is not None) and origin != expected_origin(request)) or (
        request.headers.get("sec-fetch-site") in ("cross-site", "same-site")
    ):
        raise Fault("FORBIDDEN", "This operation requires the configured same origin.")


def no_query(request: Request):
    if request.query_params:
        raise Fault("INVALID_INPUT", "Unexpected query parameters.")


async def parse_json(request: Request, cls: type[Model]):
    if request.headers.get("content-type", "").partition(";")[0].strip().lower() != "application/json":
        raise Fault("INVALID_INPUT", "Expected JSON request body.")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 16 * 1024:
            raise Fault("INVALID_INPUT", "JSON request exceeds the local control limit.")
        body.extend(chunk)
    return cls.model_validate_json(body)


def parse_query(request: Request, cls: type[Model]):
    entries = request.query_params.multi_items()
    data = dict(entries)
    if len(entries) != len(data):
        raise Fault("INVALID_INPUT", "Repeated query parameters are not allowed.")
    if "limit" in data:
        if not re.fullmatch(r"[0-9]{1,4}", data["limit"]):
            raise Fault("INVALID_INPUT", "limit must be a decimal integer.")
        data["limit"] = int(data["limit"])
    return cls.model_validate(data)


def create_app(database: Path, mode: str = "SIMULATION", credential: OperatorCredential | None = None,
               *, public: PublicDeployment | None = None):
    store = SimulationStore(database)
    provider: DataProvider = MockProvider(store)
    student = StudentService(store)
    demand = DemandAggregationService(store)
    operations = OperationsAnalysisService(store)
    execution = OperationsExecutionService(store)
    interactions = InteractionReceipts(store)
    startup_fault = None
    try:
        if mode not in MODES:
            raise Fault("INVALID_INPUT", "Unknown service data mode.")
        store.bootstrap(mode)
    except Fault as error:
        startup_fault = error
        logging.getLogger(__name__).warning("FORKCAST startup rejected: %s", error.code)

    def ready():
        if startup_fault is not None:
            raise startup_fault

    def operator(request: Request) -> OpsActor:
        authorization = request.headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        if (credential is None or scheme.lower() != "bearer" or not token or not compare_digest(
            token.encode("utf-8"), credential.token.encode("utf-8")
        )):
            raise Fault("UNAUTHENTICATED", "Local operator credential required.")
        return credential.actor

    async def health(_request: Request):
        return JSONResponse({
            "ready": startup_fault is None, "app_version": VERSION,
            "recognized_modes": list(MODES), "enabled_modes": ["SIMULATION"],
        }, status_code=200 if startup_fault is None else 503)

    async def current(request: Request):
        check_origin(request)
        no_query(request)
        ready()
        result = await run_in_threadpool(store.current)
        return success(result.snapshot, {"scenario_id": result.scenario_id})

    async def snapshot(request: Request):
        check_origin(request)
        ready()
        query = parse_query(request, SnapshotQuery)
        if "authorization" not in request.headers:
            result = await run_in_threadpool(student.snapshot, query, request.cookies.get(STUDENT_COOKIE))
            return success(result.snapshot, result.data)
        actor = operator(request)
        frame, catalog = await run_in_threadpool(provider.read, query.run_id)
        if not {venue.venue_id for venue in catalog.venues}.intersection(actor.allowed_venue_ids):
            raise Fault("FORBIDDEN", "Operator has no venue in this run.")
        return success(frame.ref, frame.ref.model_dump(mode="json"))

    async def session(request: Request):
        check_origin(request)
        no_query(request)
        ready()
        payload = await parse_json(request, OpenSession)
        result, token = await run_in_threadpool(student.open_session, payload, request.cookies.get(STUDENT_COOKIE))
        response = success(result.snapshot, result.data)
        if token is not None:
            # HTTP is restricted to loopback; HTTPS deployments also require a Secure cookie.
            response.set_cookie(STUDENT_COOKIE, token, path="/", httponly=True, samesite="strict",
                                secure=public is not None or request.url.scheme == "https")
        return response

    async def enroll(request: Request):
        check_origin(request)
        no_query(request)
        ready()
        actor = operator(request)
        payload = await parse_json(request, DemoEnrollmentRequest)
        result = await run_in_threadpool(student.issue_enrollment, payload, actor)
        return success(result.snapshot, result.data)

    async def dining(request: Request):
        check_origin(request)
        ready()
        query = parse_query(request, DiningQuery)
        actor = operator(request) if "authorization" in request.headers else None
        result = await run_in_threadpool(student.dining, query, request.cookies.get(STUDENT_COOKIE), actor)
        return success(result.snapshot, result.data)

    async def demand_insights(request: Request):
        check_origin(request)
        actor = operator(request)
        ready()
        query = parse_query(request, DemandInsightsQuery)
        ref, result = await run_in_threadpool(demand.read, query, actor)
        return success(ref, result.model_dump(mode="json"))

    async def ops_proposals(request: Request):
        check_origin(request)
        no_query(request)
        actor = operator(request)
        ready()
        payload = await parse_json(request, OpsProposalsRequest)
        ref, result = await run_in_threadpool(operations.generate, payload, actor)
        return success(ref, result.model_dump(mode="json"))

    async def ops_confirmations(request: Request):
        check_origin(request)
        no_query(request)
        actor = operator(request)
        ready()
        payload = await parse_json(request, ConfirmOpsRequest)
        result = await run_in_threadpool(execution.confirm, payload, actor)
        return success(result.confirmation_snapshot, result.model_dump(mode="json"))

    async def ops_state(request: Request):
        check_origin(request)
        actor = operator(request)
        ready()
        query = parse_query(request, OpsStateQuery)
        ref, result = await run_in_threadpool(execution.state, query, actor)
        return success(ref, result.model_dump(mode="json"))

    async def interaction_receipt(request: Request):
        check_origin(request, required=True)
        no_query(request)
        ready()
        payload = await parse_json(request, InteractionRequest)
        actor = operator(request) if payload.tool == OPS_WRITE else None
        ref, data = await run_in_threadpool(interactions.issue, payload, actor=actor,
                                            cookie=request.cookies.get(STUDENT_COOKIE))
        return success(ref, data)

    def student_write(model, action):
        async def endpoint(request: Request):
            check_origin(request)
            no_query(request)
            ready()
            payload = await parse_json(request, model)
            result = await run_in_threadpool(action, payload, request.cookies.get(STUDENT_COOKIE))
            return success(result.snapshot, result.data)
        return endpoint

    async def reset(request: Request):
        check_origin(request)
        no_query(request)
        ready()
        actor = operator(request)
        reset_request = await parse_json(request, ResetRequest)
        result = await run_in_threadpool(store.reset, reset_request, actor)
        ref = result.snapshot
        return success(ref, {"scenario_id": result.scenario_id, "run_id": ref.run_id})

    async def event(request: Request):
        check_origin(request)
        no_query(request)
        ready()
        actor = operator(request)
        command = await parse_json(request, ControlRequest)
        result = await run_in_threadpool(store.control, command, actor)
        return success(result.snapshot, result.snapshot.model_dump(mode="json"))

    async def student_home(_request: Request):
        ready()
        return FileResponse(WEB_ROOT / "index.html", media_type="text/html", headers=WEB_HEADERS)

    async def ops_home(_request: Request):
        ready()
        return FileResponse(WEB_ROOT / "ops.html", media_type="text/html", headers=WEB_HEADERS)

    async def student_asset(request: Request):
        name = request.path_params["name"]
        assets = {
            "student.css": "text/css", "student.mjs": "text/javascript", "decision_gate.mjs": "text/javascript",
            "ops.css": "text/css", "ops.mjs": "text/javascript", "mcp-receipt.mjs": "text/javascript",
        }
        if name not in assets:
            raise Fault("NOT_FOUND", "Asset not found.")
        return FileResponse(WEB_ROOT / name, media_type=assets[name], headers=WEB_HEADERS)

    async def fault_handler(_request, error):
        return failure(error)

    async def validation_handler(_request, _error):
        return failure(Fault("INVALID_INPUT", "Request fields do not match the API contract."))

    async def database_handler(_request, error):
        if isinstance(error, sqlite3.OperationalError) and getattr(error, "sqlite_errorname", "") in (
            "SQLITE_BUSY", "SQLITE_LOCKED"
        ):
            return failure(Fault("DEPENDENCY_UNAVAILABLE", "Simulation store is busy."))
        return failure(Fault("INTERNAL_ERROR", "Simulation store could not complete the operation."))

    app = Starlette(
        routes=[
            Route("/", student_home),
            Route("/ops", ops_home),
            Route("/student-assets/{name}", student_asset),
            Route("/health", health),
            Route("/api/v1/runs/current", current),
            Route("/api/v1/snapshot", snapshot),
            Route("/api/v1/session", session, methods=["POST"]),
            Route("/api/v1/dining-options", dining),
            Route("/api/v1/admin/demand-insights", demand_insights),
            Route("/api/v1/admin/ops-proposals", ops_proposals, methods=["POST"]),
            Route("/api/v1/admin/ops-confirmations", ops_confirmations, methods=["POST"]),
            Route("/api/v1/admin/ops-state", ops_state),
            Route("/api/v1/interaction-receipts", interaction_receipt, methods=["POST"]),
            Route("/api/v1/recommend", student_write(RecommendRequest, student.recommend), methods=["POST"]),
            Route("/api/v1/replan", student_write(ReplanRequest, student.replan), methods=["POST"]),
            Route("/api/v1/feedback", student_write(FeedbackRequest, student.feedback), methods=["POST"]),
            Route("/api/v1/consent/ops-sharing", student_write(ConsentRequest, student.consent), methods=["PUT"]),
            Route("/api/v1/admin/simulation/demo-enrollments", enroll, methods=["POST"]),
            Route("/api/v1/admin/simulation/reset", reset, methods=["POST"]),
            Route("/api/v1/admin/simulation/events", event, methods=["POST"]),
        ],
        exception_handlers={Fault: fault_handler, ValidationError: validation_handler, sqlite3.Error: database_handler},
        middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=public.hosts if public else ["localhost", "127.0.0.1"],
                               www_redirect=False)],
    )
    app.state.public_origin = public.origin if public else None
    return app


def operator_credential_from_env():
    token = os.getenv("FORKCAST_OPS_TOKEN")
    if token:
        venues = tuple(item.strip() for item in os.getenv("FORKCAST_OPS_VENUES", "V_A,V_B").split(","))
        return OperatorCredential(token, OpsActor(
            principal_id="local_ops", allowed_venue_ids=venues,
            can_control_run=os.getenv("FORKCAST_OPS_RUN_CONTROL", "1") == "1",
        ))


def from_env():
    origin = os.getenv("FORKCAST_PUBLIC_ORIGIN")
    upstream_host = os.getenv("FORKCAST_UPSTREAM_HOST") or os.getenv("RENDER_EXTERNAL_HOSTNAME")
    public = None
    if origin is not None or upstream_host is not None:
        public = PublicDeployment(origin or "", upstream_host or "")
    return create_app(
        Path(os.getenv("FORKCAST_DATABASE", "forkcast.sqlite3")),
        os.getenv("FORKCAST_DATA_MODE", "SIMULATION"),
        operator_credential_from_env(),
        public=public,
    )
