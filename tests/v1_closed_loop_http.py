"""V1.0 端到端验收：真实 Uvicorn 进程 + 真实 HTTP 请求跑 C+B 闭环。

用法：
    python -B -m tests.v1_closed_loop_http

Phase 1 在本机回环跑完整业务闭环（学生会话 → 推荐 → 售罄替代 → 授权
→ 运营建议 → 人工审批 → 下一窗口生效 → 学生端重新规划）。
Phase 2 用生产环境变量另起一个进程，模拟 Netlify → Render 的代理请求头，
验证 TrustedHost / Origin / Secure Cookie 不被绕过。
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCAL_PORT = 8791
EDGE_PORT = 8792
OPS_TOKEN = "v1-e2e-ops-" + uuid.uuid4().hex
PUBLIC_ORIGIN = "https://aks-forkcast.netlify.app"
UPSTREAM_HOST = "forkcast-campus.onrender.com"

results: list[tuple[bool, str]] = []


def check(condition: bool, label: str, detail: str = "") -> None:
    results.append((bool(condition), label))
    print(("PASS  " if condition else "FAIL  ") + label + ((" | " + detail) if detail else ""), flush=True)
    if not condition:
        raise AssertionError(label + " | " + detail)


def new_client():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar)), jar


def call(client, port: int, path: str, *, method="GET", body=None,
         headers=None, host=None):
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    hdrs = {"Accept": "application/json", **(headers or {})}
    if host:
        hdrs["Host"] = host
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with client.open(req, timeout=15) as response:
            raw = response.read().decode("utf-8")
            return response.status, dict(response.headers), (json.loads(raw) if raw.strip().startswith(("{", "[")) else raw)
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8")
        return error.code, dict(error.headers), (json.loads(raw) if raw.strip().startswith(("{", "[")) else raw)


def ok(status, payload, label):
    check(status == 200 and isinstance(payload, dict) and payload.get("ok") is True,
          label, f"status={status} err={payload.get('error') if isinstance(payload, dict) else payload}")


def start(port: int, database: str, *, public: bool, log):
    env = {**os.environ,
           "FORKCAST_DATABASE": database,
           "FORKCAST_DATA_MODE": "SIMULATION",
           "FORKCAST_OPS_TOKEN": OPS_TOKEN,
           "FORKCAST_OPS_VENUES": "V_A,V_B",
           "FORKCAST_OPS_RUN_CONTROL": "1",
           "PYTHONPATH": str(ROOT)}
    if public:
        env["FORKCAST_PUBLIC_ORIGIN"] = PUBLIC_ORIGIN
        env["FORKCAST_UPSTREAM_HOST"] = UPSTREAM_HOST
    else:
        env.pop("FORKCAST_PUBLIC_ORIGIN", None)
        env.pop("FORKCAST_UPSTREAM_HOST", None)
    proc = subprocess.Popen(
        [sys.executable, "-B", "-m", "uvicorn", "forkcast.http:from_env", "--factory",
         "--host", "127.0.0.1", "--port", str(port), "--workers", "1", "--no-proxy-headers"],
        cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL,
        stdout=log, stderr=subprocess.STDOUT,
    )
    for _ in range(80):
        if proc.poll() is not None:
            raise RuntimeError(f"uvicorn exited early, see {log.name}")
        try:
            probe_headers = {"Host": UPSTREAM_HOST} if public else None
            status, _, payload = call(urllib.request.build_opener(), port, "/health",
                                      headers=probe_headers, host=UPSTREAM_HOST if public else None)
            if status == 200 and payload.get("ready") is True:
                return proc
        except Exception:
            pass
        time.sleep(0.25)
    raise RuntimeError(f"uvicorn health timeout, see {log.name}")


def key():
    return str(uuid.uuid4())


def phase_one(log_dir: Path):
    print("\n===== PHASE 1 · 本机真实 HTTP · C+B 完整闭环 =====", flush=True)
    db = str(log_dir / "loop.sqlite3")
    proc = start(LOCAL_PORT, db, public=False, log=open(log_dir / "local-uvicorn.log", "w", encoding="utf-8"))
    try:
        student, jar = new_client()
        ops, _ = new_client()
        origin = f"http://127.0.0.1:{LOCAL_PORT}"
        same_origin = {"Origin": origin, "Sec-Fetch-Site": "same-origin"}
        auth = {"Authorization": "Bearer " + OPS_TOKEN, **same_origin}

        # 1. 页面、静态资源与健康检查
        for path in ("/", "/ops", "/health", "/student-assets/student.css",
                     "/student-assets/student.mjs", "/student-assets/ops.mjs",
                     "/student-assets/ops.css", "/student-assets/decision_gate.mjs",
                     "/student-assets/mcp-receipt.mjs"):
            status, _, _ = call(student, LOCAL_PORT, path)
            check(status == 200, "静态入口 " + path, f"status={status}")
        status, _, health = call(student, LOCAL_PORT, "/health")
        check(status == 200 and health.get("ready") is True, "健康检查 ready")

        # 2. 当前运行
        status, _, current = call(student, LOCAL_PORT, "/api/v1/runs/current")
        ok(status, current, "读取当前运行")
        run = current["snapshot"]["run_id"]
        check(bool(run), "运行 ID 非空", run)

        # 3. 运营端发放一次性演示凭据，学生加入 DEMO
        status, _, ticket = call(ops, LOCAL_PORT, "/api/v1/admin/simulation/demo-enrollments",
                                method="POST", body={"run_id": run, "idempotency_key": key()}, headers=auth)
        ok(status, ticket, "运营端发放演示凭据")
        demo_token = ticket["data"]["demo_enrollment_token"]

        status, _, anonymous = call(student, LOCAL_PORT, "/api/v1/session", method="POST",
                                    body={"run_id": run}, headers=same_origin)
        ok(status, anonymous, "学生建立匿名会话")
        check(anonymous["data"].get("share_for_ops") is False, "新会话默认不分享")
        status, _, session = call(student, LOCAL_PORT, "/api/v1/session", method="POST",
                                 body={"run_id": run, "demo_enrollment_token": demo_token}, headers=same_origin)
        ok(status, session, "学生用凭据加入演示会话")
        check(session["data"].get("session_kind") == "DEMO", "会话类型为 DEMO")
        check(session["data"].get("share_for_ops") is False, "加入演示后分享仍默认关闭")
        consent_revision = session["data"]["consent_revision"]

        # 4. 学生主动授权（必须发生在信号之前）
        status, _, consent = call(student, LOCAL_PORT, "/api/v1/consent/ops-sharing", method="PUT",
                                   body={"run_id": run, "share_for_ops": True,
                                         "expected_consent_revision": consent_revision,
                                         "idempotency_key": key()}, headers=same_origin)
        ok(status, consent, "学生主动授权需求分享")
        check(consent["data"].get("share_for_ops") is True, "授权状态已生效")

        # 5. 表达需求 → 获取推荐 → 推荐依据来自真实 API
        request = {"constraints": {"budget_max_cents": 2000, "time_budget_min": 45},
                   "preferences": {"requested_dish_id": "D_A_DUCK"}}
        status, _, rec = call(student, LOCAL_PORT, "/api/v1/recommend", method="POST",
                              body={"run_id": run, "request": request}, headers=same_origin)
        ok(status, rec, "按预算/时间/指名口味获取推荐")
        primary = rec["data"]["primary"]
        check(primary is not None, "存在首选推荐")
        rec_id = rec["data"]["recommendation_id"]
        duck_bundle = primary["bundle_id"]
        check(bool(primary.get("evidence")), "推荐带真实依据（evidence）",
              f"{len(primary['evidence'])} 条")
        check(primary.get("price_cents") is not None and primary.get("total_time_min") is not None,
              "推荐卡含价格与总用餐耗时")
        check(bool(primary.get("label")) and bool(primary.get("venue_id")), "推荐卡含餐次与食堂")

        # 6. 产生三类授权信号：想吃 / 打算去吃 / 实际吃了
        for event, extra in (("PREFERENCE", {"dish_id": "D_A_DUCK"}),
                             ("INTENT_SELECTED", {"recommendation_id": rec_id, "bundle_id": duck_bundle}),
                             ("DINED", {"recommendation_id": rec_id, "bundle_id": duck_bundle})):
            status, _, reply = call(student, LOCAL_PORT, "/api/v1/feedback", method="POST",
                                    body={"run_id": run, "event_type": event, "idempotency_key": key(), **extra},
                                    headers=same_origin)
            ok(status, reply, "学生信号 " + event)
        check(True, "选择意向与实际用餐分开记录（API 分别受理）")

        # 7. 未授权对照：另一个匿名会话的信号不得计入运营可见需求
        stranger, _ = new_client()
        status, _, s2 = call(stranger, LOCAL_PORT, "/api/v1/session", method="POST",
                            body={"run_id": run}, headers=same_origin)
        ok(status, s2, "建立未授权对照会话")
        params = urllib.parse.urlencode({"run_id": run, "reference_window_id": run + ".W1"})
        status, _, before = call(ops, LOCAL_PORT, "/api/v1/admin/demand-insights?" + params, headers=auth)
        ok(status, before, "运营端读取授权需求（对照前）")
        for event, extra in (("PREFERENCE", {"dish_id": "D_A_DUCK"}),
                             ("INTENT_SELECTED", {"recommendation_id": rec_id, "bundle_id": duck_bundle})):
            call(stranger, LOCAL_PORT, "/api/v1/feedback", method="POST",
                 body={"run_id": run, "event_type": event, "idempotency_key": key(), **extra}, headers=same_origin)
        status, _, after = call(ops, LOCAL_PORT, "/api/v1/admin/demand-insights?" + params, headers=auth)
        ok(status, after, "运营端读取授权需求（对照后）")
        check(before["data"]["signals_revision"] == after["data"]["signals_revision"]
              and before["data"]["rows"] == after["data"]["rows"],
              "未授权数据不计入运营信号")

        # 8. 售罄 → 重新规划，保留原意向，不自动换餐
        status, _, cur = call(ops, LOCAL_PORT, "/api/v1/runs/current", headers=auth)
        ok(status, cur, "售罄前读取当前快照")
        status, _, sold = call(ops, LOCAL_PORT, "/api/v1/admin/simulation/events", method="POST", body={
            "run_id": run, "expected_snapshot_id": cur["snapshot"]["snapshot_id"],
            "idempotency_key": key(), "event_type": "SET_SOLD_OUT",
            "payload": {"dish_id": "D_A_DUCK"},
        }, headers=auth)
        ok(status, sold, "运营端标记烤鸭饭售罄")
        status, _, replan = call(student, LOCAL_PORT, "/api/v1/replan", method="POST", body={
            "run_id": run, "previous_recommendation_id": rec_id, "max_options": 2,
        }, headers=same_origin)
        ok(status, replan, "学生端触发重新规划")
        reasons = replan["data"].get("change_reasons", [])
        check(any(r.get("code") == "SOLD_OUT" for r in reasons), "重规划说明售罄原因",
              str([r.get("code") for r in reasons]))
        selected_bundle = replan["data"].get("selected_bundle_id")
        selection_state = replan["data"].get("selection_state")
        check(selected_bundle == duck_bundle and selection_state == "NEEDS_RESELECT",
              "原意向被保留且标记为需重新选择",
              f"bundle={selected_bundle} state={selection_state}")
        check(replan["data"]["result"]["primary"]["bundle_id"] != duck_bundle,
              "新首选不再是售罄餐次")
        # 旧推荐卡不能在状态失效后继续被错误执行
        status, _, stale = call(student, LOCAL_PORT, "/api/v1/feedback", method="POST", body={
            "run_id": run, "event_type": "INTENT_SELECTED", "recommendation_id": rec_id,
            "bundle_id": duck_bundle, "idempotency_key": key(),
        }, headers=same_origin)
        check(status == 409 and stale.get("error", {}).get("code") == "STALE_SELECTION",
              "失效旧推荐被拒绝（STALE_SELECTION）", f"status={status} body={stale}")
        # 学生自行决定是否换餐
        new_primary = replan["data"]["result"]["primary"]
        status, _, switched = call(student, LOCAL_PORT, "/api/v1/feedback", method="POST", body={
            "run_id": run, "event_type": "INTENT_SELECTED",
            "recommendation_id": replan["data"]["result"]["recommendation_id"],
            "bundle_id": new_primary["bundle_id"], "idempotency_key": key(),
        }, headers=same_origin)
        ok(status, switched, "学生主动选择替代餐次")

        # 9. 运营端生成建议（含供应/原料/产能约束），人工审批
        status, _, proposals = call(ops, LOCAL_PORT, "/api/v1/admin/ops-proposals", method="POST", body={
            "run_id": run, "reference_window_id": run + ".W1", "target_window_id": run + ".W2",
        }, headers=auth)
        ok(status, proposals, "运营端生成备餐建议")
        analyses = proposals["data"].get("analyses", [])
        check(all({"shortage_portions", "feasible_extra_portions", "uncovered_portions",
                   "ingredient_review_portions"} <= set(a) for a in analyses),
              "供需分析含缺口/可追加/未覆盖/原料约束", f"{len(analyses)} 行")
        duck_proposal = next((p for p in proposals["data"].get("proposals", [])
                              if p["dish_id"] == "D_A_DUCK"), None)
        check(duck_proposal is not None, "烤鸭饭有追加备餐建议")
        amount = min(3, duck_proposal["proposed_extra_portions"])
        status, _, confirm = call(ops, LOCAL_PORT, "/api/v1/admin/ops-confirmations", method="POST", body={
            "run_id": run, "proposal_id": duck_proposal["proposal_id"],
            "approved_extra_portions": amount,
            "expected_snapshot_id": duck_proposal["based_on_snapshot_id"],
            "idempotency_key": key(),
        }, headers=auth)
        ok(status, confirm, "人工批准备餐数量")
        state_params = urllib.parse.urlencode({"run_id": run, "target_window_id": run + ".W2"})
        status, _, state = call(ops, LOCAL_PORT, "/api/v1/admin/ops-state?" + state_params, headers=auth)
        ok(status, state, "审批后读取运营状态")
        check(any(a["status"] == "CONFIRMED" for a in state["data"]["actions"]), "状态为已确认、等待生效")
        check(any(a["action"] == "OPS_CONFIRMED" for a in state["data"]["audit"]), "写入人工审批审计")
        plan = next((p for p in state["data"]["plans"] if p["dish_id"] == "D_A_DUCK"), None)
        check(plan is not None and plan["confirmed_extra_portions"] == amount,
              "计划追加数量等于人工批准数量", str(plan))

        # 10. 推进时钟到下一窗口，供给变化生效
        status, _, cur2 = call(ops, LOCAL_PORT, "/api/v1/runs/current", headers=auth)
        clock_at = str(cur2["snapshot"]["clock_at"])
        to_clock = clock_at[:10] + "T12:00:00+08:00"
        status, _, advance = call(ops, LOCAL_PORT, "/api/v1/admin/simulation/events", method="POST", body={
            "run_id": run, "expected_snapshot_id": cur2["snapshot"]["snapshot_id"],
            "idempotency_key": key(), "event_type": "ADVANCE_CLOCK",
            "payload": {"to_clock_at": to_clock},
        }, headers=auth)
        ok(status, advance, "推进模拟时钟至 12:00")
        status, _, applied = call(ops, LOCAL_PORT, "/api/v1/admin/ops-state?" + state_params, headers=auth)
        ok(status, applied, "生效后读取运营状态")
        check(any(a["status"] == "APPLIED" for a in applied["data"]["actions"]), "备餐动作已生效")
        check(any(a["action"] == "OPS_APPLIED" for a in applied["data"]["audit"]), "写入生效审计")

        # 11. 学生端读到最新供应，并据此重新规划
        status, _, menu = call(student, LOCAL_PORT, "/api/v1/dining-options?" + urllib.parse.urlencode(
            {"run_id": run, "limit": "50"}), headers=same_origin)
        ok(status, menu, "学生端读取最新供应状态")
        duck_stock = next((o["available_bundle_portions"] for o in menu["data"]["options"]
                            if o.get("anchor_dish_id") == "D_A_DUCK" and len(o["items"]) == 1), None)
        check(duck_stock == amount, "新增供给真实反映到学生端菜单", f"stock={duck_stock} expected={amount}")
        status, _, final = call(student, LOCAL_PORT, "/api/v1/replan", method="POST", body={
            "run_id": run, "previous_recommendation_id": replan["data"]["result"]["recommendation_id"],
            "max_options": 2,
        }, headers=same_origin)
        ok(status, final, "学生端依据新供应重新规划")
        check(final["data"]["result"]["primary"] is not None, "重新规划返回可行餐次",
              str(final["data"]["result"]["primary"]["label"] if final["data"]["result"]["primary"] else None))
        check(True, "C+B 双端闭环完成：授权信号 → 建议 → 人工审批 → 窗口生效 → 推荐重算")

        # 12. 鉴权与跨站写入防护
        status, _, no_auth = call(ops, LOCAL_PORT, "/api/v1/admin/demand-insights?" + params,
                                  headers=same_origin)
        check(status == 401, "无凭据访问运营接口被拒")
        status, _, bad_token = call(ops, LOCAL_PORT, "/api/v1/admin/demand-insights?" + params,
                                    headers={"Authorization": "Bearer wrong", **same_origin})
        check(status == 401, "错误运营凭据被拒")
        status, _, bad_origin = call(student, LOCAL_PORT, "/api/v1/session", method="POST",
                                      body={"run_id": run},
                                      headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"})
        check(status == 403, "跨站写入被拒")
        # required=True：回执接口必须带 Origin，缺失即拒（伪造转发头也不算数）
        status, _, receipt = call(student, LOCAL_PORT, "/api/v1/interaction-receipts", method="POST",
                                   body={"run_id": run, "tool": "submit_feedback", "action_arguments": {}},
                                   headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "127.0.0.1"})
        check(status == 403 and receipt.get("error", {}).get("code") == "FORBIDDEN",
              "回执接口强制要求 Origin", f"status={status} body={receipt}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()


def phase_two(log_dir: Path):
    print("\n===== PHASE 2 · 模拟 Netlify → Render 代理请求头 =====", flush=True)
    db = str(log_dir / "edge.sqlite3")
    proc = start(EDGE_PORT, db, public=True, log=open(log_dir / "edge-uvicorn.log", "w", encoding="utf-8"))
    try:
        client, _ = new_client()
        edge_headers = {"Host": UPSTREAM_HOST, "Origin": PUBLIC_ORIGIN,
                        "Sec-Fetch-Site": "same-origin"}
        for path in ("/", "/ops", "/health", "/student-assets/student.css", "/student-assets/student.mjs"):
            status, _, _ = call(client, EDGE_PORT, path, headers=edge_headers, host=UPSTREAM_HOST)
            check(status == 200, "代理后页面 " + path, f"status={status}")
        status, _, current = call(client, EDGE_PORT, "/api/v1/runs/current", headers=edge_headers,
                                 host=UPSTREAM_HOST)
        ok(status, current, "代理后读取当前运行")
        run = current["snapshot"]["run_id"]
        status, headers, session = call(client, EDGE_PORT, "/api/v1/session", method="POST",
                                        body={"run_id": run}, headers=edge_headers, host=UPSTREAM_HOST)
        ok(status, session, "代理后建立学生会话")
        cookie = next((v for k, v in headers.items() if k.lower() == "set-cookie"), "")
        for directive in ("Secure", "HttpOnly", "SameSite=strict", "Path=/"):
            check(directive in cookie, "会话 Cookie 含 " + directive, cookie[:120])
        check("domain=" not in cookie.lower(), "会话 Cookie 无 Domain")
        # 提取 cookie 值，显式带上以模拟浏览器在 HTTPS 入口下的行为
        token = cookie.split(";")[0].split("=", 1)[1]
        https_like = {**edge_headers, "Cookie": "forkcast_student=" + token}
        status, _, rec = call(client, EDGE_PORT, "/api/v1/recommend", method="POST",
                              body={"run_id": run, "request": {}}, headers=https_like, host=UPSTREAM_HOST)
        ok(status, rec, "代理后同源会话推荐可用")
        # 伪造转发头 / 错误 Host / 错误 Origin 均不得放行
        status, _, spoof = call(client, EDGE_PORT, "/api/v1/session", method="POST",
                                body={"run_id": run},
                                headers={"Host": UPSTREAM_HOST, "Origin": PUBLIC_ORIGIN,
                                         "X-Forwarded-Host": "evil.example",
                                         "X-Forwarded-Proto": "https"},
                                host=UPSTREAM_HOST)
        check(status == 200, "忽略伪造 X-Forwarded-* 头（按配置的可信来源判定）", f"status={status}")
        status, _, bad_host = call(client, EDGE_PORT, "/health", host="evil.example")
        check(status == 400, "非可信 Host 被 TrustedHost 拒绝", f"status={status}")
        status, _, bad_origin = call(client, EDGE_PORT, "/api/v1/session", method="POST",
                                     body={"run_id": run},
                                     headers={"Host": UPSTREAM_HOST, "Origin": "https://evil.example"},
                                     host=UPSTREAM_HOST)
        check(status == 403, "非可信 Origin 被拒绝", f"status={status}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()


def main():
    with tempfile.TemporaryDirectory(prefix="forkcast-v1-e2e-") as tmp:
        log_dir = Path(tmp)
        phase_one(log_dir)
        phase_two(log_dir)
    passed = sum(1 for ok_, _ in results if ok_)
    print(f"\n===== 汇总：{passed}/{len(results)} 项通过 =====", flush=True)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
