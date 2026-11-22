import unittest
from datetime import timedelta

from pydantic import ValidationError

from forkcast.fixture import load_fixture
from forkcast.models import Fact, IntRange, Queue, SimulationEvent, SnapshotRef, TimeRange


class ModelsFixtureTests(unittest.TestCase):
    def setUp(self):
        self.fixture = load_fixture()

    def test_models_are_strict_and_frozen(self):
        with self.assertRaises(ValidationError):
            IntRange(min="1", max=2)
        with self.assertRaises(ValidationError):
            IntRange(min=3, max=2)
        with self.assertRaises(ValidationError):
            IntRange(min=0, max=2, surplus=True)
        with self.assertRaises(ValidationError):
            IntRange(min=0, max=2).min = 1
        with self.assertRaises(ValidationError):
            TimeRange.model_validate_json('{"starts_at":"2026-11-21T11:40:00","ends_at":"2026-11-21T12:00:00+08:00"}')
        with self.assertRaises(ValidationError):
            SimulationEvent(
                run_id="run", event_id="run.1", scheduled_at=self.fixture.starts_at, sequence=1,
                event_type="SET_SOLD_OUT", target_id=None, payload=Queue(
                    venue_id="V_A", queue_depth=0, throughput_per_min=2,
                ),
            )

    def test_freshness_differentiates_missing_future_and_expired(self):
        now = self.fixture.starts_at
        later = now + timedelta(minutes=2)
        known = Fact[IntRange](**self.fixture.fact(IntRange(min=1, max=3), now, later))
        self.assertEqual(known.freshness(now, dynamic=True), "FRESH")
        self.assertEqual(known.freshness(now - timedelta(seconds=1), dynamic=True), "UNKNOWN")
        self.assertEqual(known.freshness(later, dynamic=True), "STALE")
        self.assertEqual(Fact[IntRange](**self.fixture.fact(None)).freshness(now, dynamic=True), "UNKNOWN")

    def test_catalog_and_variants(self):
        normal = self.fixture.catalog(self.fixture.scenario("CLOSED_LOOP"))
        conflict = self.fixture.catalog(self.fixture.scenario("PREFERENCE_CONFLICT"))
        self.assertEqual(len(normal.venues), 2)
        self.assertEqual(len(normal.dishes), 16)
        self.assertEqual(len(normal.bundles), 16)
        self.assertEqual(normal.catalog_version, "v03.2")
        self.assertEqual(normal.dishes[0].allergens.value, ())
        self.assertIsNone(conflict.dishes[0].allergens.value)
        self.assertEqual(self.fixture.scenario("TIME_PRESSURE").queue_override.estimate(), IntRange(min=7, max=9))
        self.assertEqual(self.fixture.scenario("PREFERENCE_CONFLICT").queue_override.estimate(), IntRange(min=11, max=13))

    def test_seed_records_and_claimed_options(self):
        w1, _ = self.fixture.scoped_windows("rA")
        ref = SnapshotRef(
            run_id="rA", snapshot_id="rA.s1", revision=1, clock_at=self.fixture.starts_at,
            data_mode="SIMULATION", catalog_version="v03.2",
        )
        seed = self.fixture.seed_state(ref, w1, self.fixture.starts_at)
        self.assertEqual(seed.signals_revision, 12)
        self.assertEqual(len(seed.sessions), 5)
        self.assertEqual([event.commit_seq for event in seed.feedback], list(range(1, 13)))
        self.assertEqual([event.event_type for event in seed.feedback],
                         ["PREFERENCE"] * 4 + ["INTENT_SELECTED"] * 5 + ["DINED"] * 3)
        recs = {rec.recommendation_id: rec for rec in seed.recommendations}
        for event in seed.feedback:
            if event.recommendation_id:
                rec = recs[event.recommendation_id]
                self.assertEqual(event.principal_id, rec.principal_id)
                self.assertIn(event.bundle_id, rec.selectable_bundle_ids)
