import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from starlette.testclient import TestClient

from forkcast.fixture import load_fixture
from forkcast.http import OperatorCredential, create_app
from forkcast.models import BundleItem, Catalog, MealBundle
from forkcast.store import OpsActor, SimulationStore

API = "/api/v1"
QUERY = API + "/admin/demand-insights"
CHANNELS = ("preferred_sessions", "intended_sessions", "dined_sessions")
DUCK, CHICKEN = "D_A_DUCK", "D_B_CHICKEN"
DUCK_BUNDLE, CHICKEN_BUNDLE = "B_A_DUCK", "B_B_CHICKEN"


class DemandInsightsTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "simulation.sqlite3"
        self.store = SimulationStore(self.path)
        self.ops = {"Authorization": "Bearer local-ops-test"}
        self.app = self.operator_app(("V_A", "V_B"), control=True)
        self.admin = self.client()
        self.run = self.current()["run_id"]
        self.w1, self.w2 = self.run + ".W1", self.run + ".W2"
        self.seq = 0

    def operator_app(self, venues, control=False):
        actor = OpsActor(principal_id="test-ops", allowed_venue_ids=venues, can_control_run=control)
        return create_app(self.path, credential=OperatorCredential("local-ops-test", actor))

    def client(self, app=None):
        client = TestClient(app or self.app, base_url="http://127.0.0.1")
        self.addCleanup(client.close)
        return client

    def key(self):
        self.seq += 1
        return f"demand-test-{self.seq}"

    def ok(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["ok"])
        return body

    def error(self, response, status, code):
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(response.json()["error"]["code"], code)

    def current(self):
        return self.ok(self.admin.get(API + "/runs/current"))["snapshot"]

    def admin_write(self, path, **fields):
        response = self.admin.post(API + "/admin/simulation/" + path, headers=self.ops,
                                   json={**fields, "idempotency_key": self.key()})
        return self.ok(response)

    def student(self, *, demo=True, share=True):
        client = self.client()
        data = {"run_id": self.run}
        if demo:
            data["demo_enrollment_token"] = self.admin_write("demo-enrollments", run_id=self.run)["data"]["demo_enrollment_token"]
        session = self.ok(client.post(API + "/session", json=data))["data"]
        self.assertEqual(session, {"session_kind": "DEMO" if demo else "USABILITY_TEST",
                                   "share_for_ops": False, "consent_revision": 0})
        if share:
            self.consent(client, True, 0)
        return client

    def consent(self, client, enabled, expected):
        response = client.put(API + "/consent/ops-sharing", json={
            "run_id": self.run, "share_for_ops": enabled, "expected_consent_revision": expected,
            "idempotency_key": self.key(),
        })
        return self.ok(response)["data"]

    def recommend(self, client, request=None):
        response = client.post(API + "/recommend", json={"run_id": self.run, "request": request or {}})
        return self.ok(response)["data"]

    def feedback(self, client, event_type, *, rec=None, bundle=None, dish=None, key=None):
        data = {"run_id": self.run, "event_type": event_type, "idempotency_key": key or self.key()}
        if rec:
            data["recommendation_id"] = rec["recommendation_id"]
        if bundle:
            data["bundle_id"] = bundle
        if dish:
            data["dish_id"] = dish
        return self.ok(client.post(API + "/feedback", json=data))["data"]

    def read(self, window=None, **fields):
        response = self.admin.get(QUERY, headers=self.ops,
                                  params={"run_id": self.run, "reference_window_id": window or self.w1, **fields})
        return self.ok(response)

    def counts(self, dish, expected, *, window=None):
        data = self.read(window)["data"]
        row = next(item for item in data["rows"] if item["dish_id"] == dish)
        self.assertEqual(tuple(row[field] for field in CHANNELS), expected)
        self.assertEqual(row["visibility"], dict(zip(
            CHANNELS, ("SUPPRESSED" if value is None else "VISIBLE" for value in expected),
        )))
        return data

    def advance(self, to_clock_at):
        return self.admin_write("events", run_id=self.run, expected_snapshot_id=self.current()["snapshot_id"],
                                event_type="ADVANCE_CLOCK", payload={"to_clock_at": to_clock_at})["snapshot"]

    def test_s06_threshold_0_through_4_is_independent_per_channel(self):
        first = self.counts(CHICKEN, (None, None, None))
        self.assertEqual(self.counts(DUCK, (4, 5, 3))["signals_revision"], 12)
        row_keys = [(row["venue_id"], row["dish_id"]) for row in first["rows"]]
        for n in range(1, 5):
            with self.subTest(subjects=n):
                client = self.student()
                rec = self.recommend(client)
                visible, prior = (n if n >= 3 else None), (n - 1 if n >= 4 else None)
                self.feedback(client, "PREFERENCE", dish=CHICKEN)
                self.counts(CHICKEN, (visible, prior, prior))
                self.feedback(client, "INTENT_SELECTED", rec=rec, bundle=CHICKEN_BUNDLE)
                self.counts(CHICKEN, (visible, visible, prior))
                self.feedback(client, "DINED", rec=rec, bundle=CHICKEN_BUNDLE)
                current = self.counts(CHICKEN, (visible, visible, visible))
                self.assertEqual([(r["venue_id"], r["dish_id"]) for r in current["rows"]], row_keys)
                self.assertEqual(current["signals_revision"], 12 + n * 3)

    def test_s06_latest_declaration_and_clears_use_commit_sequence_not_event_count(self):
        clients = [(client, self.recommend(client)) for client in (self.student() for _ in range(3))]
        for client, rec in clients:
            self.feedback(client, "PREFERENCE", dish=CHICKEN)
            self.feedback(client, "INTENT_SELECTED", rec=rec, bundle=CHICKEN_BUNDLE)
            self.feedback(client, "DINED", rec=rec, bundle=CHICKEN_BUNDLE)
        client, rec = clients[0]
        self.feedback(client, "INTENT_SELECTED", rec=rec, bundle=CHICKEN_BUNDLE)
        self.feedback(client, "DINED", rec=rec, bundle=CHICKEN_BUNDLE)
        self.counts(CHICKEN, (3, 3, 3))
        self.feedback(client, "INTENT_SELECTED", rec=rec, bundle=DUCK_BUNDLE)
        self.counts(CHICKEN, (3, None, 3))
        self.counts(DUCK, (4, 6, 3))
        self.feedback(client, "INTENT_CLEARED", rec=rec)
        self.counts(DUCK, (4, 5, 3))
        self.feedback(client, "PREFERENCE", dish=DUCK)
        self.feedback(client, "NOT_DINED", rec=rec)
        self.counts(CHICKEN, (None, None, None))
        self.counts(DUCK, (5, 5, 3))

    def test_s04_sharing_current_consent_and_idempotency(self):
        ordinary = self.student(demo=False)
        self.assertFalse(self.feedback(ordinary, "INTENT_SELECTED", rec=self.recommend(ordinary),
                                       bundle=DUCK_BUNDLE)["share_applied"])
        demo = self.student(share=False)
        rec = self.recommend(demo)
        before = self.feedback(demo, "INTENT_SELECTED", rec=rec, bundle=DUCK_BUNDLE)
        self.assertFalse(before["share_applied"])
        self.assertEqual(self.consent(demo, True, 0)["signals_revision"], 12)
        self.assertEqual(self.counts(DUCK, (4, 5, 3))["signals_revision"], 12)
        key = self.key()
        valid = self.feedback(demo, "INTENT_SELECTED", rec=rec, bundle=DUCK_BUNDLE, key=key)
        self.assertTrue(valid["share_applied"])
        self.assertEqual(self.feedback(demo, "INTENT_SELECTED", rec=rec, bundle=DUCK_BUNDLE, key=key), valid)
        self.assertEqual(self.counts(DUCK, (4, 6, 3))["signals_revision"], 13)
        self.assertEqual(self.consent(demo, False, 1)["signals_revision"], 14)
        self.assertEqual(self.counts(DUCK, (4, 5, 3))["signals_revision"], 14)
        self.assertEqual(self.consent(demo, True, 2)["signals_revision"], 14)
        self.assertEqual(self.counts(DUCK, (4, 5, 3))["signals_revision"], 14)
        self.assertTrue(self.feedback(demo, "INTENT_SELECTED", rec=rec, bundle=DUCK_BUNDLE)["share_applied"])
        self.assertEqual(self.counts(DUCK, (4, 6, 3))["signals_revision"], 15)

    def test_s04_historical_withdrawal_reapplies_privacy_threshold(self):
        subjects = [self.student() for _ in range(3)]
        for client in subjects:
            self.feedback(client, "PREFERENCE", dish=CHICKEN)
        self.advance("2026-11-21T12:00:00+08:00")
        self.counts(CHICKEN, (3, None, None))
        revoked = self.consent(subjects[0], False, 1)
        self.assertEqual(self.counts(CHICKEN, (None, None, None))["signals_revision"], revoked["signals_revision"])
        self.consent(subjects[0], True, 2)
        self.feedback(subjects[0], "PREFERENCE", dish=CHICKEN)
        self.counts(CHICKEN, (None, None, None))

    def test_s05_alternative_intent_late_self_report_and_distinct_window_inventory(self):
        baseline = self.current()
        students = []
        for _ in range(3):
            client = self.student()
            rec = self.recommend(client)
            self.assertIn(CHICKEN_BUNDLE, [item["bundle_id"] for item in rec["alternatives"]])
            self.feedback(client, "INTENT_SELECTED", rec=rec, bundle=CHICKEN_BUNDLE)
            self.feedback(client, "REJECTED", rec=rec, bundle=DUCK_BUNDLE)
            students.append((client, rec))
        self.counts(CHICKEN, (None, 3, None))
        self.assertEqual(self.current(), baseline)
        w2 = self.advance("2026-11-21T12:04:00+08:00")
        for client, rec in students:
            late = self.feedback(client, "DINED", rec=rec, bundle=DUCK_BUNDLE)
            self.assertEqual(late["window_id"], self.w1)
            self.feedback(client, "PREFERENCE", dish=CHICKEN)
        result = self.counts(DUCK, (4, 5, 6))
        self.assertEqual(result["observed_until"], "2026-11-21T12:00:00+08:00")
        self.counts(CHICKEN, (None, 3, None))
        w2_data = self.counts(CHICKEN, (3, None, None), window=self.w2)
        self.assertEqual(w2_data["observed_until"], w2["clock_at"])
        self.counts(DUCK, (None, None, None), window=self.w2)
        self.assertEqual(self.current(), w2)

    def test_s05_registered_multi_component_bundle_counts_only_anchor(self):
        fixture = load_fixture()
        variant = fixture.model_copy(update={"dishes": tuple(
            dish.model_copy(update={"active": True, "w1": 6}) if dish.dish_id == "D_B_EGG" else dish
            for dish in fixture.dishes
        )})
        # Build a registered test bundle whose first item differs from its anchor.
        with patch("forkcast.store.load_fixture", return_value=variant):
            self.run = self.admin_write("reset", scenario_id="CLOSED_LOOP")["snapshot"]["run_id"]
            self.w1 = self.run + ".W1"
        with self.store.transaction(write=True) as db:
            run = self.store._run(db, self.run)
            catalog = Catalog.model_validate_json(run["catalog_json"])
            meal = MealBundle(
                bundle_id="B_TEST_COMBO", venue_id="V_B", label="Synthetic combo", anchor_dish_id=CHICKEN,
                items=(BundleItem(dish_id="D_B_EGG", portions=1), BundleItem(dish_id=CHICKEN, portions=1)),
            )
            catalog = catalog.model_copy(update={"bundles": tuple(
                meal if bundle.bundle_id == CHICKEN_BUNDLE else bundle for bundle in catalog.bundles
            )})
            db.execute("UPDATE runs SET catalog_json=? WHERE run_id=?", (catalog.model_dump_json(), self.run))
        for _ in range(3):
            client = self.student()
            rec = self.recommend(client, {"preferences": {"requested_dish_id": CHICKEN}})
            self.assertEqual(rec["requested_option"]["bundle_id"], meal.bundle_id)
            self.feedback(client, "INTENT_SELECTED", rec=rec, bundle=meal.bundle_id)
            self.feedback(client, "DINED", rec=rec, bundle=meal.bundle_id)
        self.counts(CHICKEN, (None, 3, 3))
        self.counts("D_B_EGG", (None, None, None))

    def test_future_and_rolled_back_rows_do_not_shadow_committed_declarations(self):
        clients = [self.student() for _ in range(3)]
        for client in clients:
            first = self.feedback(client, "PREFERENCE", dish=CHICKEN)
        with self.store.transaction(write=True) as db:
            # Fault-injection only. This future event uses a different UTC offset and a later sequence.
            db.execute("""INSERT INTO feedback_events
                SELECT commit_seq + 1, 'future',run_id,principal_id,window_id,?,stream,event_type,channel,
                       recommendation_id,bundle_id,?,consent_revision FROM feedback_events WHERE event_id=?""",
                ("2026-11-21T03:40:00.000001Z", DUCK, first["event_id"]))
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with self.store.transaction(write=True) as db:
                db.execute("UPDATE feedback_events SET dish_id=? WHERE event_id=?", (DUCK, first["event_id"]))
                raise RuntimeError("rollback")
        self.counts(CHICKEN, (3, None, None))
        self.advance("2026-11-21T11:40:00.000001+08:00")
        self.counts(CHICKEN, (None, None, None))
        self.counts(DUCK, (5, 5, 3))

    def test_ops_scope_roles_and_closed_query_contract(self):
        params = {"run_id": self.run, "reference_window_id": self.w1}
        student = self.student(demo=False)
        self.error(student.get(QUERY, params=params), 401, "UNAUTHENTICATED")
        self.error(student.get(QUERY, params=params, headers={
            "Authorization": "Bearer external-agent", "X-Role": "OPS",
        }), 401, "UNAUTHENTICATED")
        self.error(self.admin.get(QUERY, params=params, headers={**self.ops, "Origin": "https://other.invalid"}),
                   403, "FORBIDDEN")
        scoped = self.client(self.operator_app(("V_B",)))
        rows = self.ok(scoped.get(QUERY, params=params, headers=self.ops))["data"]["rows"]
        self.assertEqual({item["venue_id"] for item in rows}, {"V_B"})
        self.assertEqual(self.ok(scoped.get(QUERY, params={**params, "venue_id": "V_B"}, headers=self.ops))["data"]["rows"], rows)
        for venue in ("V_A", "V_MISSING"):
            self.error(scoped.get(QUERY, params={**params, "venue_id": venue}, headers=self.ops), 403, "FORBIDDEN")
        for fields in ({"role": "OPS"}, {"principal_id": "person-1"}, {"from": "11:40"}, {"limit": "1"},
                       {"data_mode": "LIVE"}, {"allergens": "peanut"}, {"snapshot_id": "x"}):
            self.error(self.admin.get(QUERY, params={**params, **fields}, headers=self.ops), 400, "INVALID_INPUT")
        self.error(self.admin.get(QUERY, params={"run_id": self.run}, headers=self.ops), 400, "INVALID_INPUT")
        self.error(self.admin.get(QUERY, params=[*params.items(), ("run_id", self.run)], headers=self.ops), 400, "INVALID_INPUT")
        self.error(self.admin.get(QUERY, params={**params, "reference_window_id": self.w2}, headers=self.ops),
                   400, "INVALID_INPUT")
        self.error(self.admin.get(QUERY, params={**params, "reference_window_id": "OTHER.W1"}, headers=self.ops),
                   404, "NOT_FOUND")

    def test_read_is_deterministic_closed_and_does_not_mutate_state(self):
        client = self.student()
        receipt = self.feedback(client, "PREFERENCE", dish=CHICKEN)
        with self.store.transaction() as db:
            before = tuple(db.iterdump())
        first = self.read()
        self.assertEqual(first, self.read())
        self.assertEqual(first["snapshot"], self.current())
        self.assertEqual(set(first), {"schema_version", "ok", "snapshot", "data", "data_mode"})
        self.assertEqual(first["data_mode"], "SIMULATION")
        data = first["data"]
        self.assertEqual(set(data), {"signals_revision", "observed_until", "rows"})
        self.assertEqual(data["signals_revision"], receipt["signals_revision"])
        self.assertEqual(data["observed_until"], first["snapshot"]["clock_at"])
        for row in data["rows"]:
            self.assertEqual(set(row), {"run_id", "reference_window_id", "venue_id", "dish_id",
                                        "visibility", "signals_revision", "observed_until", *CHANNELS})
            self.assertEqual(row["signals_revision"], data["signals_revision"])
            self.assertEqual(row["observed_until"], data["observed_until"])
        self.counts(CHICKEN, (None, None, None))
        public = self.ok(self.admin.get(API + "/runs/current"))
        menu = self.ok(client.get(API + "/dining-options", params={"run_id": self.run}))
        for response in (public, menu):
            self.assertNotIn("preferred_sessions", str(response))
            self.assertNotIn(receipt["event_id"], str(response))
        with self.store.transaction() as db:
            self.assertEqual(tuple(db.iterdump()), before)

    def test_run_reset_and_live_are_isolated(self):
        student = self.student()
        self.feedback(student, "INTENT_SELECTED", rec=self.recommend(student), bundle=DUCK_BUNDLE)
        self.counts(DUCK, (4, 6, 3))
        old_run, old_window = self.run, self.w1
        new = self.admin_write("reset", scenario_id="CLOSED_LOOP")
        self.run = new["snapshot"]["run_id"]
        self.w1 = self.run + ".W1"
        self.assertNotEqual(self.run, old_run)
        self.error(self.admin.get(QUERY, params={"run_id": old_run, "reference_window_id": old_window},
                                  headers=self.ops), 404, "NOT_FOUND")
        self.assertEqual(self.counts(DUCK, (4, 5, 3))["signals_revision"], 12)
        live_path = self.path.with_name("live.sqlite3")
        actor = OpsActor(principal_id="test-ops", allowed_venue_ids=("V_A", "V_B"), can_control_run=True)
        live = self.client(create_app(live_path, "LIVE", OperatorCredential("local-ops-test", actor)))
        self.error(live.get(QUERY, params={"run_id": self.run, "reference_window_id": self.w1}, headers=self.ops),
                   403, "MODE_DISABLED")
        self.assertFalse(live_path.exists())
