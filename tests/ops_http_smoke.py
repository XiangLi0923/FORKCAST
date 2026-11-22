"""Exercise S07 against a real loopback Uvicorn process and temporary SQLite database."""

from urllib.parse import urlencode
from uuid import uuid4

from forkcast.store import SimulationStore
from tests.demand_http_smoke import http_client, run_service, wait_ready


def smoke(port, token, database):
    request = http_client(port, token)
    wait_ready(request)
    api = "/api/v1"
    ref = request(api + "/runs/current")["snapshot"]
    run = {"run_id": ref["run_id"]}
    w1, w2 = ref["run_id"] + ".W1", ref["run_id"] + ".W2"
    fields = {**run, "reference_window_id": w1, "target_window_id": w2, "venue_id": "V_A"}
    route = api + "/admin/ops-proposals"
    insight_path = api + "/admin/demand-insights?" + urlencode({
        **run, "reference_window_id": w1, "venue_id": "V_A",
    })

    ticket = request(api + "/admin/simulation/demo-enrollments", method="POST", ops=True,
                     fields={**run, "idempotency_key": uuid4().hex})["data"]["demo_enrollment_token"]
    request(api + "/session", method="POST", fields={**run, "demo_enrollment_token": ticket})
    request(api + "/consent/ops-sharing", method="PUT", fields={
        **run, "share_for_ops": True, "expected_consent_revision": 0, "idempotency_key": uuid4().hex,
    })
    rec = request(api + "/recommend", method="POST", fields={**run, "request": {}})["data"]
    request(api + "/feedback", method="POST", fields={
        **run, "event_type": "INTENT_SELECTED", "bundle_id": "B_A_DUCK",
        "recommendation_id": rec["recommendation_id"], "idempotency_key": uuid4().hex,
    })
    signals = request(insight_path, ops=True)
    duck = next(row for row in signals["data"]["rows"] if row["dish_id"] == "D_A_DUCK")
    assert (duck["preferred_sessions"], duck["intended_sessions"], duck["dined_sessions"]) == (4, 6, 3)
    store = SimulationStore(database)

    def physical_state():
        with store.transaction() as db:
            return (
                store._frame(db, ref["run_id"]),
                db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0],
                db.execute("SELECT COUNT(*) FROM audit").fetchone()[0],
            )

    before = physical_state()
    result = request(route, method="POST", ops=True, fields=fields)
    assert result["data_mode"] == "SIMULATION" and result["snapshot"] == signals["snapshot"] == ref
    assert result["data"]["signals_revision"] == signals["data"]["signals_revision"] == 13
    row = next(row for row in result["data"]["analyses"] if row["dish_id"] == "D_A_DUCK")
    measured = ("demand_proxy_portions", "planned_supply_portions", "shortage_portions",
                "feasible_extra_portions", "uncovered_portions", "ingredient_review_portions")
    assert tuple(row[key] for key in measured) == (6, 0, 6, 3, 3, 1)
    proposal, = result["data"]["proposals"]
    assert proposal["status"] == "PROPOSED"
    assert proposal["expected_effective_at"] == "2026-11-21T12:00:00+08:00"
    assert proposal["evidence_codes"] == ["DEMAND_FROM_INTENT", "INGREDIENT_LIMIT"]
    assert request(route, method="POST", ops=True, fields=fields) == result
    assert request(route, method="POST", fields=fields, expected=401)["error"]["code"] == "UNAUTHENTICATED"
    assert physical_state() == before

    request(api + "/consent/ops-sharing", method="PUT", fields={
        **run, "share_for_ops": False, "expected_consent_revision": 1, "idempotency_key": uuid4().hex,
    })
    latest = request(route, method="POST", ops=True, fields=fields)
    assert latest["data"]["signals_revision"] == 14
    assert latest["data"]["proposals"][0]["proposal_id"] != proposal["proposal_id"]
    with store.transaction() as db:
        old = db.execute("SELECT status FROM ops_proposals WHERE proposal_id=?", (proposal["proposal_id"],)).fetchone()
        assert old["status"] == "STALE"
    assert physical_state() == before
    print("SIMULATION · 模拟运营数据 | S07 W1→W2 | P/I/A=4/6/3 | signals_revision=13")
    print("PASS: 代理=6，计划=0，缺口=6，追加=3，未覆盖=3，原料复核=1。")
    print("PASS: PROPOSED，预计生效 12:00+08:00，同版本复用，学生 401。")
    print("PASS: 撤回后信号版本 14，旧提案 STALE，供应、库存、原料、预留和审计均不变。")


if __name__ == "__main__":
    run_service(smoke)
