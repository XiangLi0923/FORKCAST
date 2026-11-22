import sqlite3
import unittest
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier

from forkcast.errors import Fault
from forkcast.fixture import load_fixture
from forkcast.models import AdvanceClock, IntRange, Queue, Sale, SimulationEvent, SoldOut
from forkcast.store import ControlRequest, MockProvider, OpsActor, ResetRequest, SimulationStore


class StoreAcceptance(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "forkcast.sqlite3"
        self.store = SimulationStore(self.path)
        self.provider = MockProvider(self.store)
        self.actor = OpsActor(principal_id="ops1", allowed_venue_ids=("V_A", "V_B"), can_control_run=True)
        self.start = self.store.bootstrap("SIMULATION").snapshot

    def command(self, event_type, payload, key, *, ref=None):
        ref = self.start if ref is None else ref
        return ControlRequest(
            run_id=ref.run_id, expected_snapshot_id=ref.snapshot_id,
            event_type=event_type, payload=payload, idempotency_key=key,
        )

    def queue(self, n, key):
        return self.command("SET_QUEUE", Queue(venue_id="V_A", queue_depth=n, throughput_per_min=2), key)

    def schedule_sale(self, at, portions, suffix, sequence=0):
        # Extra trusted fixture event for S02/S03. The HTTP interface cannot add this event.
        sale = SimulationEvent(
            run_id=self.start.run_id, event_id=f"{self.start.run_id}.{suffix}", scheduled_at=at,
            sequence=sequence, event_type="SIM_SALE", target_id="D_B_CHICKEN",
            payload=Sale(dish_id="D_B_CHICKEN", portions=portions),
        )
        with self.store.transaction(write=True) as db:
            self.store._insert_event(db, sale)

    @staticmethod
    def stock(snapshot, dish_id):
        return next(row.stock_portions.value for row in snapshot.dish_states if row.dish_id == dish_id)

    def counts(self):
        with self.store.transaction() as db:
            return tuple(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                         for table in ("runs", "snapshots", "events", "audit", "receipts"))

    def test_s01_reseed_is_deterministic_and_reset_isolated(self):
        first, first_catalog = self.provider.read(self.start.run_id)
        revision, first_seed = self.store.seed_state(self.start.run_id)
        self.assertEqual(revision, 12)
        self.assertEqual(Counter(event.event_type for event in first_seed.feedback),
                         {"PREFERENCE": 4, "INTENT_SELECTED": 5, "DINED": 3})
        changed = self.store.control(self.queue(16, "s01queue"), self.actor)
        self.assertEqual(changed.snapshot.revision, 2)

        request = ResetRequest(scenario_id="CLOSED_LOOP", idempotency_key="s01reset")
        second_ref = self.store.reset(request, self.actor).snapshot
        second, second_catalog = self.provider.read(second_ref.run_id)
        second_revision, second_seed = self.store.seed_state(second_ref.run_id)
        third_ref = self.store.reset(request.model_copy(update={"idempotency_key": "s01again"}), self.actor).snapshot
        self.assertEqual(len({self.start.run_id, second_ref.run_id, third_ref.run_id}), 3)
        self.assertEqual(self.store.current().snapshot, third_ref)
        self.assertEqual(self.store.reset(request, self.actor).snapshot, second_ref)
        self.assertEqual(self.store.current().snapshot, third_ref)
        with self.assertRaises(Fault) as old_read:
            self.provider.read(self.start.run_id, self.start.snapshot_id)
        self.assertEqual(old_read.exception.code, "NOT_FOUND")
        with self.assertRaises(Fault) as old_write:
            self.store.control(self.queue(16, "s01queue"), self.actor)
        self.assertEqual(old_write.exception.code, "RUN_REPLACED")

        def normalized(model, run_id):
            return model.model_dump_json(exclude={"sessions"}).replace(run_id, "<run>")

        third, third_catalog = self.provider.read(third_ref.run_id)
        third_revision, third_seed = self.store.seed_state(third_ref.run_id)
        for snapshot, catalog, seed_revision, seed in (
            (second, second_catalog, second_revision, second_seed),
            (third, third_catalog, third_revision, third_seed),
        ):
            self.assertEqual(snapshot.ref.revision, 1)
            self.assertEqual(normalized(first, self.start.run_id), normalized(snapshot, snapshot.ref.run_id))
            self.assertEqual(first_catalog, catalog)
            self.assertEqual(seed_revision, 12)
            self.assertEqual(normalized(first_seed, self.start.run_id), normalized(seed, snapshot.ref.run_id))
        self.assertEqual([row.model_dump(exclude={"updated_at", "run_id", "principal_id"}) for row in first_seed.sessions],
                         [row.model_dump(exclude={"updated_at", "run_id", "principal_id"}) for row in third_seed.sessions])
        self.assertEqual(len({ref.snapshot_id for ref in (self.start, second_ref, third_ref)}), 3)

    def test_s02_sold_out_queue_same_tick_sale_and_snapshot_history(self):
        w2_start = load_fixture().windows[1].starts_at
        self.schedule_sale(w2_start, 1, "sale")
        start, _ = self.provider.read(self.start.run_id)
        out = self.store.control(self.command("SET_SOLD_OUT", SoldOut(dish_id="D_A_DUCK", reason="LOSS"),
                                              "s02loss1"), self.actor).snapshot
        until_out, _ = self.provider.read(self.start.run_id, out.snapshot_id)
        self.assertEqual(self.stock(until_out, "D_A_DUCK"), 0)
        queue = Queue(venue_id="V_A", queue_depth=16, throughput_per_min=2)
        moved = self.store.control(self.command("SET_QUEUE", queue, "s02queue", ref=out), self.actor).snapshot
        queued, _ = self.provider.read(self.start.run_id, moved.snapshot_id)
        self.assertEqual(queued.venue_states[0].queue_min.value, IntRange(min=7, max=9))

        command = self.command("ADVANCE_CLOCK", AdvanceClock(to_clock_at=w2_start), "s02clock", ref=moved)
        end = self.store.control(command, self.actor).snapshot
        w2, _ = self.provider.read(self.start.run_id)
        self.assertEqual(end.revision, 4)
        self.assertTrue(w2.active_window_id.endswith(".W2"))
        self.assertEqual(self.stock(w2, "D_B_CHICKEN"), 11)
        self.assertEqual(self.stock(w2, "D_A_DUCK"), 0)
        self.assertEqual(w2.venue_states[0].queue_min.value, IntRange(min=1, max=3))
        self.assertEqual(w2.venue_states[0].queue_min.as_of, w2_start)
        self.assertEqual(self.provider.read(self.start.run_id, self.start.snapshot_id)[0], start)
        self.assertEqual(self.stock(start, "D_A_DUCK"), 2)
        log = self.store.event_log(self.start.run_id)
        self.assertEqual([row.event.event_type for row in log],
                         ["SET_SOLD_OUT", "SET_QUEUE", "ACTIVATE_WINDOW", "SIM_SALE", "ADVANCE_CLOCK"])
        self.assertEqual([row.applied_revision for row in log], [2, 3, 4, 4, 4])
        self.assertEqual(log[0].removed_portions, 2)
        self.assertEqual(log[0].event.payload.reason, "LOSS")
        self.assertEqual(log[3].removed_portions, 1)
        self.assertEqual(self.store.control(command, self.actor).snapshot, end)
        self.assertEqual(self.store.event_log(self.start.run_id), log)

        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            with self.store.transaction(write=True) as db:
                db.execute("UPDATE snapshots SET frame_json = '{}' WHERE snapshot_id = ?", (self.start.snapshot_id,))

    def test_s03_batch_rollback_and_conflicts_across_connections(self):
        at = self.start.clock_at + timedelta(minutes=1)
        self.schedule_sale(at, 1, "one", 0)
        self.schedule_sale(at, 12, "overdraw", 1)
        before = self.counts()
        bad = self.command("ADVANCE_CLOCK", AdvanceClock(to_clock_at=at), "s03bad01")
        with self.assertRaises(Fault) as caught:
            self.store.control(bad, self.actor)
        self.assertEqual(caught.exception.code, "INVALID_INPUT")
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.store.event_log(self.start.run_id), ())
        self.assertEqual(self.store.current().snapshot, self.start)
        self.assertEqual(self.stock(self.provider.read(self.start.run_id)[0], "D_B_CHICKEN"), 12)

        requests = (self.queue(10, "s03race1"), self.queue(20, "s03race2"))
        barrier = Barrier(2)

        def race(request):
            barrier.wait(timeout=5)
            try:
                return SimulationStore(self.path).control(request, self.actor)
            except Fault as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as tasks:
            results = tuple(tasks.map(race, requests))
        winner = next(i for i, result in enumerate(results) if result != "SNAPSHOT_CONFLICT")
        self.assertEqual(results[1 - winner], "SNAPSHOT_CONFLICT")
        current = results[winner]
        self.assertEqual(current.snapshot.revision, 2)
        self.assertEqual(self.store.control(requests[winner], self.actor), current)
        with self.assertRaises(Fault) as conflict:
            self.store.control(self.queue(30, requests[winner].idempotency_key), self.actor)
        self.assertEqual(conflict.exception.code, "IDEMPOTENCY_CONFLICT")
        self.assertEqual(self.counts(), (1, 2, 5, 1, 1))

    def test_s03_sql_failure_rolls_back_snapshot_and_event(self):
        with self.store.transaction(write=True) as db:
            db.execute("""CREATE TRIGGER reject_audit BEFORE INSERT ON audit
                          BEGIN SELECT RAISE(ABORT, 'S03 forced audit failure'); END;""")
        before = self.counts()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "forced audit failure"):
            self.store.control(self.queue(20, "s03audit"), self.actor)
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.store.current().snapshot, self.start)
        self.assertEqual(self.store.event_log(self.start.run_id), ())

    def test_m11_available_dish_becomes_sold_out_in_one_transaction(self):
        snapshot, _ = self.provider.read(self.start.run_id)
        before = next(row for row in snapshot.dish_states if row.dish_id == "D_A_DUCK")
        self.assertEqual((before.service_status.value, before.stock_portions.value), ("AVAILABLE", 2))

        request = self.command("SET_SOLD_OUT", SoldOut(dish_id="D_A_DUCK", reason="LOSS"), "m11avail")
        result = self.store.control(request, self.actor)
        snapshot, _ = self.provider.read(self.start.run_id)
        after = next(row for row in snapshot.dish_states if row.dish_id == "D_A_DUCK")
        self.assertEqual((after.service_status.value, after.stock_portions.value), ("SOLD_OUT", 0))
        self.assertEqual(result.snapshot.revision, self.start.revision + 1)
        entry, = self.store.event_log(self.start.run_id)
        self.assertEqual(entry.removed_portions, 2)
        self.assertEqual(entry.event.payload.reason, "LOSS")
        self.assertEqual(entry.applied_revision, result.snapshot.revision)

    def test_m11_unavailable_rejection_keeps_snapshot_and_database_unchanged(self):
        before = self.provider.read(self.start.run_id)
        dish = next(row for row in before[0].dish_states if row.dish_id == "D_A_TOFU")
        self.assertEqual(dish.service_status.value, "UNAVAILABLE")
        database = self.path.read_bytes()
        request = self.command("SET_SOLD_OUT", SoldOut(dish_id="D_A_TOFU"), "m11unavail")
        with self.assertRaises(Fault) as caught:
            self.store.control(request, self.actor)
        self.assertEqual(caught.exception.code, "INVALID_INPUT")
        self.assertEqual(caught.exception.snapshot, self.start)
        self.assertEqual(self.provider.read(self.start.run_id), before)
        self.assertEqual(self.store.event_log(self.start.run_id), ())
        self.assertEqual(self.path.read_bytes(), database)

    def test_m11_already_sold_out_replays_only_the_original_request(self):
        payload = SoldOut(dish_id="D_A_DUCK")
        request = self.command("SET_SOLD_OUT", payload, "m11original")
        result = self.store.control(request, self.actor)
        before = self.provider.read(self.start.run_id)
        event_log = self.store.event_log(self.start.run_id)
        database = self.path.read_bytes()

        self.assertEqual(self.store.control(request, self.actor), result)
        duplicate = self.command("SET_SOLD_OUT", payload, "m11different", ref=result.snapshot)
        with self.assertRaises(Fault) as caught:
            self.store.control(duplicate, self.actor)
        self.assertEqual(caught.exception.code, "INVALID_INPUT")
        self.assertEqual(caught.exception.snapshot, result.snapshot)
        self.assertEqual(self.provider.read(self.start.run_id), before)
        self.assertEqual(self.store.event_log(self.start.run_id), event_log)
        self.assertEqual(self.path.read_bytes(), database)
