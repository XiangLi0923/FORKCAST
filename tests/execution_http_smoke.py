"""Run the approved E01 confirmation and control paths over real Windows loopback HTTP."""

from urllib.parse import urlencode
from urllib.request import urlopen
from uuid import uuid4

from forkcast.store import SimulationStore
from tests.demand_http_smoke import http_client, run_service, wait_ready


def smoke(port, token, database):
    request = http_client(port, token)
    wait_ready(request)
    with urlopen(f"http://127.0.0.1:{port}/ops", timeout=3) as page:
        assert "SIMULATION · 模拟运营数据" in page.read().decode()
    store = SimulationStore(database)
    api = "/api/v1"

    def flow(confirm):
        def post(path, fields, *, ops=False, expected=200):
            return request(api + path, method="POST", fields=fields, ops=ops, expected=expected)

        def edit(path, fields, *, ops=False, method="POST"):
            return request(api + path, method=method, ops=ops, fields={**fields, "idempotency_key": uuid4().hex})

        current = request(api + "/runs/current")["snapshot"]
        run = {"run_id": current["run_id"]}
        w1, w2 = current["run_id"] + ".W1", current["run_id"] + ".W2"

        def event(kind, payload):
            ref = request(api + "/runs/current")["snapshot"]
            return edit("/admin/simulation/events", {**run, "expected_snapshot_id": ref["snapshot_id"],
                                                     "event_type": kind, "payload": payload}, ops=True)["snapshot"]

        def clock(time):
            return event("ADVANCE_CLOCK", {"to_clock_at": "2026-11-21T" + time + "+08:00"})

        def replan(previous):
            return post("/replan", {**run, "previous_recommendation_id": previous["recommendation_id"]})["data"]

        def state(snapshot=None):
            params = {**run, "target_window_id": w2}
            if snapshot:
                params["snapshot_id"] = snapshot
            return request(api + "/admin/ops-state?" + urlencode(params), ops=True)

        def physical():
            with store.transaction() as db:
                frame = store._frame(db, current["run_id"])
                stock = next(row.stock_portions.value for row in frame.supply.dish_states if row.dish_id == "D_A_DUCK")
                plan = next(row for row in frame.plans if row.window_id == w2 and row.dish_id == "D_A_DUCK")
                prep = next(row for row in frame.prep if row.window_id == w2 and row.venue_id == "V_A")
                ingredient = next(row for row in frame.ingredients if row.window_id == w2 and row.dish_id == "D_A_DUCK")
                events = db.execute("SELECT count(*) FROM feedback_events WHERE run_id=?", (current["run_id"],)).fetchone()[0]
                return (stock, plan.confirmed_extra_portions, prep.reserved_prep_portions,
                        ingredient.reserved_ingredient_portions, ingredient.extra_ingredient_portions, events)

        ticket = edit("/admin/simulation/demo-enrollments", run, ops=True)["data"]["demo_enrollment_token"]
        post("/session", {**run, "demo_enrollment_token": ticket})
        rec = post("/recommend", {**run, "request": {
            "constraints": {"budget_max_cents": 2000, "time_budget_min": 45},
            "preferences": {"requested_dish_id": "D_A_DUCK"},
        }})["data"]
        assert (rec["primary"]["bundle_id"], rec["primary"]["match_score"], rec["alternatives"][0]["match_score"]) == (
            "B_A_DUCK", 644, 480,
        )
        edit("/consent/ops-sharing", {**run, "share_for_ops": True, "expected_consent_revision": 0}, method="PUT")
        picked = edit("/feedback", {**run, "event_type": "INTENT_SELECTED", "bundle_id": "B_A_DUCK",
                                    "recommendation_id": rec["recommendation_id"]})["data"]
        assert picked["signals_revision"] == 13 and picked["share_applied"]
        clock("11:41:00")
        event("SET_SOLD_OUT", {"dish_id": "D_A_DUCK"})
        next_rec = replan(rec)
        assert (next_rec["result"]["primary"]["bundle_id"], next_rec["selected_bundle_id"],
                next_rec["selection_state"]) == ("B_B_CHICKEN", "B_A_DUCK", "NEEDS_RESELECT")
        clock("11:42:00")
        demand = request(api + "/admin/demand-insights?" + urlencode({**run, "reference_window_id": w1}), ops=True)
        row = next(r for r in demand["data"]["rows"] if r["dish_id"] == "D_A_DUCK")
        assert (row["preferred_sessions"], row["intended_sessions"], row["dined_sessions"]) == (4, 6, 3)
        report = post("/admin/ops-proposals", {**run, "reference_window_id": w1, "target_window_id": w2}, ops=True)
        proposal, = report["data"]["proposals"]
        analysis = next(r for r in report["data"]["analyses"] if r["dish_id"] == "D_A_DUCK")
        assert (analysis["demand_proxy_portions"], analysis["planned_supply_portions"], analysis["shortage_portions"],
                analysis["feasible_extra_portions"], analysis["uncovered_portions"], analysis["ingredient_review_portions"]
                ) == (6, 0, 6, 3, 3, 1)
        assert physical() == (0, 0, 0, 0, 3, 1)
        if confirm:
            confirmation = edit("/admin/ops-confirmations", {
                **run, "proposal_id": proposal["proposal_id"], "approved_extra_portions": 3,
                "expected_snapshot_id": report["snapshot"]["snapshot_id"],
            }, ops=True)
            historical_id = confirmation["snapshot"]["snapshot_id"]
            frozen = state()
            assert frozen["snapshot"] == confirmation["snapshot"]
            assert frozen["data"]["actions"][0]["status"] == "CONFIRMED"
            assert [a["action"] for a in frozen["data"]["audit"]] == ["OPS_CONFIRMED"]
            assert physical() == (0, 3, 3, 3, 3, 1)
            assert replan(next_rec["result"])["result"]["primary"]["bundle_id"] == "B_B_CHICKEN"

        output = clock("12:00:00")
        result = replan(next_rec["result"])
        assert (result["selected_bundle_id"], result["selection_state"]) == ("B_A_DUCK", "NEEDS_RESELECT")
        assert result["result"]["snapshot"] == output
        directory = request(api + "/dining-options?" + urlencode({
            **run, "snapshot_id": output["snapshot_id"], "venue_id": "V_A",
        }))
        duck = next(row for row in directory["data"]["options"] if row["bundle_id"] == "B_A_DUCK")
        assert directory["snapshot"] == output
        assert (duck["available_bundle_portions"], duck["supply_state"]) == (
            (3, "AVAILABLE") if confirm else (0, "SOLD_OUT")
        )
        final = state()["data"]
        if confirm:
            assert (result["result"]["primary"]["bundle_id"], result["result"]["primary"]["match_score"],
                    result["result"]["alternatives"][0]["match_score"]) == ("B_A_DUCK", 555, 383)
            assert physical() == (3, 3, 0, 0, 0, 1)
            assert [a["action"] for a in final["audit"]] == ["OPS_CONFIRMED", "OPS_APPLIED"]
            assert (final["actions"][0]["status"], final["actions"][0]["application_snapshot_id"]) == (
                "APPLIED", output["snapshot_id"],
            )
            assert state(historical_id) == frozen
            print("E01 CONFIRMED PASS | W1 烤鸭 644→鸡腿，W2 烤鸭 555 / 鸡腿 383 | 库存 0→3 | 两条审计")
        else:
            assert (result["result"]["primary"]["bundle_id"], result["result"]["primary"]["match_score"]) == ("B_B_CHICKEN", 383)
            assert physical() == (0, 0, 0, 0, 3, 1)
            assert final["actions"] == final["audit"] == []
            print("E01 CONTROL PASS | W2 鸡腿 383 | 烤鸭库存 0 | 无确认/生效审计")
        return run

    old = flow(True)
    edit = request(api + "/admin/simulation/reset", method="POST", ops=True,
                   fields={"scenario_id": "CLOSED_LOOP", "idempotency_key": uuid4().hex})
    assert edit["snapshot"]["run_id"] != old["run_id"]
    denied = request(api + "/recommend", method="POST", fields={**old, "request": {}}, expected=409)
    assert denied["error"]["code"] == "RUN_REPLACED"
    flow(False)
    print("SIMULATION · 模拟运营数据 | A06 旧会话被拒。两条路径均保持 1 条主动意向，无自动改写。")


if __name__ == "__main__":
    run_service(smoke)
