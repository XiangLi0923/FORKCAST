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

    def decision(self, budget=2000, minutes=45, profile="DEFAULT", sold_out=False, dish="D_A_DUCK", stage="CURRENT"):
        preferences = {"profile": profile}
        if dish is not None:
            preferences["requested_dish_id"] = dish
        response = self.client.post("/api/v1/demo/decision", json={
            "request": {"constraints": {"budget_max_cents": budget, "time_budget_min": minutes},
                        "preferences": preferences, "max_options": 4},
            "duck_sold_out": sold_out, "stage": stage,
        })
        self.assertEqual(response.status_code, 200)
        return response.json()["data"]

    def test_page_assets_and_visible_entry_points(self):
        for url in ("/", "/ops"):
            self.assertIn('href="/demo"', self.client.get(url).text)
        for url in ("/demo", "/student-assets/demo.mjs", "/student-assets/demo.css", "/student-assets/demo-hero.webp"):
            self.assertEqual(self.client.get(url).status_code, 200)
        self.assertIn("赛后体验更新", self.client.get("/demo").text)

    def test_budget_and_time_change_search_space(self):
        default = self.decision()
        self.assertEqual(default["venue_count"], 6)
        self.assertEqual(len(default["candidates"]), 40)
        self.assertEqual(len({row["id"] for row in default["candidates"]}), 40)
        self.assertEqual(sum(row["available"] for row in default["candidates"]), 36)
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

    def test_next_window_compares_same_conditions_with_and_without_approval(self):
        run_path = "/api/v1/runs/current"
        before = self.client.get(run_path).json()
        no_approval = self.decision(stage="W2_BASE")
        approved = self.decision(stage="W2_APPROVED")
        self.assertEqual(no_approval["supply_comparison"]["proposal_portions"], 3)
        self.assertEqual(no_approval["supply_comparison"]["current_duck_stock"], 0)
        self.assertEqual(approved["supply_comparison"]["current_duck_stock"], 3)
        self.assertNotEqual(no_approval["result"]["primary"]["bundle_id"], "B_A_DUCK")
        self.assertEqual(approved["result"]["primary"]["bundle_id"], "B_A_DUCK")
        self.assertEqual(before, self.client.get(run_path).json())

    def test_guided_operations_returns_plan_only_after_explicit_action(self):
        path = "/api/v1/demo/operations"
        run_path = "/api/v1/runs/current"
        before = self.client.get(run_path).json()
        request = {"action": "SUMMARIZE", "chosen_dish_id": "D_A_DUCK"}
        response = self.client.post(path, json=request)
        self.assertEqual(response.status_code, 200)
        summary = response.json()["data"]
        self.assertEqual(summary["signals"]["intent"], 5)
        self.assertNotIn("plan", summary)
        self.assertFalse(summary["included"])

        request.update(action="PLAN", share_intent=True)
        data = self.client.post(path, json=request).json()["data"]
        self.assertTrue(data["included"])
        self.assertEqual(data["signals"]["intent"], 6)
        self.assertEqual(data["plan"]["demand_proxy_portions"], 6)
        self.assertEqual(data["plan"]["feasible_extra_portions"], 3)
        self.assertIn("INGREDIENT_LIMIT", data["plan"]["evidence_codes"])
        request.update(chosen_dish_id="D_B_CHICKEN")
        unrelated = self.client.post(path, json=request).json()["data"]
        self.assertEqual(unrelated["signals"]["intent"], 5)
        self.assertFalse(unrelated["included"])
        self.assertEqual(self.client.get(run_path).json(), before)
        self.assertEqual(self.client.post(path, json={**request, "chosen_dish_id": "invented"}).status_code, 400)
