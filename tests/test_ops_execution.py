import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from forkcast.models import SimulationEvent
from tests import test_ops_proposals as base

API = "/api/v1"
CONFIRM = API + "/admin/ops-confirmations"
STATE = API + "/admin/ops-state"
DUCK = "D_A_DUCK"
TOFU = "D_A_TOFU"


class OpsExecutionTests(unittest.TestCase):
    def setUp(self):
        self.env = base.OpsProposalsTests()
        self.addCleanup(self.env.doCleanups)
        self.env.setUp()
        self.db = self.env.store

    def proposal(self):
        record, = self.env.report()["data"]["proposals"]
        return record

    def body(self, proposal, amount=3, **fields):
        return {
            "run_id": self.env.run, "proposal_id": proposal["proposal_id"], "approved_extra_portions": amount,
            "expected_snapshot_id": proposal["based_on_snapshot_id"], "idempotency_key": uuid4().hex, **fields,
        }

    def confirm(self, proposal, amount=3):
        return self.env.ok(self.env.admin.post(CONFIRM, json=self.body(proposal, amount), headers=base.HEADERS))

    def ops_state(self, **fields):
        return self.env.ok(self.env.admin.get(STATE, headers=base.HEADERS, params={
            "run_id": self.env.run, "target_window_id": self.env.w2, **fields,
        }))["data"]

    def frame(self, ref=None):
        with self.db.transaction() as db:
            return self.db._frame(db, self.env.run, ref)

    def database_dump(self):
        with self.db.transaction() as db:
            return tuple(db.iterdump())

    def advance_body(self, at="2026-11-21T12:00:00+08:00"):
        return {"run_id": self.env.run, "expected_snapshot_id": self.env.current()["snapshot_id"],
                "event_type": "ADVANCE_CLOCK", "payload": {"to_clock_at": at}, "idempotency_key": uuid4().hex}

    def advance(self, body=None):
        return self.env.ok(self.env.admin.post(API + "/admin/simulation/events", headers=base.HEADERS,
                                               json=body or self.advance_body()))

    def resources(self, frame, dish=DUCK):
        plan = next(row for row in frame.plans if row.window_id == self.env.w2 and row.dish_id == dish)
        prep = next(row for row in frame.prep if row.window_id == self.env.w2 and row.venue_id == "V_A")
        ingredients = next(row for row in frame.ingredients if row.window_id == self.env.w2 and row.dish_id == dish)
        return plan.confirmed_extra_portions, prep.reserved_prep_portions, ingredients.reserved_ingredient_portions, ingredients.extra_ingredient_portions

    @staticmethod
    def stock(frame, dish=DUCK):
        return next(row.stock_portions.value for row in frame.supply.dish_states if row.dish_id == dish)

    def test_s08_partial_confirm_idempotency_and_error_precedence(self):
        proposal = self.proposal()
        before = self.frame()
        body = self.body(proposal, 2)
        first = self.env.ok(self.env.admin.post(CONFIRM, json=body, headers=base.HEADERS))
        self.assertEqual(set(first["data"]), {
            "proposal_id", "approved_extra_portions", "actor_id", "confirmed_at", "confirmation_snapshot",
        })
        self.assertEqual(first["data"]["confirmation_snapshot"], first["snapshot"])
        self.assertEqual(first["data"]["approved_extra_portions"], 2)
        self.assertEqual(self.resources(self.frame()), (2, 2, 2, 3))
        self.assertEqual(self.frame().supply.dish_states, before.supply.dish_states)
        state = self.database_dump()
        self.assertEqual(self.env.ok(self.env.admin.post(CONFIRM, json=body, headers=base.HEADERS)), first)
        for payload, code in (({**body, "approved_extra_portions": 1}, "IDEMPOTENCY_CONFLICT"),
                              ({**body, "idempotency_key": "second-confirm"}, "OPS_ALREADY_CONFIRMED")):
            self.env.expect_error(self.env.admin.post(CONFIRM, json=payload, headers=base.HEADERS), 409, code)
        self.assertEqual(self.database_dump(), state)
        self.advance()
        self.assertEqual(self.env.ok(self.env.admin.post(CONFIRM, json=body, headers=base.HEADERS)), first)
        self.assertEqual(self.resources(self.frame()), (2, 0, 0, 1))

    def test_s08_amount_validation_and_resource_recheck(self):
        proposal = self.proposal()
        initial = self.database_dump()
        for quantity in (0, -1, 4, 2.0, True, "2"):
            self.env.expect_error(self.env.admin.post(CONFIRM, json=self.body(proposal, quantity), headers=base.HEADERS),
                                  400, "INVALID_INPUT")
        self.assertEqual(self.database_dump(), initial)
        # Isolate the execution guard with an inconsistent stored recommendation from a synthetic producer.
        with self.db.transaction(write=True) as db:
            record = db.execute("SELECT proposal_json FROM ops_proposals").fetchone()[0]
            unsafe = {**json.loads(record), "proposed_extra_portions": 4}
            db.execute("UPDATE ops_proposals SET proposal_json=?", (json.dumps(unsafe),))
        state = self.database_dump()
        self.env.expect_error(self.env.admin.post(CONFIRM, json=self.body(proposal, 4), headers=base.HEADERS),
                              409, "STALE_PROPOSAL")
        self.assertEqual(self.database_dump(), state)

    def test_s08_signal_and_supply_changes_reject_without_reserving(self):
        student = self.env.demo()
        first = self.proposal()
        self.env.select(student, "B_A_DUCK")
        self.env.expect_error(self.env.admin.post(CONFIRM, json=self.body(first), headers=base.HEADERS),
                              409, "STALE_PROPOSAL")
        second = self.proposal()
        self.env.consent(student, False, 1)
        self.env.expect_error(self.env.admin.post(CONFIRM, json=self.body(second), headers=base.HEADERS),
                              409, "STALE_PROPOSAL")
        third = self.proposal()
        self.advance(self.advance_body("2026-11-21T11:42:00+08:00"))
        self.env.expect_error(self.env.admin.post(CONFIRM, json=self.body(third), headers=base.HEADERS),
                              409, "SNAPSHOT_CONFLICT")
        self.advance()
        request = self.body(third, expected_snapshot_id=self.env.current()["snapshot_id"])
        self.env.expect_error(self.env.admin.post(CONFIRM, json=request, headers=base.HEADERS), 409, "STALE_PROPOSAL")
        self.assertEqual(self.resources(self.frame()), (0, 0, 0, 3))
        self.assertEqual(self.stock(self.frame()), 0)
        self.assertEqual(self.ops_state()["audit"], [])

    def test_s08_concurrent_same_proposal_only_one_confirmation(self):
        proposal = self.proposal()
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: self.env.admin.post(CONFIRM, json=self.body(proposal),
                                                                     headers=base.HEADERS), range(2)))
        self.assertEqual(sorted(r.status_code for r in responses), [200, 409])
        self.assertEqual(next(r.json()["error"]["code"] for r in responses if r.status_code == 409),
                         "OPS_ALREADY_CONFIRMED")
        self.assertEqual(self.resources(self.frame()), (3, 3, 3, 3))
        self.assertEqual(len(self.ops_state()["audit"]), 1)

    def test_s08_concurrent_shared_prep_capacity_then_regeneration(self):
        self.env.publish_test_resources(TOFU)
        self.env.select(self.env.demo(), "B_A_DUCK")
        for _ in range(12):
            self.env.write(self.env.demo(), "/feedback", {
                "run_id": self.env.run, "event_type": "PREFERENCE", "dish_id": TOFU,
            })
        proposals = self.env.report()["data"]["proposals"]
        self.assertEqual([(p["dish_id"], p["proposed_extra_portions"]) for p in proposals], [(DUCK, 3), (TOFU, 3)])
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(lambda p: self.env.admin.post(CONFIRM, json=self.body(p),
                                                                  headers=base.HEADERS), proposals))
        self.assertEqual(sorted(reply.status_code for reply in replies), [200, 409])
        self.assertEqual(next(r.json()["error"]["code"] for r in replies if r.status_code == 409), "SNAPSHOT_CONFLICT")
        frame = self.frame()
        self.assertEqual(self.resources(frame)[1], 3)
        self.assertEqual(sum(self.resources(frame, dish)[2] for dish in (DUCK, TOFU)), 3)
        regenerated = self.env.report()["data"]["proposals"]
        self.assertTrue(regenerated)
        self.assertEqual(max(p["proposed_extra_portions"] for p in regenerated), 1)
        self.assertTrue(all(row.reserved_prep_portions <= row.extra_prep_capacity_portions for row in frame.prep))
        self.assertTrue(all(row.reserved_ingredient_portions <= row.extra_ingredient_portions for row in frame.ingredients))

    def test_s09_activate_then_same_tick_sale_and_read_history(self):
        proposal = self.proposal()
        # Fixture-only sale at 12:00 proves stock is loaded first and exactly once.
        with self.db.transaction(write=True) as db:
            event = SimulationEvent.model_validate_json(json.dumps({
                "event_id": "sale-after-boundary", "run_id": self.env.run, "scheduled_at": "2026-11-21T12:00:00+08:00",
                "sequence": 3, "event_type": "SIM_SALE", "target_id": DUCK, "payload": {"dish_id": DUCK, "portions": 1},
            }))
            self.db._insert_event(db, event)
        initial = self.frame()
        confirmed = self.confirm(proposal)["snapshot"]
        before = self.frame()
        self.assertEqual(self.stock(before), 2)
        self.assertEqual(self.resources(before), (3, 3, 3, 3))
        body = self.advance_body()
        output = self.advance(body)
        self.assertEqual(self.stock(self.frame()), 2)
        self.assertEqual(self.resources(self.frame()), (3, 0, 0, 0))
        self.assertEqual(self.frame(confirmed["snapshot_id"]), before)
        self.assertEqual(self.frame(initial.supply.ref.snapshot_id), initial)
        state = self.ops_state()
        self.assertEqual(state["actions"][0]["status"], "APPLIED")
        self.assertEqual(state["actions"][0]["application_snapshot_id"], output["snapshot"]["snapshot_id"])
        self.assertEqual(state["actions"][0]["applied_at"], "2026-11-21T12:00:00+08:00")
        self.assertEqual([a["action"] for a in state["audit"]], ["OPS_CONFIRMED", "OPS_APPLIED"])
        self.assertNotIn("actor_id", state["audit"][0])
        frozen = self.ops_state(snapshot_id=confirmed["snapshot_id"])
        self.assertEqual(frozen["actions"][0]["status"], "CONFIRMED")
        self.assertIsNone(frozen["actions"][0]["application_snapshot_id"])
        self.assertIsNone(frozen["actions"][0]["applied_at"])
        self.assertEqual([a["action"] for a in frozen["audit"]], ["OPS_CONFIRMED"])
        self.assertEqual(self.ops_state(snapshot_id=initial.supply.ref.snapshot_id)["actions"], [])
        self.assertEqual(self.advance(body), output)
        self.env.expect_error(self.env.admin.post(API + "/admin/simulation/events", headers=base.HEADERS,
                                                 json=self.advance_body()), 400, "INVALID_INPUT")
        self.advance(self.advance_body("2026-11-21T12:01:00+08:00"))
        self.assertEqual(self.stock(self.frame()), 2)
        self.assertEqual(self.ops_state(), state)

    def test_confirm_and_activation_audit_failures_roll_back_everything(self):
        proposal = self.proposal()
        for action in ("OPS_CONFIRMED", "OPS_APPLIED"):
            with self.db.transaction(write=True) as db:
                db.execute(f"""CREATE TRIGGER fail_execution BEFORE INSERT ON audit
                    WHEN json_extract(NEW.audit_json,'$.action')='{action}'
                    BEGIN SELECT RAISE(ABORT,'injected audit failure'); END""")
            before = self.database_dump()
            path, body = ((CONFIRM, self.body(proposal)) if action == "OPS_CONFIRMED"
                          else (API + "/admin/simulation/events", self.advance_body()))
            self.env.expect_error(self.env.admin.post(path, json=body, headers=base.HEADERS), 500, "INTERNAL_ERROR")
            self.assertEqual(self.database_dump(), before)
            with self.db.transaction(write=True) as db:
                db.execute("DROP TRIGGER fail_execution")
            self.env.ok(self.env.admin.post(path, json=body, headers=base.HEADERS))
        self.assertEqual(self.stock(self.frame()), 3)

    def test_a05_permissions_shape_and_a06_reset_isolation(self):
        proposal = self.proposal()
        payload = self.body(proposal)
        student = self.env.demo()
        params = {"run_id": self.env.run, "target_window_id": self.env.w2}
        for headers in ({}, {"Authorization": "Bearer external-agent", "X-Role": "OPS"}):
            self.env.expect_error(student.post(CONFIRM, json=payload, headers=headers), 401, "UNAUTHENTICATED")
            self.env.expect_error(student.get(STATE, params=params, headers=headers), 401, "UNAUTHENTICATED")
        scoped = self.env.client(self.env.app(("V_B",), False))
        self.env.expect_error(scoped.post(CONFIRM, json=payload, headers=base.HEADERS), 404, "NOT_FOUND")
        self.env.expect_error(scoped.get(STATE, params={**params, "venue_id": "V_A"}, headers=base.HEADERS),
                              403, "FORBIDDEN")
        self.env.expect_error(self.env.admin.post(CONFIRM, json={**payload, "interaction_receipt": "x"},
                                                 headers=base.HEADERS), 400, "INVALID_INPUT")
        result = self.env.ok(self.env.admin.post(CONFIRM, json=payload, headers=base.HEADERS))
        visible = self.env.ok(scoped.get(STATE, params=params, headers=base.HEADERS))["data"]
        self.assertEqual((visible["actions"], visible["audit"]), ([], []))
        self.assertEqual({row["venue_id"] for row in visible["plans"]}, {"V_B"})
        self.env.write(self.env.admin, "/admin/simulation/reset", {"scenario_id": "CLOSED_LOOP"}, headers=base.HEADERS)
        self.env.expect_error(student.post(API + "/recommend", json={"run_id": self.env.run, "request": {}}),
                              409, "RUN_REPLACED")
        self.env.expect_error(self.env.admin.post(CONFIRM, json=payload, headers=base.HEADERS), 404, "NOT_FOUND")
        self.env.expect_error(self.env.admin.get(STATE, headers=base.HEADERS, params={
            **params, "snapshot_id": result["snapshot"]["snapshot_id"],
        }), 404, "NOT_FOUND")

    def test_ops_workbench_serves_real_assets_on_same_origin(self):
        home = self.env.admin.get("/ops")
        self.assertEqual(home.status_code, 200)
        self.assertIn('SIMULATION · 模拟运营数据', home.text)
        self.assertIn("default-src 'self'", home.headers["content-security-policy"])
        script = self.env.admin.get("/student-assets/ops.mjs")
        self.assertEqual(script.status_code, 200)
        self.assertIn("admin/ops-confirmations", script.text)
        self.assertIn("admin/ops-state", script.text)
