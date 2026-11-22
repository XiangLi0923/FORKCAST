"""Run E03 against real loopback HTTP and three SDK-driven local MCP processes."""

import asyncio
from contextlib import AsyncExitStack
from http.cookiejar import CookieJar
from urllib.parse import urlencode
from uuid import uuid4

from forkcast.http import STUDENT_COOKIE
from forkcast.mcp_access import HostAccess
from forkcast.mcp_host import call_checked, connect
from forkcast.store import OpsActor, SimulationStore
from tests.demand_http_smoke import http_client, run_service, wait_ready
from tests.test_mcp import REQUEST, discovered, omit_rec

API = "/api/v1"


def smoke(port, token, database):
    cookies = CookieJar()
    http = http_client(port, token, cookies=cookies)
    wait_ready(http)
    actor = OpsActor(principal_id="local_ops", allowed_venue_ids=("V_A", "V_B"), can_control_run=True)
    access = HostAccess(SimulationStore(database))

    def current():
        return http(API + "/runs/current")["snapshot"]

    def post(path, fields, *, ops=False, expected=200, origin=None):
        return http(API + path, method="POST", fields=fields, ops=ops, expected=expected, origin=origin)

    def get(path, fields, *, ops=False):
        return http(API + path + "?" + urlencode(fields), ops=ops)

    def edit(path, fields, *, ops=False, method="POST"):
        return http(API + path, method=method, ops=ops, fields={**fields, "idempotency_key": uuid4().hex})

    def authorization(tool, args, *, ops=False):
        return post("/interaction-receipts", {"run_id": args["run_id"], "tool": tool, "action_arguments": args},
                    ops=ops, origin=f"http://127.0.0.1:{port}")["data"]["interaction_receipt"]

    async def flow():
        initial = current()
        run_id = initial["run_id"]
        run = {"run_id": run_id}

        def event(kind, payload):
            return edit("/admin/simulation/events", {
                **run, "expected_snapshot_id": current()["snapshot_id"], "event_type": kind, "payload": payload,
            }, ops=True)["snapshot"]

        def clock(time):
            return event("ADVANCE_CLOCK", {"to_clock_at": initial["clock_at"][:10] + "T" + time + "+08:00"})

        def menu(ref):
            return {**run, "snapshot_id": ref["snapshot_id"], "venue_id": "V_A"}

        async def host(stack, role, **credentials):
            grant = access.bind(run_id, role, **credentials)
            session = await stack.enter_async_context(connect(database, grant))
            tools = await discovered(session)
            async def call(name, args):
                return await call_checked(session, tools, name, args)
            return call

        async with AsyncExitStack() as stack:
            agent = await host(stack, "EXTERNAL_AGENT", actor=actor)
            post("/session", run)
            assert await agent("get_dining_options", menu(initial)) == get("/dining-options", menu(initial))
            agent_rec = await agent("recommend_meal", {**run, "request": REQUEST})
            assert (agent_rec["data"]["primary"]["match_score"], agent_rec["data"]["alternatives"][0]["match_score"]) == (644, 480)
            clock("11:41:00")
            event("SET_SOLD_OUT", {"dish_id": "D_A_DUCK"})
            replan = await agent("replan_meal", {**run, "previous_recommendation_id": agent_rec["data"]["recommendation_id"]})
            assert replan["data"]["result"]["primary"]["bundle_id"] == "B_B_CHICKEN"
            assert any(row["code"] == "SOLD_OUT" for row in replan["data"]["change_reasons"])
            assert (await agent("submit_feedback", run))["error"]["code"] == "FORBIDDEN"
            foreign = post("/recommend", {**run, "request": REQUEST})["data"]["recommendation_id"]
            assert (await agent("replan_meal", {**run, "previous_recommendation_id": foreign}))["error"]["code"] == "NOT_FOUND"
            print("E03-A PASS | EXTERNAL_AGENT 3 tools | 644/480 -> chicken; SOLD_OUT | foreign replan and write denied")

    async def execution_flow():
        reset = edit("/admin/simulation/reset", {"scenario_id": "CLOSED_LOOP"}, ops=True)
        ref = current()
        run_id = ref["run_id"]
        run = {"run_id": run_id}
        assert reset["snapshot"] == ref
        ticket = edit("/admin/simulation/demo-enrollments", run, ops=True)["data"]["demo_enrollment_token"]
        post("/session", {**run, "demo_enrollment_token": ticket})
        cookie = next(item.value for item in cookies if item.name == STUDENT_COOKIE)
        edit("/consent/ops-sharing", {**run, "share_for_ops": True, "expected_consent_revision": 0}, method="PUT")
        w1, w2 = (run_id + "." + window for window in ("W1", "W2"))

        def event(kind, payload):
            return edit("/admin/simulation/events", {
                **run, "expected_snapshot_id": current()["snapshot_id"], "event_type": kind, "payload": payload,
            }, ops=True)["snapshot"]

        def clock(time):
            return event("ADVANCE_CLOCK", {"to_clock_at": ref["clock_at"][:10] + "T" + time + "+08:00"})

        def state(snapshot_id=None):
            args = {**run, "target_window_id": w2}
            if snapshot_id:
                args["snapshot_id"] = snapshot_id
            return get("/admin/ops-state", args, ops=True)

        async with AsyncExitStack() as stack:
            student = await stack.enter_async_context(connect(database, access.bind(run_id, "STUDENT", cookie=cookie)))
            ops = await stack.enter_async_context(connect(database, access.bind(run_id, "OPS", actor=actor)))
            external = await stack.enter_async_context(connect(database, access.bind(run_id, "EXTERNAL_AGENT", actor=actor)))
            student_tools, ops_tools, external_tools = [await discovered(session) for session in (student, ops, external)]
            query = {**run, "request": REQUEST}
            rec = await call_checked(student, student_tools, "recommend_meal", query)
            agent_rec = await call_checked(external, external_tools, "recommend_meal", query)
            assert omit_rec(rec) == omit_rec(post("/recommend", query)) == omit_rec(agent_rec)
            args = {**run, "event_type": "INTENT_SELECTED", "bundle_id": "B_A_DUCK",
                    "recommendation_id": rec["data"]["recommendation_id"], "idempotency_key": uuid4().hex}
            grant = authorization("submit_feedback", args)
            picked = await call_checked(student, student_tools, "submit_feedback", {**args, "interaction_receipt": grant})
            assert picked["data"]["signals_revision"] == 13 and picked["data"]["share_applied"]
            assert post("/feedback", args) == picked
            clock("11:41:00")
            event("SET_SOLD_OUT", {"dish_id": "D_A_DUCK"})
            agent_new = await call_checked(external, external_tools, "replan_meal", {
                **run, "previous_recommendation_id": agent_rec["data"]["recommendation_id"],
            })
            student_new = post("/replan", {**run, "previous_recommendation_id": args["recommendation_id"]})
            assert student_new["data"]["selection_state"] == "NEEDS_RESELECT"
            assert student_new["data"]["selected_bundle_id"] == "B_A_DUCK"
            clock("11:42:00")
            demand_args = {**run, "reference_window_id": w1}
            demand = await call_checked(ops, ops_tools, "get_demand_insights", demand_args)
            assert get("/admin/demand-insights", demand_args, ops=True) == demand
            row = next(row for row in demand["data"]["rows"] if row["dish_id"] == "D_A_DUCK")
            assert tuple(row[field] for field in ("preferred_sessions", "intended_sessions", "dined_sessions")) == (4, 6, 3)
            proposal_args = {**demand_args, "target_window_id": w2}
            report = await call_checked(ops, ops_tools, "generate_ops_recommendations", proposal_args)
            assert post("/admin/ops-proposals", proposal_args, ops=True) == report
            proposal, = report["data"]["proposals"]
            action = {**run, "proposal_id": proposal["proposal_id"], "approved_extra_portions": 3,
                      "expected_snapshot_id": report["snapshot"]["snapshot_id"], "idempotency_key": uuid4().hex}
            blocked = await call_checked(ops, ops_tools, "confirm_ops_action", action)
            assert blocked["error"]["code"] == "CONFIRMATION_REQUIRED"
            assert state()["data"]["actions"] == []
            wrong_tool = await call_checked(ops, ops_tools, "confirm_ops_action", {**action, "interaction_receipt": grant})
            assert wrong_tool["error"]["code"] == "CONFIRMATION_REQUIRED"
            receipt = authorization("confirm_ops_action", action, ops=True)
            altered = await call_checked(ops, ops_tools, "confirm_ops_action",
                                         {**action, "approved_extra_portions": 2, "interaction_receipt": receipt})
            assert altered["error"]["code"] == "CONFIRMATION_REQUIRED"
            confirmation = await call_checked(ops, ops_tools, "confirm_ops_action", {**action, "interaction_receipt": receipt})
            assert confirmation["ok"]
            assert await call_checked(ops, ops_tools, "confirm_ops_action", action) == confirmation
            assert post("/admin/ops-confirmations", action, ops=True) == confirmation
            confirmed = state()
            assert confirmed["data"]["actions"][0]["status"] == "CONFIRMED"
            assert [row["action"] for row in confirmed["data"]["audit"]] == ["OPS_CONFIRMED"]
            assert any(row["dish_id"] == "D_A_DUCK" and row["confirmed_extra_portions"] == 3
                       for row in confirmed["data"]["plans"])
            historical_id = confirmed["snapshot"]["snapshot_id"]
            menu_args = {**run, "venue_id": "V_A"}
            now = await call_checked(external, external_tools, "get_dining_options", menu_args)
            assert next(row["available_bundle_portions"] for row in now["data"]["options"] if row["bundle_id"] == "B_A_DUCK") == 0
            final_ref = clock("12:00:00")
            final = state()
            assert final["data"]["actions"][0]["status"] == "APPLIED"
            assert [row["action"] for row in final["data"]["audit"]] == ["OPS_CONFIRMED", "OPS_APPLIED"]
            assert state(historical_id) == confirmed
            menu_args["snapshot_id"] = final_ref["snapshot_id"]
            supply = await call_checked(external, external_tools, "get_dining_options", menu_args)
            assert supply == get("/dining-options", menu_args)
            assert next(row["available_bundle_portions"] for row in supply["data"]["options"] if row["bundle_id"] == "B_A_DUCK") == 3
            updated = await call_checked(external, external_tools, "replan_meal", {
                **run, "previous_recommendation_id": agent_new["data"]["result"]["recommendation_id"],
            })
            scores = updated["data"]["result"]
            assert (scores["primary"]["bundle_id"], scores["primary"]["match_score"],
                    scores["alternatives"][0]["match_score"]) == ("B_A_DUCK", 555, 383)
            assert any(change["code"] in ("SUPPLY_RESTORED", "SERVICE_WINDOW_CHANGED")
                       for change in updated["data"]["change_reasons"])
            print("E03-B PASS | P/I/A=4/6/3 | receipt -> confirm 3 | W1 stock 0 | APPLIED at W2 stock 3 | 555/383")
            print("E03-C / A07 PASS | same HTTP/MCP decision, aggregation, proposal, confirmation, history and W2 supply")

    async def check():
        await flow()
        await execution_flow()
        print("SIMULATION | MCP 2025-11-25 | SDK stdio processes closed")

    asyncio.run(check())


if __name__ == "__main__":
    run_service(smoke)
