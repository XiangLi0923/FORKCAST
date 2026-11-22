import json
import unittest
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from starlette.testclient import TestClient

from forkcast.decision_engine import evaluate
from forkcast.decision_models import DecisionInput, DecisionRequest
from forkcast.fixture import Fixture
from forkcast.http import STUDENT_COOKIE, OperatorCredential, create_app
from forkcast.models import BundleItem, Catalog, MealBundle
from forkcast.store import MockProvider, OpsActor, SimulationStore
from forkcast.student_service import StudentService

API = "/api/v1"
DUCK, CHICKEN = "B_A_DUCK", "B_B_CHICKEN"
OPS = {"Authorization": "Bearer local-m22-test"}


class StudentHttpTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "forge.sqlite"
        self.store = SimulationStore(self.path)
        self.student = StudentService(self.store)
        self.ops = OpsActor(principal_id="ops", allowed_venue_ids=("V_A", "V_B"), can_control_run=True)
        self.app = create_app(self.path, credential=OperatorCredential("local-m22-test", self.ops))
        self.client = self.browser()
        self.ref = self.current()
        self.run = self.ref["run_id"]
        self.key_counter = 0

    def browser(self):
        c = TestClient(self.app, base_url="http://127.0.0.1")
        self.addCleanup(c.close)
        return c

    def key(self):
        self.key_counter += 1
        return f"browser-key-{self.key_counter}"

    def current(self):
        return self.client.get(API + "/runs/current").json()["snapshot"]

    def enroll(self):
        r = self.client.post(API + "/admin/simulation/demo-enrollments", headers=OPS,
                             json={"run_id": self.run, "idempotency_key": self.key()})
        self.assertEqual(r.status_code, 200)
        return r.json()["data"]["demo_enrollment_token"]

    def open(self, ticket=None, client=None):
        client = client or self.client
        data = {"run_id": self.run}
        if ticket:
            data["demo_enrollment_token"] = ticket
        r = client.post(API + "/session", json=data)
        self.assertEqual(r.status_code, 200, r.text)
        return r

    def rec(self, request=None, client=None):
        client = client or self.client
        response = client.post(API + "/recommend", json={"run_id": self.run, "request": request or {}})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["data"]

    def consent(self, on, rev, key=None):
        return self.client.put(API + "/consent/ops-sharing", json={
            "run_id": self.run, "share_for_ops": on, "expected_consent_revision": rev,
            "idempotency_key": key or self.key(),
        })

    def feedback(self, event, rec=None, bundle=None, *, key=None, dish=None, client=None):
        client = client or self.client
        data = {"run_id": self.run, "event_type": event, "idempotency_key": key or self.key()}
        if rec:
            data["recommendation_id"] = rec["recommendation_id"]
        if bundle:
            data["bundle_id"] = bundle
        if dish:
            data["dish_id"] = dish
        return client.post(API + "/feedback", json=data)

    def replan(self, rec):
        r = self.client.post(API + "/replan", json={
            "run_id": self.run, "previous_recommendation_id": rec["recommendation_id"],
        })
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["data"]

    def event(self, event_type, payload):
        result = self.client.post(API + "/admin/simulation/events", headers=OPS, json={
            "run_id": self.run, "expected_snapshot_id": self.current()["snapshot_id"], "event_type": event_type,
            "payload": payload, "idempotency_key": self.key(),
        })
        self.assertEqual(result.status_code, 200, result.text)

    def reset(self, scenario):
        r = self.client.post(API + "/admin/simulation/reset", headers=OPS,
                             json={"scenario_id": scenario, "idempotency_key": self.key()})
        self.assertEqual(r.status_code, 200)
        self.ref = self.current()
        self.run = self.ref["run_id"]

    def signals(self, event_type):
        revision, rows = self.student.effective_demo_signals(self.run)
        return revision, [row for row in rows if row["event_type"] == event_type]

    def count_feedback(self):
        with self.store.transaction() as db:
            return db.execute("SELECT count(*) FROM feedback_events").fetchone()[0]

    def test_anonymous_cookie_is_private_scope_is_enforced_and_browsing_is_quiet(self):
        old = self.client.get(API + "/snapshot", params={"run_id": self.run})
        self.assertEqual(old.status_code, 401)
        c = self.open()
        set_cookie = c.headers["set-cookie"].lower()
        self.assertIn("httponly", set_cookie)
        self.assertIn("samesite=strict", set_cookie)
        self.assertNotIn("domain=", set_cookie)
        self.assertNotIn(self.client.cookies.get(STUDENT_COOKIE), c.text)
        self.assertEqual(self.client.get(API + "/snapshot", params={"run_id": self.run}).status_code, 200)
        rec = self.rec()
        other = self.browser()
        self.open(client=other)
        self.assertEqual(self.feedback("INTENT_SELECTED", rec, DUCK, client=other).status_code, 404)
        spoofed = self.client.post(API + "/session", json={"run_id": self.run, "session_kind": "DEMO"})
        self.assertEqual(spoofed.status_code, 400)
        forged = self.client.post(API + "/feedback", json={
            "run_id": self.run, "event_type": "PREFERENCE", "dish_id": "D_A_DUCK", "idempotency_key": self.key(),
            "principal_id": "forged", "stream": "SIMULATED",
        })
        self.assertEqual(forged.status_code, 400)
        cross = self.client.post(API + "/session", headers={"origin": "https://external.invalid"},
                                 json={"run_id": self.run})
        self.assertEqual(cross.status_code, 403)
        anonymous_enrollment = self.client.post(API + "/admin/simulation/demo-enrollments",
                                                json={"run_id": self.run, "idempotency_key": self.key()})
        self.assertEqual(anonymous_enrollment.status_code, 401)
        self.assertEqual(self.count_feedback(), 0)

    def test_paging_pins_snapshot_and_rejects_conflicting_cursor(self):
        self.open()
        route = API + "/dining-options"
        start = self.client.get(route, params={"run_id": self.run, "venue_id": "V_A", "limit": 1}).json()
        self.assertEqual(start["data"]["options"][0]["bundle_id"], "B_A_BEEF")
        self.event("SET_SOLD_OUT", {"dish_id": "D_A_DUCK"})
        cursor = start["data"]["next_cursor"]
        self.assertTrue(cursor)
        page = self.client.get(route, params={"run_id": self.run, "cursor": cursor}).json()
        self.assertEqual(page["snapshot"], start["snapshot"])
        self.assertEqual(len(page["data"]["options"]), 1)
        bad = self.client.get(route, params={"run_id": self.run, "cursor": cursor, "venue_id": "V_B"})
        self.assertEqual(bad.status_code, 400)
        for bad_params in ({"limit": "0"}, {"limit": "1.0"}, {"cursor": "x" * 24}):
            self.assertEqual(self.client.get(route, params={"run_id": self.run, **bad_params}).status_code, 400)
        current = self.client.get(route, params={"run_id": self.run}).json()
        self.assertEqual(current["snapshot"]["revision"], 2)
        duck = next(row for row in current["data"]["options"] if row["bundle_id"] == DUCK)
        self.assertEqual(duck["supply_state"], "SOLD_OUT")
        self.assertEqual(self.count_feedback(), 0)

    def parity(self, request):
        response = self.rec(request)
        with self.store.transaction() as db:
            run = self.store._run(db, self.run)
            fixture = Fixture.model_validate_json(run["fixture_json"])
        snapshot, catalog = MockProvider(self.store).read(self.run)
        window = next(w for w in fixture.scoped_windows(self.run) if w.window_id == snapshot.active_window_id)
        engine = evaluate(DecisionInput(catalog=catalog, snapshot=snapshot, window=window,
                                        request=DecisionRequest.model_validate_json(json.dumps(request))))
        self.assertEqual(response, engine.body.bind(response["recommendation_id"]).model_dump(mode="json"))
        return response

    def test_d01_d02_d03_d04_d05_d07_d08_http_parity(self):
        vectors = [
            ("CLOSED_LOOP", {}, "OK"),
            ("CLOSED_LOOP", {"constraints": {"budget_max_cents": 1599}}, "OK"),
            ("TIME_PRESSURE", {"constraints": {"time_budget_min": 25}}, "OK"),
            ("PREFERENCE_CONFLICT", {"constraints": {"avoid_allergens": ["peanut"]}}, "OK"),
            ("CLOSED_LOOP", {"max_options": 4, "preferences": {"profile": "EXPLORE"}}, "OK"),
            ("PREFERENCE_CONFLICT", {"preferences": {"requested_dish_id": "D_A_DUCK"}}, "OK"),
            ("TIME_PRESSURE", {"constraints": {"time_budget_min": 21}}, "NO_FEASIBLE_OPTION"),
            ("CLOSED_LOOP", {"preferences": {"profile": "FAST", "nutrition_goal": "LIGHT"}}, "OK"),
        ]
        for scenario, request, expected in vectors:
            with self.subTest(scenario=scenario, request=request):
                self.reset(scenario)
                self.open()
                self.assertEqual(self.parity(request)["status"], expected)
                self.assertEqual(self.count_feedback(), 0)

    def test_d06_complete_registered_bundle_http_parity(self):
        self.open()
        with self.store.transaction(write=True) as db:
            current = self.store._run(db)
            catalog = Catalog.model_validate_json(current["catalog_json"])
            duck = next(b for b in catalog.bundles if b.bundle_id == DUCK)
            bundle = MealBundle(
                bundle_id="DOUBLE", venue_id=duck.venue_id, label="双份烤鸭饭", anchor_dish_id="D_A_DUCK",
                items=(BundleItem(dish_id="D_A_DUCK", portions=2),),
            )
            catalog = catalog.model_copy(update={"bundles": (*catalog.bundles, bundle)})
            db.execute("UPDATE runs SET catalog_json=? WHERE run_id=?", (catalog.model_dump_json(), self.run))
        request = {"constraints": {"budget_max_cents": 1599}, "preferences": {"requested_dish_id": "D_A_DUCK"}}
        self.assertIsNone(self.parity(request)["requested_option"])
        entries = self.client.get(API + "/dining-options", params={"run_id": self.run}).json()["data"]["options"]
        double = next(b for b in entries if b["bundle_id"] == "DOUBLE")
        self.assertEqual((double["price_cents"], double["available_bundle_portions"]), (3200, 1))

    def test_d09_old_card_rejected_replan_preserves_intent_and_explicit_new_selection(self):
        self.open()
        first = self.rec({"constraints": {"budget_max_cents": 2000, "time_budget_min": 45},
                          "preferences": {"requested_dish_id": "D_A_DUCK"}})
        self.assertEqual(first["primary"]["bundle_id"], DUCK)
        self.assertEqual(self.feedback("INTENT_SELECTED", first, DUCK).status_code, 200)
        self.event("SET_SOLD_OUT", {"dish_id": "D_A_DUCK"})
        stale = self.feedback("INTENT_SELECTED", first, DUCK)
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["error"]["code"], "STALE_SELECTION")
        self.assertEqual(stale.json()["snapshot"], self.current())
        latest = self.replan(first)
        self.assertEqual(latest["selected_bundle_id"], DUCK)
        self.assertEqual(latest["selection_state"], "NEEDS_RESELECT")
        self.assertEqual(latest["result"]["primary"]["bundle_id"], CHICKEN)
        self.assertIn("SOLD_OUT", {reason["code"] for reason in latest["change_reasons"]})
        self.assertEqual(self.count_feedback(), 1)
        self.assertEqual(self.feedback("INTENT_SELECTED", latest["result"], CHICKEN).status_code, 200)
        self.assertEqual(self.replan(latest["result"])["selected_bundle_id"], CHICKEN)

    def test_s04_ordinary_signals_excluded_demo_replay_revocation_and_regrant(self):
        self.assertFalse(self.open().json()["data"]["share_for_ops"])
        self.assertEqual(self.consent(True, 0).json()["data"]["consent_revision"], 1)
        usual = self.feedback("INTENT_SELECTED", self.rec(), DUCK).json()["data"]
        self.assertFalse(usual["share_applied"])
        self.assertEqual((usual["signals_revision"], len(self.signals("INTENT_SELECTED")[1])), (12, 5))
        self.open(self.enroll())
        self.assertEqual(self.consent(True, 0).json()["data"]["consent_revision"], 1)
        demo_rec = self.rec()
        key = self.key()
        selected = self.feedback("INTENT_SELECTED", demo_rec, DUCK, key=key).json()
        self.assertTrue(selected["data"]["share_applied"])
        self.assertEqual(selected["data"]["signals_revision"], 13)
        self.assertEqual(self.feedback("INTENT_SELECTED", demo_rec, DUCK, key=key).json(), selected)
        clash = self.feedback("INTENT_SELECTED", demo_rec, CHICKEN, key=key)
        self.assertEqual(clash.json()["error"]["code"], "IDEMPOTENCY_CONFLICT")
        self.assertEqual(len(self.signals("INTENT_SELECTED")[1]), 6)
        self.assertEqual(self.consent(False, 1).json()["data"]["signals_revision"], 14)
        self.assertEqual(len(self.signals("INTENT_SELECTED")[1]), 5)
        self.assertEqual(self.consent(True, 2).json()["data"]["signals_revision"], 14)
        self.assertEqual(len(self.signals("INTENT_SELECTED")[1]), 5)
        new = self.feedback("INTENT_SELECTED", demo_rec, CHICKEN).json()["data"]
        self.assertEqual(new["signals_revision"], 15)
        current = self.signals("INTENT_SELECTED")[1]
        self.assertEqual((len(current), current[-1]["bundle_id"], current[-1]["consent_revision"]), (6, CHICKEN, 3))

    def test_s05_latest_per_channel_late_self_report_and_inventory_independence(self):
        self.open(self.enroll())
        self.consent(True, 0)
        first = self.rec()
        for bundle in (DUCK, DUCK, CHICKEN):
            self.assertEqual(self.feedback("INTENT_SELECTED", first, bundle).status_code, 200)
        self.assertEqual(self.signals("INTENT_SELECTED")[1][-1]["bundle_id"], CHICKEN)
        self.assertEqual(self.feedback("INTENT_CLEARED", first).status_code, 200)
        self.assertEqual(len(self.signals("INTENT_SELECTED")[1]), 5)
        self.assertIsNone(self.replan(first)["selected_bundle_id"])
        before = self.current()
        report = self.feedback("DINED", first, DUCK).json()["data"]
        self.assertEqual(self.signals("DINED")[1][-1]["bundle_id"], DUCK)
        self.assertEqual(self.feedback("NOT_DINED", first).status_code, 200)
        self.assertEqual(len(self.signals("DINED")[1]), 3)
        self.assertEqual(self.current(), before)
        self.event("ADVANCE_CLOCK", {"to_clock_at": (self.store.current().snapshot.clock_at
                                                    + timedelta(minutes=20)).isoformat()})
        later = self.current()
        self.assertEqual(self.feedback("INTENT_SELECTED", first, CHICKEN).json()["error"]["code"], "STALE_SELECTION")
        late = self.feedback("DINED", first, CHICKEN).json()["data"]
        self.assertEqual(late["window_id"], report["window_id"])
        self.assertEqual(self.signals("DINED")[1][-1]["bundle_id"], CHICKEN)
        self.assertEqual(later, self.current())

    def test_a06_reset_old_cookie_and_ticket_cannot_inherit_consent_or_write(self):
        ticket = self.enroll()
        self.open(self.enroll())
        self.consent(True, 0)
        rec = self.rec()
        old_run = self.run
        old_cookie = self.client.cookies.get(STUDENT_COOKIE)
        self.reset("TIME_PRESSURE")
        old_write = self.client.post(API + "/feedback", json={
            "run_id": old_run, "event_type": "INTENT_SELECTED", "recommendation_id": rec["recommendation_id"],
            "bundle_id": DUCK, "idempotency_key": self.key(),
        })
        self.assertEqual(old_write.json()["error"]["code"], "RUN_REPLACED")
        self.assertEqual(old_write.json()["snapshot"]["run_id"], self.run)
        self.assertEqual(self.client.get(API + "/snapshot", params={"run_id": self.run}).status_code, 401)
        reused = self.client.post(API + "/session", json={"run_id": self.run, "demo_enrollment_token": ticket})
        self.assertEqual(reused.status_code, 403)
        opened = self.open().json()["data"]
        self.assertNotEqual(old_cookie, self.client.cookies.get(STUDENT_COOKIE))
        self.assertEqual(opened, {"session_kind": "USABILITY_TEST", "share_for_ops": False, "consent_revision": 0})
        self.assertEqual(self.count_feedback(), 0)
