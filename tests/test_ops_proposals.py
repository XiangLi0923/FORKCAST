import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from starlette.testclient import TestClient

from forkcast.http import OperatorCredential, create_app
from forkcast.models import IngredientPool, PrepResources, SupplyPlan
from forkcast.store import OpsActor, SimulationStore

API = "/api/v1"
OPS = API + "/admin/ops-proposals"
DUCK, TOFU, CHICKEN = "D_A_DUCK", "D_A_TOFU", "D_B_CHICKEN"
HEADERS = {"Authorization": "Bearer local-ops-test"}


class OpsProposalsTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "ops.sqlite3"
        self.store = SimulationStore(self.path)
        self.ops = self.app()
        self.admin = self.client()
        self.run = self.current()["run_id"]
        self.w1, self.w2 = self.run + ".W1", self.run + ".W2"

    def app(self, venues=("V_A", "V_B"), control=True, mode="SIMULATION", path=None):
        actor = OpsActor(principal_id="test-ops", allowed_venue_ids=venues, can_control_run=control)
        return create_app(path or self.path, mode, credential=OperatorCredential("local-ops-test", actor))

    def client(self, app=None):
        client = TestClient(app or self.ops, base_url="http://127.0.0.1")
        self.addCleanup(client.close)
        return client

    def ok(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def expect_error(self, response, status, code):
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(response.json()["error"]["code"], code)

    def current(self):
        return self.ok(self.admin.get(API + "/runs/current"))["snapshot"]

    def params(self, **fields):
        return {"run_id": self.run, "reference_window_id": self.w1, "target_window_id": self.w2, **fields}

    def report(self, **fields):
        return self.ok(self.admin.post(OPS, json=self.params(**fields), headers=HEADERS))

    def write(self, client, path, fields, *, headers=None, method="POST"):
        return self.ok(client.request(method, API + path, headers=headers,
                                      json={**fields, "idempotency_key": uuid4().hex}))

    def demo(self):
        run = {"run_id": self.run}
        doc = self.write(self.admin, "/admin/simulation/demo-enrollments", run, headers=HEADERS)
        client = self.client()
        self.ok(client.post(API + "/session", json={**run,
            "demo_enrollment_token": doc["data"]["demo_enrollment_token"],
        }))
        self.consent(client, True, 0)
        return client

    def consent(self, client, enabled, revision):
        return self.write(client, "/consent/ops-sharing", {
            "run_id": self.run, "share_for_ops": enabled, "expected_consent_revision": revision,
        }, method="PUT")["data"]

    def select(self, client, bundle):
        rec = self.ok(client.post(API + "/recommend", json={"run_id": self.run, "request": {}}))["data"]
        self.write(client, "/feedback", {
            "run_id": self.run, "event_type": "INTENT_SELECTED", "bundle_id": bundle,
            "recommendation_id": rec["recommendation_id"],
        })

    def snapshot_sql(self):
        with self.store.transaction() as db:
            tables = ("runs", "snapshots", "events", "audit", "feedback_events")
            return tuple(tuple(map(tuple, db.execute(f"SELECT * FROM {name}"))) for name in tables)

    def status(self):
        with self.store.transaction() as db:
            return dict(db.execute("SELECT proposal_id,status FROM ops_proposals"))

    def analysis(self, report, dish):
        return next(row for row in report["data"]["analyses"] if row["dish_id"] == dish)

    def test_s07_fixed_vector_proposal_reuse_and_unchanged_supply_and_c_recommendations(self):
        student = self.demo()
        self.select(student, "B_A_DUCK")
        request = {"run_id": self.run, "request": {}}
        before = self.ok(student.post(API + "/recommend", json=request))["data"]
        state = self.snapshot_sql()
        result = self.report()
        self.assertEqual(result["data_mode"], "SIMULATION")
        self.assertEqual(result["snapshot"], self.current())
        self.assertEqual(result["data"]["signals_revision"], 13)
        row = self.analysis(result, DUCK)
        self.assertEqual(row, {
            "venue_id": "V_A", "dish_id": DUCK, "reference_window_id": self.w1, "target_window_id": self.w2,
            "demand_proxy_portions": 6, "planned_supply_portions": 0, "shortage_portions": 6,
            "feasible_extra_portions": 3, "uncovered_portions": 3, "ingredient_review_portions": 1,
            "evidence_codes": ["DEMAND_FROM_INTENT", "INGREDIENT_LIMIT"],
        })
        proposals = result["data"]["proposals"]
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0], {
            "proposal_id": proposals[0]["proposal_id"], "run_id": self.run, "venue_id": "V_A", "dish_id": DUCK,
            "reference_window_id": self.w1, "target_window_id": self.w2,
            "based_on_snapshot_id": result["snapshot"]["snapshot_id"], "signals_revision": 13,
            "expected_effective_at": "2026-11-21T12:00:00+08:00", "demand_proxy_portions": 6,
            "planned_supply_portions": 0, "shortage_portions": 6, "proposed_extra_portions": 3,
            "uncovered_portions": 3, "evidence_codes": row["evidence_codes"], "status": "PROPOSED",
        })
        self.assertEqual(self.report(), result)
        self.assertEqual(self.report(venue_id="V_A")["data"]["proposals"], proposals)
        self.assertEqual(self.status(), {proposals[0]["proposal_id"]: "PROPOSED"})
        self.assertEqual(self.snapshot_sql(), state)
        after = self.ok(student.post(API + "/recommend", json=request))["data"]
        before.pop("recommendation_id")
        after.pop("recommendation_id")
        self.assertEqual(after, before)

    def publish_test_resources(self, *dishes, venue_b=False, omit=()):
        # Publish a fresh, immutable test frame to exercise an alternate W2 resource vector.
        with self.store.transaction(write=True) as db:
            frame = self.store._frame(db, self.run)
            supply = frame.supply
            revision = supply.ref.revision + 1
            ref = supply.ref.model_copy(update={"revision": revision, "snapshot_id": f"{self.run}.s{revision}"})
            plans = [row for row in frame.plans if row.window_id != self.w2 or row.dish_id not in {*dishes, *omit}]
            ingredients = [row for row in frame.ingredients if row.window_id != self.w2 or row.dish_id not in dishes]
            for dish in dishes:
                plans.append(SupplyPlan(run_id=self.run, window_id=self.w2, dish_id=dish, base_planned_portions=0,
                                        confirmed_extra_portions=0, max_served_portions=8))
                ingredients.append(IngredientPool(run_id=self.run, window_id=self.w2, dish_id=dish,
                                                  extra_ingredient_portions=3, reserved_ingredient_portions=0))
            prep = frame.prep
            if venue_b:
                prep = tuple(
                    PrepResources(run_id=self.run, window_id=self.w2, venue_id="V_B",
                                  extra_prep_capacity_portions=4, reserved_prep_portions=0)
                    if row.window_id == self.w2 and row.venue_id == "V_B" else row for row in prep
                )
            self.store._insert_frame(db, frame.model_copy(update={
                "supply": supply.model_copy(update={"ref": ref}), "plans": tuple(plans),
                "ingredients": tuple(ingredients), "prep": prep,
            }))

    def test_s07_suppressed_threshold_revocation_stales_without_waiting_for_regeneration(self):
        self.publish_test_resources(CHICKEN, venue_b=True)
        subjects = [self.demo() for _ in range(3)]
        for student in subjects:
            self.select(student, "B_B_CHICKEN")
        result = self.report(venue_id="V_B")
        proposal, = result["data"]["proposals"]
        self.assertEqual(proposal["proposed_extra_portions"], 3)
        self.assertEqual(proposal["evidence_codes"], ["DEMAND_FROM_INTENT"])
        revision = self.consent(subjects[0], False, 1)["signals_revision"]
        self.assertEqual(self.status(), {proposal["proposal_id"]: "STALE"})
        after = self.report(venue_id="V_B")
        self.assertEqual(after["snapshot"], result["snapshot"])
        self.assertEqual(after["data"]["signals_revision"], revision)
        self.assertEqual(after["data"]["proposals"], [])
        row = self.analysis(after, CHICKEN)
        self.assertEqual(row["evidence_codes"], ["INSUFFICIENT_SIGNAL"])
        for field in ("demand_proxy_portions", "shortage_portions", "feasible_extra_portions",
                      "uncovered_portions", "ingredient_review_portions"):
            self.assertIsNone(row[field])

    def test_s07_shared_prep_hypotheses_sorted_without_reservations(self):
        self.publish_test_resources(TOFU, CHICKEN, venue_b=True)
        self.select(self.demo(), "B_A_DUCK")
        # Twelve explicit preferences create proxy six for tofu and for chicken. Separate subjects let both stand.
        for dish in (TOFU, CHICKEN):
            for _ in range(12):
                self.write(self.demo(), "/feedback", {"run_id": self.run, "event_type": "PREFERENCE", "dish_id": dish})
        self.assertEqual([p["dish_id"] for p in self.report()["data"]["proposals"]], [DUCK, TOFU, CHICKEN])
        for _ in range(2):
            self.write(self.demo(), "/feedback", {"run_id": self.run, "event_type": "PREFERENCE", "dish_id": CHICKEN})
        state = self.snapshot_sql()
        result = self.report()["data"]
        self.assertEqual([(p["dish_id"], p["shortage_portions"], p["proposed_extra_portions"])
                          for p in result["proposals"]], [(CHICKEN, 7, 3), (DUCK, 6, 3), (TOFU, 6, 3)])
        self.assertEqual(self.snapshot_sql(), state)
        self.assertEqual(self.report(venue_id="V_A")["data"]["proposals"], result["proposals"][1:])
        self.assertEqual(self.report()["data"], result)

    def test_no_plan_generates_no_proposal_even_with_visible_demand(self):
        self.publish_test_resources(omit=(DUCK,))
        data = self.report()["data"]
        self.assertEqual(data["proposals"], [])
        self.assertNotIn(DUCK, {row["dish_id"] for row in data["analyses"]})
        self.assertEqual(self.status(), {})

    def test_snapshot_change_invalidates_and_reset_cascades(self):
        first, = self.report()["data"]["proposals"]
        self.write(self.admin, "/admin/simulation/events", {
            "run_id": self.run, "expected_snapshot_id": self.current()["snapshot_id"],
            "event_type": "SET_SOLD_OUT", "payload": {"dish_id": DUCK},
        }, headers=HEADERS)
        self.assertEqual(self.status(), {first["proposal_id"]: "STALE"})
        second, = self.report()["data"]["proposals"]
        self.assertNotEqual(first["proposal_id"], second["proposal_id"])
        self.assertNotEqual(first["based_on_snapshot_id"], second["based_on_snapshot_id"])
        self.assertEqual(first["signals_revision"], second["signals_revision"])
        self.write(self.admin, "/admin/simulation/reset", {"scenario_id": "CLOSED_LOOP"}, headers=HEADERS)
        self.assertEqual(self.status(), {})
        self.expect_error(self.admin.post(OPS, json=self.params(), headers=HEADERS), 404, "NOT_FOUND")

    def test_concurrent_generation_reuses_one_proposal(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.report(), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(self.status()), 1)

    def test_failed_second_insert_rolls_back_entire_report(self):
        self.publish_test_resources(TOFU)
        for _ in range(4):
            self.write(self.demo(), "/feedback", {"run_id": self.run, "event_type": "PREFERENCE", "dish_id": TOFU})
        state = self.snapshot_sql()
        with self.store.transaction(write=True) as db:
            db.execute("""CREATE TRIGGER fail_tofu BEFORE INSERT ON ops_proposals WHEN NEW.dish_id='D_A_TOFU'
                          BEGIN SELECT RAISE(ABORT, 'test write failure'); END""")
        self.expect_error(self.admin.post(OPS, json=self.params(), headers=HEADERS), 500, "INTERNAL_ERROR")
        self.assertEqual(self.status(), {})
        self.assertEqual(self.snapshot_sql(), state)
        with self.store.transaction(write=True) as db:
            db.execute("DROP TRIGGER fail_tofu")
        self.assertEqual([row["dish_id"] for row in self.report()["data"]["proposals"]], [DUCK, TOFU])

    def test_ops_only_scope_and_closed_parameters(self):
        scoped = self.client(self.app(("V_B",), False))
        data = self.ok(scoped.post(OPS, json=self.params(), headers=HEADERS))["data"]
        self.assertEqual({row["venue_id"] for row in data["analyses"]}, {"V_B"})
        self.assertEqual(data["proposals"], [])
        self.expect_error(scoped.post(OPS, json=self.params(venue_id="V_A"), headers=HEADERS), 403, "FORBIDDEN")
        student = self.demo()
        for headers in ({}, {"Authorization": "Bearer external-agent", "X-Role": "OPS"}):
            self.expect_error(student.post(OPS, json=self.params(), headers=headers), 401, "UNAUTHENTICATED")
        for extra in ({"venue_id": None}, {"snapshot_id": "old"}, {"role": "OPS"}, {"data_mode": "LIVE"}):
            self.expect_error(self.admin.post(OPS, json=self.params(**extra), headers=HEADERS), 400, "INVALID_INPUT")
        self.expect_error(self.admin.post(OPS + "?venue_id=V_A", json=self.params(), headers=HEADERS),
                          400, "INVALID_INPUT")
        self.expect_error(self.admin.post(OPS, json=self.params(target_window_id=self.w1), headers=HEADERS),
                          400, "INVALID_INPUT")
        self.expect_error(self.admin.post(OPS, json=self.params(reference_window_id=self.w2), headers=HEADERS),
                          400, "INVALID_INPUT")
        self.expect_error(self.admin.post(OPS, json=self.params(target_window_id="other.W2"), headers=HEADERS),
                          404, "NOT_FOUND")
        self.assertEqual(self.status(), {})

    def test_window_activation_stales_and_rejects_old_reference_without_changes(self):
        first, = self.report()["data"]["proposals"]
        self.write(self.admin, "/admin/simulation/events", {
            "run_id": self.run, "expected_snapshot_id": self.current()["snapshot_id"], "event_type": "ADVANCE_CLOCK",
            "payload": {"to_clock_at": "2026-11-21T12:00:00+08:00"},
        }, headers=HEADERS)
        self.assertEqual(self.status(), {first["proposal_id"]: "STALE"})
        state = self.snapshot_sql()
        self.expect_error(self.admin.post(OPS, json=self.params(), headers=HEADERS), 400, "INVALID_INPUT")
        self.assertEqual(self.snapshot_sql(), state)

    def test_live_endpoint_refuses_without_creating_database(self):
        live_path = self.path.with_name("live.sqlite3")
        live = self.client(self.app(mode="LIVE", path=live_path))
        self.expect_error(live.post(OPS, json=self.params(), headers=HEADERS), 403, "MODE_DISABLED")
        self.assertFalse(live_path.exists())
