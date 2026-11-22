"""Verify the public edge contract without relying on forwarded request headers."""

import tempfile
import unittest
from pathlib import Path

from starlette.testclient import TestClient

from forkcast.http import OperatorCredential, PublicDeployment, create_app
from forkcast.store import OpsActor


PUBLIC = "https://aks-forkcast.netlify.app"
UPSTREAM = "forkcast-campus.onrender.com"


class PublicHTTPTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        operator = OperatorCredential("test-operator", OpsActor(
            principal_id="test_ops", allowed_venue_ids=("V_A",), can_control_run=False,
        ))
        self.app = create_app(Path(directory) / "public.sqlite3", credential=operator,
                              public=PublicDeployment(PUBLIC, UPSTREAM))
        self.edge = self.enterContext(TestClient(self.app, base_url=PUBLIC))
        self.run = self.edge.get("/api/v1/runs/current").json()["snapshot"]["run_id"]

    def test_edge_reads_and_secure_host_only_student_session(self):
        for path in ("/", "/ops", "/health", "/student-assets/student.mjs"):
            with self.subTest(path=path):
                self.assertEqual(self.edge.get(path).status_code, 200)
        enrolled = self.edge.post("/api/v1/session", json={"run_id": self.run},
                                  headers={"Origin": PUBLIC, "Host": UPSTREAM})
        self.assertEqual(enrolled.status_code, 200)
        cookie = enrolled.headers["set-cookie"]
        for directive in ("HttpOnly", "SameSite=strict", "Secure", "Path=/"):
            self.assertIn(directive, cookie)
        self.assertNotIn("domain=", cookie.lower())
        recommendation = self.edge.post("/api/v1/recommend", json={"run_id": self.run, "request": {}},
                                        headers={"Origin": PUBLIC, "Host": UPSTREAM})
        self.assertEqual(recommendation.status_code, 200)
        self.assertTrue(recommendation.json()["ok"])

    def test_bad_hosts_origins_and_spoofed_forwarding_are_rejected(self):
        self.assertEqual(self.edge.get("/health", headers={"Host": "invalid.example"}).status_code, 400)
        for headers in (
            {"Origin": "https://foreign.example", "X-Forwarded-Host": "foreign.example"},
            {"Origin": "https://" + UPSTREAM},
            {"Origin": PUBLIC, "Sec-Fetch-Site": "same-site"},
            {"Origin": PUBLIC, "Sec-Fetch-Site": "cross-site"},
        ):
            with self.subTest(headers=headers):
                result = self.edge.post("/api/v1/session", json={"run_id": self.run}, headers=headers)
                self.assertEqual((result.status_code, result.json()["error"]["code"]), (403, "FORBIDDEN"))
                self.assertNotIn("set-cookie", result.headers)

    def test_health_probe_and_operator_auth_keep_distinct_requirements(self):
        with TestClient(self.app, base_url="http://" + UPSTREAM) as upstream:
            self.assertEqual(upstream.get("/health").status_code, 200)
            session = upstream.post("/api/v1/session", json={"run_id": self.run}, headers={"Origin": PUBLIC})
            self.assertIn("Secure", session.headers["set-cookie"])
        params = {"run_id": self.run, "reference_window_id": self.run + ".W1"}
        path = "/api/v1/admin/demand-insights"
        self.assertEqual(self.edge.get(path, params=params).status_code, 401)
        self.assertEqual(self.edge.get(path, params=params, headers={
            "Host": UPSTREAM, "Authorization": "Bearer test-operator",
        }).status_code, 200)
        receipt = self.edge.post("/api/v1/interaction-receipts", json={}, headers={
            "X-Forwarded-Proto": "https", "X-Forwarded-Host": "aks-forkcast.netlify.app",
        })
        self.assertEqual((receipt.status_code, receipt.json()["error"]["code"]), (403, "FORBIDDEN"))

    def test_config_rejects_wildcards_http_ports_and_paths(self):
        for origin, host in (
            ("http://example.com", UPSTREAM), (PUBLIC + "/", UPSTREAM), (PUBLIC + ":443", UPSTREAM),
            (PUBLIC, "*.onrender.com"), (PUBLIC, ""), ("https://user@example.com", UPSTREAM),
        ):
            with self.subTest(origin=origin, host=host), self.assertRaises(ValueError):
                PublicDeployment(origin, host)
