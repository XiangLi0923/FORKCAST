"""Start real loopback HTTP and check the OPS read with temporary demo inputs."""

import json
import os
import socket
import subprocess
import sys
import time
from http.cookiejar import CookieJar
from pathlib import Path
from secrets import token_urlsafe
from tempfile import TemporaryDirectory
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener
from uuid import uuid4


def http_client(port, token, *, cookies=None):
    opener = build_opener(HTTPCookieProcessor(cookies if cookies is not None else CookieJar()))

    def request(path, *, fields=None, ops=False, method="GET", expected=200, origin=None):
        headers = {"Authorization": f"Bearer {token}"} if ops else {}
        if origin is not None:
            headers["Origin"] = origin
        body = None
        if fields is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(fields).encode()
        query = Request(f"http://127.0.0.1:{port}{path}", body, headers, method=method)
        try:
            reply = opener.open(query, timeout=3)
        except HTTPError as error:
            reply = error
        with reply:
            result = json.load(reply)
            assert reply.status == expected, (reply.status, result)
            return result

    return request


def wait_ready(request):
    started = time.monotonic() + 12
    while True:
        try:
            health = request("/health")
            break
        except URLError:
            if time.monotonic() >= started:
                raise
            time.sleep(0.1)
    assert health["ready"] and health["app_version"] == "0.3.0-m3.3"


def smoke(port, token, _database):
    request = http_client(port, token)
    wait_ready(request)
    api = "/api/v1"
    ref = request(api + "/runs/current")["snapshot"]
    run = {"run_id": ref["run_id"]}
    keys = ("preferred_sessions", "intended_sessions", "dined_sessions")
    path = api + "/admin/demand-insights?" + urlencode({**run, "reference_window_id": ref["run_id"] + ".W1"})

    def observe(label, expected_intent, revision):
        result = request(path, ops=True)
        assert result["ok"] and result["data_mode"] == "SIMULATION" and result["snapshot"] == ref
        data = result["data"]
        assert data["observed_until"] == ref["clock_at"] and data["signals_revision"] == revision
        duck = next(row for row in data["rows"] if row["dish_id"] == "D_A_DUCK")
        masked = next(row for row in data["rows"] if row["dish_id"] == "D_B_CHICKEN")
        assert tuple(duck[key] for key in keys) == (4, expected_intent, 3)
        assert tuple(masked[key] for key in keys) == (None, None, None)
        assert set(masked["visibility"].values()) == {"SUPPRESSED"}
        print(f"SIMULATION · 模拟运营数据 | {label} | W1 | P/I/A=4/{expected_intent}/3 | "
              f"鸡腿饭三渠道 SUPPRESSED/null | signals_revision={revision} | observed_until={data['observed_until']}")

    observe("初始种子账", 5, 12)
    ticket = request(api + "/admin/simulation/demo-enrollments", method="POST", ops=True,
                     fields={**run, "idempotency_key": uuid4().hex})["data"]["demo_enrollment_token"]
    session = request(api + "/session", method="POST", fields={**run, "demo_enrollment_token": ticket})["data"]
    assert session["session_kind"] == "DEMO" and not session["share_for_ops"]
    assert request(path, expected=401)["error"]["code"] == "UNAUTHENTICATED"

    def consent(enabled, revision):
        return request(api + "/consent/ops-sharing", method="PUT", fields={
            **run, "share_for_ops": enabled, "expected_consent_revision": revision, "idempotency_key": uuid4().hex,
        })

    consent(True, 0)
    rec = request(api + "/recommend", method="POST", fields={**run, "request": {}})["data"]
    selected = request(api + "/feedback", method="POST", fields={
        **run, "event_type": "INTENT_SELECTED", "recommendation_id": rec["recommendation_id"],
        "bundle_id": "B_A_DUCK", "idempotency_key": uuid4().hex,
    })
    assert selected["data"]["share_applied"]
    observe("DEMO 授权后选餐", 6, 13)
    consent(False, 1)
    observe("撤回授权", 5, 14)
    consent(True, 2)
    observe("重新授权且无新声明", 5, 14)
    print("PASS: OPS 读取、学生 401、撤回立即生效、推荐快照未变化。")


def run_service(check):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    token = token_urlsafe(32)
    with TemporaryDirectory(prefix="forkcast-demand-http-") as directory:
        path = Path(directory)
        database = path / "demo.sqlite3"
        env = {
            **os.environ, "FORKCAST_DATABASE": str(database), "FORKCAST_DATA_MODE": "SIMULATION",
            "FORKCAST_OPS_TOKEN": token, "FORKCAST_OPS_VENUES": "V_A,V_B", "FORKCAST_OPS_RUN_CONTROL": "1",
        }
        with (path / "http.log").open("w+", encoding="utf-8") as log:
            with subprocess.Popen(
                [sys.executable, "-B", "-m", "forkcast", "--port", str(port)],
                env=env, stdout=log, stderr=subprocess.STDOUT,
            ) as service:
                try:
                    check(port, token, database)
                except BaseException:
                    log.seek(0)
                    print(log.read(), file=sys.stderr)
                    raise
                finally:
                    service.terminate()


if __name__ == "__main__":
    run_service(smoke)
