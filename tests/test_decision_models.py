import unittest

from pydantic import ValidationError

from forkcast.decision_models import DecisionInput, DecisionRequest
from forkcast.fixture import load_fixture
from forkcast.simulator import Simulator


class DecisionModelTests(unittest.TestCase):
    def test_raw_input_length_precedes_canonical_dedup(self):
        raw = '{"preferences":{"taste_tags":["rice","light","rice"]},"max_options":2}'
        request = DecisionRequest.model_validate_json(raw)
        self.assertEqual(request.preferences.taste_tags, ("light", "rice"))
        self.assertTrue(request.explicit_max_options)
        self.assertFalse(DecisionRequest().explicit_max_options)
        tags = ",".join(['"rice"'] * 11)
        with self.assertRaises(ValidationError):
            DecisionRequest.model_validate_json('{"preferences":{"taste_tags":[' + tags + ']}}')

    def test_strict_limits_defaults_and_closed_objects(self):
        default = DecisionRequest.model_validate_json("{}")
        self.assertTrue(default.origin_assumed)
        self.assertEqual(default.origin_zone_id, "DEMO_DEFAULT")
        self.assertEqual(default.max_options, 2)
        for raw in ('{"max_options":0}', '{"max_options":5}', '{"max_options":null}',
                    '{"constraints":{"budget_max_cents":true}}', '{"constraints":{"time_budget_min":181}}',
                    '{"preferences":{"free_text":"hello"}}', '{"constraints":{"avoid_allergens":null}}'):
            with self.subTest(raw=raw), self.assertRaises(ValidationError):
                DecisionRequest.model_validate_json(raw)

    def test_window_and_catalog_version_are_bound_to_snapshot(self):
        fixture = load_fixture()
        simulator = Simulator(fixture, fixture.scenario("CLOSED_LOOP"), "test")
        snapshot = simulator.initial_frame().supply
        catalog = fixture.catalog(simulator.scenario)
        fields = {"request": DecisionRequest(), "catalog": catalog, "snapshot": snapshot}
        self.assertEqual(DecisionInput(**fields, window=simulator.windows[0]).snapshot, snapshot)
        with self.assertRaises(ValidationError):
            DecisionInput(**fields, window=simulator.windows[1])
        fields["catalog"] = catalog.model_copy(update={"catalog_version": "vwrong"})
        with self.assertRaises(ValidationError):
            DecisionInput(**fields, window=simulator.windows[0])
