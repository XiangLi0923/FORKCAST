import unittest
from datetime import timedelta

from forkcast.errors import Fault
from forkcast.fixture import load_fixture
from forkcast.models import AdvanceClock, IntRange, Queue, Sale, SimulationEvent, SoldOut, event_target
from forkcast.simulator import Simulator


class SimulatorTests(unittest.TestCase):
    def setUp(self):
        self.fixture = load_fixture()
        self.sim = Simulator(self.fixture, self.fixture.scenario("CLOSED_LOOP"), "example")
        self.frame = self.sim.initial_frame()

    def event(self, label, payload, at, sequence=0, suffix="cmd"):
        return SimulationEvent(
            run_id="example", event_id=f"example.{suffix}", event_type=label,
            scheduled_at=at, sequence=sequence, target_id=event_target(payload), payload=payload,
        )

    @staticmethod
    def stock(frame, dish):
        return next(row.stock_portions.value for row in frame.supply.dish_states if row.dish_id == dish)

    def test_same_tick_window_before_sale_and_no_stock_carry(self):
        w1, w2 = self.sim.windows
        command = self.event("ADVANCE_CLOCK", AdvanceClock(to_clock_at=w2.starts_at), w2.starts_at)
        sale = self.event("SIM_SALE", Sale(dish_id="D_B_CHICKEN", portions=1), w2.starts_at, suffix="sale")
        frame, journal = self.sim.transition(self.frame, command, (sale, *self.fixture.boundaries("example", w2)))
        self.assertEqual([entry.event.event_type for entry in journal], ["ACTIVATE_WINDOW", "SIM_SALE", "ADVANCE_CLOCK"])
        self.assertEqual(self.stock(frame, "D_B_CHICKEN"), 11)
        self.assertEqual(self.stock(frame, "D_A_DUCK"), 0)
        self.assertEqual(self.stock(self.frame, "D_A_DUCK"), 2)
        self.assertEqual(self.frame.supply.active_window_id, w1.window_id)
        self.assertEqual(frame.supply.active_window_id, w2.window_id)
        self.assertEqual(frame.supply.ref.revision, 2)

    def test_sold_out_queue_and_close(self):
        clock = self.fixture.starts_at
        frame, records = self.sim.transition(
            self.frame, self.event("SET_SOLD_OUT", SoldOut(dish_id="D_A_DUCK", reason="LOSS"), clock), (),
        )
        self.assertEqual(records[0].removed_portions, 2)
        self.assertEqual(self.stock(frame, "D_A_DUCK"), 0)
        frame, _ = self.sim.transition(
            frame, self.event("SET_QUEUE", Queue(venue_id="V_A", queue_depth=16, throughput_per_min=2), clock), (),
        )
        self.assertEqual(frame.supply.venue_states[0].queue_min.value, IntRange(min=7, max=9))
        w2 = self.sim.windows[-1]
        command = self.event("ADVANCE_CLOCK", AdvanceClock(to_clock_at=w2.ends_at), w2.ends_at)
        frame, _ = self.sim.transition(frame, command, self.fixture.boundaries("example", w2))
        self.assertIsNone(frame.supply.active_window_id)
        self.assertEqual(frame.supply.dish_states, ())
        self.assertEqual([row.service_status.value for row in frame.supply.venue_states], ["CLOSED", "CLOSED"])
        self.assertTrue(all(row.queue_min.value is None for row in frame.supply.venue_states))

    def test_rejects_sold_out_transition_for_unavailable_dish(self):
        command = self.event(
            "SET_SOLD_OUT", SoldOut(dish_id="D_A_TOFU"), self.fixture.starts_at,
        )
        with self.assertRaises(Fault) as caught:
            self.sim.transition(self.frame, command, ())
        self.assertEqual(caught.exception.code, "INVALID_INPUT")

    def test_rejects_new_sold_out_transition_for_already_sold_out_dish(self):
        clock = self.fixture.starts_at
        command = self.event("SET_SOLD_OUT", SoldOut(dish_id="D_A_DUCK"), clock)
        frame, _ = self.sim.transition(self.frame, command, ())
        duplicate = self.event("SET_SOLD_OUT", SoldOut(dish_id="D_A_DUCK"), clock, suffix="repeat")
        with self.assertRaises(Fault) as caught:
            self.sim.transition(frame, duplicate, ())
        self.assertEqual(caught.exception.code, "INVALID_INPUT")

    def test_invalid_clock_and_overdraw(self):
        clock = self.fixture.starts_at
        for target in (clock - timedelta(seconds=1), clock, self.sim.windows[-1].ends_at + timedelta(seconds=1)):
            with self.subTest(target=target), self.assertRaisesRegex(Fault, "Clock"):
                self.sim.transition(self.frame, self.event("ADVANCE_CLOCK", AdvanceClock(to_clock_at=target), target), ())
        with self.assertRaisesRegex(Fault, "exceeds"):
            self.sim._apply(self.frame, self.event("SIM_SALE", Sale(dish_id="D_A_DUCK", portions=3), clock))
