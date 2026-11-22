import sqlite3
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from forkcast.decision_models import Constraints, DecisionRequest, Preferences
from forkcast.errors import Fault
from forkcast.models import AdvanceClock
from forkcast.store import ControlRequest, OpsActor, SimulationStore
from forkcast.student_models import DemoEnrollmentRequest, OpenSession, RecommendRequest, ReplanRequest
from forkcast.student_service import StudentService


class StudentIdentityTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = SimulationStore(Path(tmp.name) / "db.sqlite")
        self.start = self.store.bootstrap("SIMULATION").snapshot
        self.run_id = self.start.run_id
        self.now = datetime(2026, 11, 21, tzinfo=UTC)
        self.service = StudentService(self.store, now=lambda: self.now)
        self.ops = OpsActor(principal_id="ops", allowed_venue_ids=("V_A", "V_B"), can_control_run=True)

    def ticket(self, key="enroll.01"):
        return self.service.issue_enrollment(DemoEnrollmentRequest(run_id=self.run_id, idempotency_key=key), self.ops)

    def test_open_starts_unshared_and_reuses_the_current_private_session(self):
        opened, token = self.service.open_session(OpenSession(run_id=self.run_id), None)
        self.assertEqual(opened.data, {"session_kind": "USABILITY_TEST", "share_for_ops": False, "consent_revision": 0})
        self.assertEqual(self.service.open_session(OpenSession(run_id=self.run_id), token), (opened, None))
        self.assertNotIn(token, self.store.path.read_bytes().decode("latin1"))

    def test_ops_ticket_replay_expiration_and_single_use(self):
        first = self.ticket()
        self.assertEqual(first, self.ticket())
        opaque = first.data["demo_enrollment_token"]
        self.assertEqual(datetime.fromisoformat(first.data["expires_at"]) - self.now, timedelta(seconds=120))
        request = OpenSession(run_id=self.run_id, demo_enrollment_token=opaque)
        demo, token = self.service.open_session(request, None)
        self.assertEqual(demo.data["session_kind"], "DEMO")
        self.assertFalse(demo.data["share_for_ops"])
        self.assertNotIn(opaque, self.store.path.read_bytes().decode("latin1"))
        with self.assertRaises(Fault) as used:
            self.service.open_session(request, token)
        self.assertEqual(used.exception.code, "FORBIDDEN")
        expire = self.ticket("enroll.02").data["demo_enrollment_token"]
        self.now += timedelta(seconds=120)
        with self.assertRaises(Fault) as expired:
            self.service.open_session(OpenSession(run_id=self.run_id, demo_enrollment_token=expire), None)
        self.assertEqual(expired.exception.code, "FORBIDDEN")

    def test_ticket_consumption_rolls_back_with_failed_session(self):
        ticket = self.ticket().data["demo_enrollment_token"]
        request = OpenSession(run_id=self.run_id, demo_enrollment_token=ticket)
        with self.store.transaction(write=True) as db:
            db.execute("CREATE TRIGGER blocked_session BEFORE INSERT ON student_sessions BEGIN "
                       "SELECT RAISE(ABORT, 'test insert failure'); END;")
        with self.assertRaises(sqlite3.IntegrityError):
            self.service.open_session(request, None)
        with self.store.transaction(write=True) as db:
            db.execute("DROP TRIGGER blocked_session")
        self.assertEqual(self.service.open_session(request, None)[0].data["session_kind"], "DEMO")

    def test_record_is_owned_and_replanning_restores_original_deadline(self):
        _, owner = self.service.open_session(OpenSession(run_id=self.run_id), None)
        _, other = self.service.open_session(OpenSession(run_id=self.run_id), None)
        request = RecommendRequest(run_id=self.run_id, request=DecisionRequest(
            constraints=Constraints(budget_max_cents=2000, time_budget_min=45),
            preferences=Preferences(requested_dish_id="D_A_DUCK"),
        ))
        first = self.service.recommend(request, owner)
        self.assertEqual(first.data["primary"]["match_score"], 644)
        replan_request = ReplanRequest(run_id=self.run_id, previous_recommendation_id=first.data["recommendation_id"])
        with self.assertRaises(Fault) as foreign:
            self.service.replan(replan_request, other)
        self.assertEqual(foreign.exception.code, "NOT_FOUND")
        advance = AdvanceClock(to_clock_at=self.start.clock_at + timedelta(minutes=1))
        next_ref = self.store.control(ControlRequest(
            run_id=self.run_id, expected_snapshot_id=self.start.snapshot_id, event_type="ADVANCE_CLOCK",
            payload=advance, idempotency_key="student.test-clock",
        ), self.ops).snapshot
        result = self.service.replan(replan_request, owner)
        self.assertEqual(result.snapshot, next_ref)
        self.assertNotEqual(result.data["result"]["recommendation_id"], first.data["recommendation_id"])
        with self.store.transaction() as db:
            rows = db.execute("SELECT principal_id, recommendation_id FROM recommendation_records").fetchall()
            self.assertEqual(len(rows), 2)
            records = [self.service._record(db, self.run_id, row["principal_id"], row["recommendation_id"]) for row in rows]
            self.assertEqual(records[0].deadline_at, records[1].deadline_at)
            self.assertEqual(records[0].deadline_at, self.start.clock_at + timedelta(minutes=45))
            self.assertTrue(records[1].normalized_request.origin_assumed)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM feedback_events").fetchone()[0], 0)
