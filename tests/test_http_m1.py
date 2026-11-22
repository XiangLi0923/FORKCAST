import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from starlette.testclient import TestClient

from forkcast.http import OperatorCredential, create_app
from forkcast.store import MockProvider, OpsActor, SimulationStore


class HttpM1Acceptance(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "test.sqlite"
        self.auth = {"Authorization": "Bearer fixture-test-token"}

    def client(self, mode="SIMULATION", venues=("V_A", "V_B"), control=True):
        actor = OpsActor(principal_id="ops-test", allowed_venue_ids=venues, can_control_run=control)
        return TestClient(
            create_app(self.path, mode, OperatorCredential("fixture-test-token", actor)),
            base_url="http://127.0.0.1",
        )

    @staticmethod
    def current(client):
        return client.get("/api/v1/runs/current").json()["snapshot"]

    @staticmethod
    def queue(snapshot, venue="V_A", key="queuekey"):
        return {
            "run_id": snapshot["run_id"], "expected_snapshot_id": snapshot["snapshot_id"],
            "event_type": "SET_QUEUE", "payload": {"venue_id": venue, "queue_depth": 16, "throughput_per_min": 2},
            "idempotency_key": key,
        }

    def test_a05_p0_live_fresh_start_leaves_no_database(self):
        with self.assertLogs("forkcast.http", level="WARNING") as logs, self.client("LIVE") as client:
            health = client.get("/health")
            self.assertEqual(health.status_code, 503)
            self.assertEqual(health.json()["recognized_modes"], ["SIMULATION", "LIVE"])
            self.assertEqual(health.json()["enabled_modes"], ["SIMULATION"])
            self.assertFalse(health.json()["ready"])
            self.assertIn("MODE_DISABLED", logs.output[0])
            reset = client.post("/api/v1/admin/simulation/reset", headers=self.auth,
                                json={"scenario_id": "CLOSED_LOOP", "idempotency_key": "livekey1"})
            self.assertEqual(reset.status_code, 403)
            self.assertEqual(reset.json()["error"]["code"], "MODE_DISABLED")
            self.assertIsNone(reset.json()["data_mode"])
        self.assertEqual(list(self.path.parent.iterdir()), [])

    def test_a05_p0_live_existing_database_is_unchanged(self):
        with self.client() as client:
            before = self.current(client)
            health = client.get("/health")
            self.assertEqual(health.status_code, 200)
            self.assertTrue(health.json()["ready"])
            changed = client.post("/api/v1/admin/simulation/events", headers=self.auth, json=self.queue(before))
            self.assertEqual(changed.status_code, 200)
            before = self.current(client)
        initial_bytes = self.path.read_bytes()
        with self.assertLogs("forkcast.http", level="WARNING"), self.client("LIVE") as disabled:
            self.assertEqual(disabled.get("/api/v1/runs/current").json()["error"]["code"], "MODE_DISABLED")
            denied = disabled.post("/api/v1/admin/simulation/events", headers=self.auth, json=self.queue(before))
            self.assertEqual(denied.json()["error"]["code"], "MODE_DISABLED")
        self.assertEqual(self.path.read_bytes(), initial_bytes)
        with self.client() as reopened:
            self.assertEqual(self.current(reopened), before)

    def test_a05_p0_mode_field_and_internal_event_are_rejected_without_writes(self):
        with self.client() as client:
            before = self.current(client)
            raw = self.queue(before)
            commands = [
                {**raw, "data_mode": "LIVE"},
                {**raw, "payload": {**raw["payload"], "data_mode": "LIVE"}},
                {**raw, "event_type": "SIM_SALE", "payload": {"dish_id": "D_B_CHICKEN", "portions": 1}},
                {**raw, "event_type": "SET_SOLD_OUT"},
            ]
            for command in commands:
                with self.subTest(command=command):
                    failure = client.post("/api/v1/admin/simulation/events", json=command, headers=self.auth)
                    self.assertEqual(failure.status_code, 400)
                    self.assertEqual(failure.json()["error"]["code"], "INVALID_INPUT")
            bad_query = client.get("/api/v1/runs/current", params={"data_mode": "LIVE"})
            self.assertEqual(bad_query.json()["error"]["code"], "INVALID_INPUT")
            reset = client.post("/api/v1/admin/simulation/reset", headers=self.auth, json={
                "scenario_id": "CLOSED_LOOP", "idempotency_key": "modekey1", "data_mode": "LIVE",
            })
            self.assertEqual(reset.json()["error"]["code"], "INVALID_INPUT")
            self.assertEqual(self.current(client), before)
            self.assertEqual(SimulationStore(self.path).event_log(before["run_id"]), ())

    def test_a05_p0_control_authorization_and_run_scope(self):
        with self.client(venues=("V_A",)) as client:
            before = self.current(client)
            path = "/api/v1/admin/simulation/events"
            anonymous = client.post(path, json=self.queue(before))
            self.assertEqual(anonymous.status_code, 401)
            foreign = client.post(path, json=self.queue(before, "V_B"), headers=self.auth)
            self.assertEqual(foreign.status_code, 403)
            reset = client.post("/api/v1/admin/simulation/reset", headers=self.auth,
                                json={"scenario_id": "CLOSED_LOOP", "idempotency_key": "scopekey"})
            self.assertEqual(reset.status_code, 403)
            clock = {**self.queue(before), "event_type": "ADVANCE_CLOCK",
                     "payload": {"to_clock_at": "2026-11-21T11:41:00+08:00"}}
            self.assertEqual(client.post(path, json=clock, headers=self.auth).status_code, 403)
            cross = client.post(path, json=self.queue(before), headers={**self.auth, "Origin": "https://example.org"})
            self.assertEqual(cross.status_code, 403)
            self.assertEqual(self.current(client), before)
            allowed = client.post(path, json=self.queue(before), headers=self.auth)
            self.assertEqual(allowed.status_code, 200)
            self.assertEqual(allowed.json()["snapshot"]["revision"], 2)

    def test_m1_http_reset_discovery_and_history(self):
        with self.client() as client:
            before = self.current(client)
            reset = client.post("/api/v1/admin/simulation/reset", headers=self.auth,
                                json={"scenario_id": "TIME_PRESSURE", "idempotency_key": "resetkey"})
            self.assertEqual(reset.status_code, 200)
            after = self.current(client)
            self.assertEqual(after, reset.json()["snapshot"])
            self.assertNotEqual(after["run_id"], before["run_id"])
            self.assertEqual(after["revision"], 1)
            refused = client.post("/api/v1/admin/simulation/events", headers=self.auth, json=self.queue(before))
            self.assertEqual(refused.json()["error"]["code"], "RUN_REPLACED")
            peek = client.get("/api/v1/snapshot", params={"run_id": after["run_id"]}, headers=self.auth)
            self.assertEqual(peek.status_code, 200)
            self.assertEqual(peek.json()["data"], after)
            data, _ = MockProvider(SimulationStore(self.path)).read(after["run_id"])
            self.assertEqual(data.venue_states[0].queue_min.value.max, 9)
