import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from secrets import token_urlsafe
from tempfile import TemporaryDirectory

from jsonschema import Draft202012Validator
from starlette.testclient import TestClient

from forkcast.errors import Fault
from forkcast.http import STUDENT_COOKIE, OperatorCredential, create_app
from forkcast.mcp_access import ROLE_TOOLS, CallLimit, HostAccess
from forkcast.mcp_contracts import TOOLS
from forkcast.mcp_host import call_checked, connect
from forkcast.mcp_receipts import InteractionReceipts, InteractionRequest
from forkcast.mcp_server import PROTOCOL
from forkcast.store import OpsActor, SimulationStore
from forkcast.student_models import FeedbackRequest

API = "/api/v1"
ORIGIN = "http://localhost"
REQUEST = {"constraints": {"budget_max_cents": 2000, "time_budget_min": 45},
           "preferences": {"requested_dish_id": "D_A_DUCK"}}


async def discovered(session):
    return {item.name: item for item in (await session.list_tools()).tools}


class McpTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="forkcast-mcp-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "mcp.sqlite3"
        self.actor = OpsActor(principal_id="local_ops", allowed_venue_ids=("V_A", "V_B"), can_control_run=True)
        self.ops_token = token_urlsafe(32)
        self.app = create_app(self.path, "SIMULATION", OperatorCredential(self.ops_token, self.actor))
        self.client = self.web()
        self.run = {"run_id": self.reference()["run_id"]}
        self.store = SimulationStore(self.path)
        self.access = HostAccess(self.store)
        self.open_web(self.client)

    def web(self):
        client = TestClient(self.app, base_url=ORIGIN)
        self.addCleanup(client.close)
        return client

    def open_web(self, client):
        reply = client.post(API + "/session", json=self.run)
        self.assertEqual(reply.status_code, 200)
        return client.cookies.get(STUDENT_COOKIE)

    def bind(self, role, *, client=None, actor=None):
        return self.access.bind(self.run["run_id"], role, actor=actor or self.actor,
                                cookie=(client or self.client).cookies.get(STUDENT_COOKIE))

    def reference(self):
        return self.client.get(API + "/runs/current").json()["snapshot"]

    def ledger(self):
        with self.store.transaction() as db:
            return tuple(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                         for table in ("feedback_events", "snapshots", "audit"))

    def receipt(self, client, args, *, origin=ORIGIN, expected=200):
        response = client.post(API + "/interaction-receipts", headers={"Origin": origin}, json={
            **self.run, "tool": "submit_feedback", "action_arguments": args,
        })
        self.assertEqual(response.status_code, expected, response.text if expected != 200 else "")
        return response.json()

    async def test_a01_real_sdk_handshake_discovery_and_seven_schemas(self):
        union = set()
        for role, names in ROLE_TOOLS.items():
            async with connect(self.path, self.bind(role)) as session:
                await session.send_ping()
                tools = await discovered(session)
                self.assertEqual(tuple(tools), names)
                union.update(tools)
                for item in tools.values():
                    Draft202012Validator.check_schema(item.input_schema)
                    Draft202012Validator.check_schema(item.output_schema)
                    invalid = await call_checked(session, tools, item.name, {**self.run, "unrecognized_field": True})
                    self.assertEqual(invalid["error"]["code"], "INVALID_INPUT")
                self.assertTrue((await call_checked(session, tools, "get_dining_options", self.run))["ok"])
                failed = await call_checked(session, tools, "get_dining_options", {**self.run, "limit": "20"})
                self.assertEqual(failed["error"]["code"], "INVALID_INPUT")
        self.assertEqual(union, set(TOOLS))

    async def test_a02_http_equivalence_empty_decision_and_invalid_inputs(self):
        async with connect(self.path, self.bind("STUDENT")) as session:
            tools = await discovered(session)
            query = {**self.run, "snapshot_id": self.reference()["snapshot_id"]}
            mcp = await call_checked(session, tools, "get_dining_options", query)
            self.assertEqual(mcp, self.client.get(API + "/dining-options", params=query).json())
            request = {**self.run, "request": REQUEST}
            native = self.client.post(API + "/recommend", json=request).json()
            recommendation = await call_checked(session, tools, "recommend_meal", request)
            self.assertEqual(omit_rec(native), omit_rec(recommendation))
            previous = {**self.run, "previous_recommendation_id": native["data"]["recommendation_id"]}
            native_replan = self.client.post(API + "/replan", json=previous).json()
            tool_replan = await call_checked(session, tools, "replan_meal", previous)
            for reply in (native_replan, tool_replan):
                del reply["data"]["result"]["recommendation_id"]
            self.assertEqual(native_replan, tool_replan)
            self.assertEqual(tool_replan["data"]["outcome"], "UNCHANGED")
            no_options = await call_checked(session, tools, "recommend_meal", {
                **self.run, "request": {"constraints": {"budget_max_cents": 1}},
            })
            self.assertIsNone(no_options["data"]["primary"])
            self.assertEqual(no_options["data"]["status"], "NO_FEASIBLE_OPTION")
            invalid = ({**query, "cursor": "invalid"}, {**query, "venue_id": None},
                       {**query, "limit": True}, {**query, "data_mode": "LIVE"})
            for parameters in invalid:
                self.assertEqual((await call_checked(session, tools, "get_dining_options",
                                                     parameters))["error"]["code"], "INVALID_INPUT")
            missing = await call_checked(session, tools, "replan_meal",
                                         {**self.run, "previous_recommendation_id": "nonexistent"})
            self.assertEqual(missing["error"]["code"], "NOT_FOUND")

    async def test_a03_external_grant_rejects_direct_calls_and_foreign_records(self):
        http_recommendation = self.client.post(API + "/recommend", json={**self.run, "request": REQUEST}).json()
        before = self.ledger(), self.reference()
        async with connect(self.path, self.bind("EXTERNAL_AGENT")) as agent:
            tools = await discovered(agent)
            for tool in ("get_demand_insights", "generate_ops_recommendations", "confirm_ops_action", "submit_feedback"):
                denied = await call_checked(agent, tools, tool, {**self.run, "role": "OPS"})
                self.assertEqual(denied["error"]["code"], "FORBIDDEN")
            foreign = await call_checked(agent, tools, "replan_meal", {
                **self.run, "previous_recommendation_id": http_recommendation["data"]["recommendation_id"],
            })
            self.assertEqual(foreign["error"]["code"], "NOT_FOUND")
        self.assertEqual(before, (self.ledger(), self.reference()))

    async def test_a04_receipt_scope_expiry_replay_and_revocation(self):
        token = self.bind("STUDENT")
        args = {**self.run, "event_type": "PREFERENCE", "dish_id": "D_A_DUCK", "idempotency_key": "mcp-pref-01"}
        self.receipt(self.client, args, origin="http://invalid.example", expected=403)
        blocked = self.client.post(API + "/interaction-receipts", json={**self.run, "tool": "submit_feedback",
                                                                        "action_arguments": args})
        self.assertEqual(blocked.status_code, 403)
        expired = InteractionReceipts(self.store, now=lambda: 0).issue(
            InteractionRequest(**self.run, tool="submit_feedback", action_arguments=args),
            cookie=self.client.cookies.get(STUDENT_COOKIE),
        )[1]["interaction_receipt"]
        receipt = self.receipt(self.client, args)["data"]["interaction_receipt"]
        foreign = self.web()
        self.open_web(foreign)
        wrong_actor = self.receipt(foreign, args)["data"]["interaction_receipt"]
        before = self.ledger()
        async with connect(self.path, token) as session:
            tools = await discovered(session)
            async def feedback(change):
                return await call_checked(session, tools, "submit_feedback", {**args, **change})

            for change in ({}, {"interaction_receipt": expired}, {"interaction_receipt": token_urlsafe(32)},
                           {"interaction_receipt": wrong_actor}, {"interaction_receipt": receipt, "dish_id": "D_B_CHICKEN"},
                           {"interaction_receipt": receipt, "idempotency_key": "changed-key"}):
                self.assertEqual((await feedback(change))["error"]["code"], "CONFIRMATION_REQUIRED")
            self.assertEqual((await feedback({"interaction_receipt": receipt, "run_id": "other_run"}))
                             ["error"]["code"], "NOT_FOUND")
            self.assertEqual(before, self.ledger())
            approved = await feedback({"interaction_receipt": receipt})
            self.assertTrue(approved["ok"])
            self.assertFalse(approved["data"]["share_applied"])
            self.assertEqual(approved, await feedback({}))
            self.assertEqual((await feedback({"dish_id": "D_B_CHICKEN"}))["error"]["code"], "IDEMPOTENCY_CONFLICT")
            self.assertEqual(self.ledger(), (before[0] + 1, *before[1:]))
            self.access.revoke(token, cookie=self.client.cookies.get(STUDENT_COOKIE))
            self.assertEqual((await feedback({}))["error"]["code"], "UNAUTHENTICATED")
            self.assertEqual(await discovered(session), {})

    async def test_a05_ops_scope_unauthenticated_identity_and_live_no_database(self):
        query = {**self.run, "reference_window_id": self.run["run_id"] + ".W1"}
        scoped = OpsActor(principal_id=self.actor.principal_id, allowed_venue_ids=("V_A",), can_control_run=False)
        async with connect(self.path, self.bind("OPS", actor=scoped)) as session:
            tools = await discovered(session)
            visible = await call_checked(session, tools, "get_demand_insights", query)
            self.assertEqual({row["venue_id"] for row in visible["data"]["rows"]}, {"V_A"})
            denied = await call_checked(session, tools, "get_demand_insights", {**query, "venue_id": "V_B"})
            self.assertEqual(denied["error"]["code"], "FORBIDDEN")
            self.assertEqual((await call_checked(session, tools, "get_dining_options",
                                                 {**self.run, "principal_id": "local_ops"}))["error"]["code"], "INVALID_INPUT")
        async with connect(self.path, None) as anonymous:
            self.assertEqual(await discovered(anonymous), {})
            self.assertEqual((await call_checked(anonymous, {}, "get_demand_insights", query))
                             ["error"]["code"], "UNAUTHENTICATED")
        live = self.path.with_name("live.sqlite3")
        async with connect(live, None, mode="LIVE") as session:
            self.assertEqual(await discovered(session), {})
            self.assertEqual((await call_checked(session, {}, "get_dining_options", self.run))
                             ["error"]["code"], "MODE_DISABLED")
        self.assertFalse(live.exists())

    async def test_a06_reset_invalidates_old_binding_receipt_and_recommendation(self):
        previous = self.run
        token = self.bind("STUDENT")
        recommendation = self.client.post(API + "/recommend", json={**previous, "request": REQUEST}).json()["data"]
        args = {**previous, "event_type": "PREFERENCE", "dish_id": "D_A_DUCK", "idempotency_key": "reset-test-key"}
        receipt = self.receipt(self.client, args)["data"]["interaction_receipt"]
        async with connect(self.path, token) as initial:
            self.assertIn("submit_feedback", await discovered(initial))
        reset = self.client.post(API + "/admin/simulation/reset", headers={"Authorization": "Bearer " + self.ops_token},
                                 json={"scenario_id": "CLOSED_LOOP", "idempotency_key": "mcp-reset-01"}).json()
        self.run = {"run_id": self.reference()["run_id"]}
        self.assertEqual(self.run["run_id"], reset["snapshot"]["run_id"])
        self.assertNotEqual(self.run, previous)
        self.assertEqual(self.client.post(API + "/recommend", json={**previous, "request": {}}).status_code, 409)
        async with connect(self.path, token) as stale:
            self.assertEqual(await discovered(stale), {})
            denied = await call_checked(stale, {}, "submit_feedback", {**args, "interaction_receipt": receipt})
            self.assertEqual(denied["error"]["code"], "UNAUTHENTICATED")
        self.open_web(self.client)
        async with connect(self.path, self.bind("STUDENT")) as rebound:
            tools = await discovered(rebound)
            self.assertTrue((await call_checked(rebound, tools, "get_dining_options", self.run))["ok"])
            reply = await call_checked(rebound, tools, "replan_meal", {
                **self.run, "previous_recommendation_id": recommendation["recommendation_id"],
            })
            self.assertEqual(reply["error"]["code"], "NOT_FOUND")
            denied = await call_checked(rebound, tools, "submit_feedback", {**args, **self.run, "interaction_receipt": receipt})
            self.assertEqual(denied["error"]["code"], "CONFIRMATION_REQUIRED")

    def test_rolling_limit_shared_by_principal_and_anonymous_per_connection(self):
        now = [1000.0]
        left = CallLimit(self.access, self.bind("OPS"), now=lambda: now[0])
        right = CallLimit(self.access, self.bind("OPS"), now=lambda: now[0])
        anonymous = CallLimit(self.access, None, now=lambda: now[0])
        separate = CallLimit(self.access, None, now=lambda: now[0])
        for index in range(120):
            (left if index % 2 else right).charge()
            anonymous.charge()
        for subject in (left, right, anonymous):
            with self.assertRaises(Fault) as error:
                subject.charge()
            self.assertEqual((error.exception.code, error.exception.details), ("RATE_LIMITED", {"retry_after_seconds": 60}))
        self.assertIsNone(separate.charge())
        now[0] += 59.5
        with self.assertRaises(Fault) as error:
            right.charge()
        self.assertEqual(error.exception.details, {"retry_after_seconds": 1})
        now[0] += 0.5
        self.assertIsNotNone(left.charge())
        self.assertIsNone(anonymous.charge())

    def test_receipt_tool_binding_and_transaction_rollback(self):
        args = {**self.run, "event_type": "PREFERENCE", "dish_id": "D_A_DUCK", "idempotency_key": "receipt-rollback"}
        receipt = self.receipt(self.client, args)["data"]["interaction_receipt"]
        model = FeedbackRequest.model_validate(args)
        service = InteractionReceipts(self.store)
        before = self.ledger()
        with self.store.transaction() as db:
            principal = db.execute("SELECT principal_id FROM student_sessions WHERE token_digest IS NOT NULL "
                                   "AND session_kind='USABILITY_TEST'").fetchone()[0]
        with self.assertRaises(Fault) as error, self.store.transaction(write=True) as db:
            service.approval("confirm_ops_action", receipt)(db, principal, model)
        self.assertEqual(error.exception.code, "CONFIRMATION_REQUIRED")
        with self.assertRaisesRegex(RuntimeError, "rollback"), self.store.transaction(write=True) as db:
            service.approval("submit_feedback", receipt)(db, principal, model)
            raise RuntimeError("rollback")
        result = service.student.feedback(model, self.client.cookies.get(STUDENT_COOKIE),
                                          approval=service.approval("submit_feedback", receipt))
        self.assertEqual(result.data["event_type"], "PREFERENCE")
        self.assertEqual(self.ledger(), (before[0] + 1, *before[1:]))

    def test_protocol_errors_keep_request_ids_and_stdout_contains_only_frames(self):
        frames = [
            b"{broken", {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": PROTOCOL, "clientInfo": {"name": "test", "version": "1"},
                                     "capabilities": {}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": "unknown", "method": "tools/call", "params": {"name": "ops_state"}},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_dining_options", "arguments": []}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "get_dining_options", "arguments": {}}},
        ]
        result = subprocess.run([sys.executable, "-B", "-m", "forkcast.mcp_server"],
                                input=b"".join((frame if isinstance(frame, bytes) else json.dumps(frame).encode()) + b"\n"
                                               for frame in frames),
                                env={**os.environ, "FORKCAST_DATABASE": str(self.path), "FORKCAST_DATA_MODE": "SIMULATION",
                                     "FORKCAST_MCP_TOKEN": self.bind("EXTERNAL_AGENT")},
                                capture_output=True, timeout=15, check=True)
        self.assertEqual(result.stderr, b"")
        decoded = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([row["id"] for row in decoded], [None, 1, "unknown", 3, 4])
        self.assertEqual([decoded[i]["error"]["code"] for i in (0, 2, 3)], [-32700, -32602, -32602])
        self.assertEqual(decoded[4]["result"]["structuredContent"]["error"]["code"], "INVALID_INPUT")


def omit_rec(envelope):
    return {**envelope, "data": {key: value for key, value in envelope["data"].items() if key != "recommendation_id"}}
