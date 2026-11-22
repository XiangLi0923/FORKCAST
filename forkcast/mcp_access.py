"""Server-owned, run-scoped MCP grants and per-subject rate limiting."""

import json
from collections import deque
from hashlib import sha256
from math import ceil
from secrets import token_urlsafe
from time import time

from .errors import Fault
from .fixture import Fixture
from .store import OpsActor, SimulationStore
from .student_models import OpenSession
from .student_service import StudentService

STUDENT_WRITE = "submit_feedback"
OPS_WRITE = "confirm_ops_action"
AGENT_TOOLS = ("get_dining_options", "recommend_meal", "replan_meal")
ROLE_TOOLS = {
    "EXTERNAL_AGENT": AGENT_TOOLS,
    "STUDENT": (*AGENT_TOOLS, STUDENT_WRITE),
    "OPS": ("get_dining_options", "get_demand_insights", "generate_ops_recommendations", OPS_WRITE),
}


def digest_token(token: str) -> str:
    return sha256(token.encode("utf-8")).hexdigest()


class HostAccess:
    def __init__(self, store: SimulationStore):
        self.store = store
        self.student = StudentService(store)

    def bind(self, run_id: str, role: str, *, actor=None, cookie=None, tools=None) -> str:
        allowed = ROLE_TOOLS[role]
        scope = allowed if tools is None else tuple(dict.fromkeys(tools))
        if not set(scope).issubset(allowed):
            raise Fault("FORBIDDEN", "The local grant exceeds the role scope.")
        if role == "EXTERNAL_AGENT":
            with self.store.transaction() as db:
                run = self.store._run(db, run_id)
                if actor is None:
                    raise Fault("UNAUTHENTICATED", "An operator must provision the Agent.")
                self.store._authorize(actor, Fixture.model_validate_json(run["fixture_json"]), None)
            _, cookie = self.student.open_session(OpenSession(run_id=run_id), None)
        with self.store.transaction(write=True) as db:
            self.store._run(db, run_id)
            if role == "OPS":
                if actor is None:
                    raise Fault("UNAUTHENTICATED", "An operator is required to bind this host.")
                principal, venues = actor.principal_id, actor.allowed_venue_ids
            else:
                _, student = self.student._identity(db, run_id, cookie)
                principal, venues = student["principal_id"], ()
            token = token_urlsafe(32)
            db.execute("INSERT INTO mcp_hosts VALUES (?,?,?,?,?,?)",
                       (digest_token(token), run_id, principal, role, json.dumps(venues), json.dumps(scope)))
            return token

    def revoke(self, token: str, *, actor=None, cookie=None):
        with self.store.transaction(write=True) as db:
            grant = self.lookup(db, token)
            if grant is None:
                return
            run_id = grant["run_id"]
            if grant["role"] == "STUDENT":
                _, student = self.student._identity(db, run_id, cookie)
                permitted = student["principal_id"] == grant["principal_id"]
            elif grant["role"] == "OPS":
                permitted = actor is not None and actor.principal_id == grant["principal_id"]
            else:
                run = self.store._run(db, run_id)
                permitted = actor is not None and actor.can_control_run and {
                    venue.venue_id for venue in Fixture.model_validate_json(run["fixture_json"]).venues
                }.issubset(actor.allowed_venue_ids)
            if not permitted:
                raise Fault("FORBIDDEN", "The current identity cannot revoke this host.")
            db.execute("DELETE FROM mcp_hosts WHERE token_digest=?", (digest_token(token),))

    @staticmethod
    def lookup(db, token: str | None):
        if not token or len(token) > 512:
            return None
        return db.execute(
            """SELECT h.* FROM mcp_hosts h JOIN runs r USING (run_id)
               WHERE h.token_digest=? AND (h.role='OPS' OR EXISTS (
                 SELECT 1 FROM student_sessions s WHERE s.run_id=h.run_id AND s.principal_id=h.principal_id
               ))""", (digest_token(token),),
        ).fetchone()

    def require(self, db, token, tool, run_id):
        grant = self.lookup(db, token)
        if grant is None:
            raise Fault("UNAUTHENTICATED", "Bind this local host to an active identity.")
        if tool not in json.loads(grant["tools_json"]):
            raise Fault("FORBIDDEN", "The host is not authorized for this tool.")
        if run_id != grant["run_id"]:
            raise Fault("NOT_FOUND", "Run not found.")
        return grant

    def student_authority(self, token, tool):
        return lambda db, run_id: self.require(db, token, tool, run_id)["principal_id"]

    def operator_authority(self, token, tool):
        def authorize(db, run_id):
            grant = self.require(db, token, tool, run_id)
            return OpsActor(principal_id=grant["principal_id"], allowed_venue_ids=tuple(json.loads(grant["venues_json"])),
                            can_control_run=False)
        return authorize


class CallLimit:
    def __init__(self, access: HostAccess, token: str | None, *, now=time):
        self.access = access
        self.token = token
        self.now = now
        self.anonymous = deque()

    def charge(self):
        now = self.now()
        cutoff = now - 60
        with self.access.store.transaction(write=True) as db:
            grant = self.access.lookup(db, self.token)
            if grant is None:
                while self.anonymous and self.anonymous[0] <= cutoff:
                    self.anonymous.popleft()
                if len(self.anonymous) >= 120:
                    raise rate_fault(self.anonymous[0], now)
                self.anonymous.append(now)
                return None
            subject = grant["run_id"], grant["principal_id"]
            db.execute("DELETE FROM mcp_calls WHERE run_id=? AND principal_id=? AND happened_at<=?", (*subject, cutoff))
            count, earliest = db.execute(
                "SELECT count(*),min(happened_at) FROM mcp_calls WHERE run_id=? AND principal_id=?", subject,
            ).fetchone()
            if count >= 120:
                raise rate_fault(earliest, now)
            db.execute("INSERT INTO mcp_calls VALUES (?,?,?)", (*subject, now))
            return grant


def rate_fault(first, now):
    return Fault("RATE_LIMITED", "MCP call limit reached.", details={"retry_after_seconds": max(1, ceil(first + 60 - now))})
