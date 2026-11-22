"""The public showcase uses a broad synthetic catalog without writing to the live run."""

import tempfile
import unittest
from pathlib import Path

from starlette.testclient import TestClient

from forkcast.http import create_app


class DecisionDemoTest(unittest.TestCase):
    def setUp(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.client = self.enterContext(TestClient(create_app(Path(temp) / "demo.sqlite3"),
                                                   base_url="http://localhost"))

    def decision(self, budget=2000, minutes=45, profile="DEFAULT", sold_out=False, dish="D_A_DUCK"):
        preferences = {"profile": profile}
        if dish is not None:
            preferences["requested_dish_id"] = dish
        response = self.client.post("/api/v1/demo/decision", json={
            "request": {"constraints": {"budget_max_cents": budget, "time_budget_min": minutes},
                        "preferences": preferences, "max_options": 4},
            "duck_sold_out": sold_out,
        })
        self.assertEqual(response.status_code, 200)
        return response.json()["data"]

    def test_page_assets_and_visible_entry_points(self):
        for url in ("/", "/ops"):
            self.assertIn('href="/demo"', self.client.get(url).text)
        for url in ("/demo", "/student-assets/demo.mjs", "/student-assets/demo.css"):
            self.assertEqual(self.client.get(url).status_code, 200)
        self.assertIn("赛后体验更新", self.client.get("/demo").text)

    def test_budget_and_time_change_search_space(self):
        default = self.decision()
        self.assertEqual(len(default["candidates"]), 16)
        self.assertEqual(sum(row["available"] for row in default["candidates"]), 12)
        self.assertEqual(default["result"]["primary"]["bundle_id"], "B_A_DUCK")
        self.assertEqual(len(default["result"]["alternatives"]), 3)
        budget = self.decision(budget=1500, profile="VALUE")
        self.assertNotEqual(budget["result"]["primary"]["bundle_id"], "B_A_DUCK")
        duck = next(row for row in budget["candidates"] if row["dish_id"] == "D_A_DUCK")
        self.assertEqual(duck["blocks"], ["OVER_BUDGET"])
        self.assertIsNone(duck["score"])
        fast = self.decision(minutes=20, profile="FAST", dish=None)
        self.assertEqual(sum(row["score"] is not None for row in fast["candidates"]), 6)
        self.assertTrue(any("OVER_TIME" in row["blocks"] for row in fast["candidates"]))
        self.assertIsNone(self.decision(budget=500)["result"]["primary"])

    def test_sold_out_is_local_to_each_request(self):
        run_path = "/api/v1/runs/current"
        before = self.client.get(run_path).json()
        default = self.decision()
        changed = self.decision(sold_out=True)
        self.assertNotEqual(changed["result"]["primary"]["bundle_id"], "B_A_DUCK")
        duck = next(row for row in changed["candidates"] if row["dish_id"] == "D_A_DUCK")
        self.assertIn("SOLD_OUT", duck["blocks"])
        self.assertEqual(default, self.decision())
        self.assertEqual(before, self.client.get(run_path).json())
