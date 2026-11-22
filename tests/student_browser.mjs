// Windows browser acceptance using the built-in Node 24 WebSocket and Chromium DevTools.
import assert from "node:assert/strict";
import {spawn} from "node:child_process";
import {randomBytes} from "node:crypto";
import {access, mkdir, mkdtemp, realpath, rm, writeFile} from "node:fs/promises";
import {createServer} from "node:net";
import {basename, dirname, join, resolve} from "node:path";
import {setTimeout as delay} from "node:timers/promises";

const [browserPath, destination] = process.argv.slice(2);
if (!browserPath || !destination) throw new Error("Usage: node tests/student_browser.mjs <Chromium executable> <screenshot-directory>");
await access(browserPath);
const screenshots = resolve(destination);
await mkdir(screenshots, {recursive: true});
const tempRoot = await realpath(dirname(screenshots));
const scratch = await mkdtemp(join(tempRoot, "forkcast-m22-browser-"));
const ownedScratch = await realpath(scratch);
assert.equal(dirname(ownedScratch).toLowerCase(), tempRoot.toLowerCase());
assert.ok(basename(ownedScratch).startsWith("forkcast-m22-browser-"));

async function choosePort() {
  const local = createServer();
  return new Promise((done, reject) => {
    local.once("error", reject);
    local.listen(0, "127.0.0.1", () => {
      const port = local.address().port;
      local.close(error => error ? reject(error) : done(port));
    });
  });
}

async function waitFor(label, operation, timeout = 12000) {
  const stop = Date.now() + timeout;
  let cause;
  while (Date.now() < stop) {
    try {
      const result = await operation();
      if (result) return result;
    } catch (error) {
      cause = error;
    }
    await delay(100);
  }
  throw new Error("Timed out: " + label, {cause});
}

function cdpConnection(ws) {
  let sequence = 0;
  const pending = new Map();
  ws.addEventListener("message", event => {
    const reply = JSON.parse(event.data);
    if (pending.has(reply.id)) {
      const {done, reject} = pending.get(reply.id);
      pending.delete(reply.id);
      if (reply.error) reject(new Error(reply.error.message));
      else done(reply.result);
    }
  });
  return (method, params = {}) => {
    const id = ++sequence;
    return new Promise((done, reject) => {
      const timer = setTimeout(() => {
        pending.delete(id);
        reject(new Error("DevTools command timed out: " + method));
      }, 8000);
      pending.set(id, {
        done: result => {clearTimeout(timer); done(result);},
        reject: error => {clearTimeout(timer); reject(error);},
      });
      ws.send(JSON.stringify({id, method, params}));
    });
  };
}

const apiPort = await choosePort();
const devPort = await choosePort();
const host = "http://127.0.0.1:" + apiPort;
const token = randomBytes(30).toString("hex");
const server = spawn("python", ["-B", "-m", "forkcast", "--port", String(apiPort)], {
  windowsHide: true, env: {...process.env, FORKCAST_DATA_MODE: "SIMULATION",
    FORKCAST_DATABASE: join(scratch, "student.sqlite3"), FORKCAST_OPS_TOKEN: token},
});
let logs = "";
let browserLogs = "";
server.stderr.setEncoding("utf8").on("data", value => {logs += value;});
let chrome, socket, cdp;
try {
  const health = await waitFor("HTTP service start", async () => {
    const reply = await fetch(host + "/health");
    return reply.ok && await reply.json();
  });
  assert.equal(health.app_version, "0.3.0-m3.3");
  assert.equal(health.ready, true);
  assert.deepEqual(health.enabled_modes, ["SIMULATION"]);
  console.log("Windows HTTP PASS: Uvicorn reports ready on loopback, SIMULATION enabled.");
  chrome = spawn(browserPath, [
    "--headless=new", "--disable-gpu", "--disable-background-networking", "--no-first-run", "--no-default-browser-check",
    "--enable-logging=stderr", "--disable-crashpad-for-testing",
    "--remote-debugging-port=" + devPort, "--user-data-dir=" + join(scratch, "profile"), "about:blank",
  ], {windowsHide: true, stdio: ["ignore", "ignore", "pipe"],
    env: {...process.env, TEMP: ownedScratch, TMP: ownedScratch}});
  chrome.stderr.setEncoding("utf8").on("data", value => {browserLogs += value;});

  const page = await waitFor("Chromium DevTools page", async () => {
    const reply = await fetch("http://127.0.0.1:" + devPort + "/json/list");
    return (await reply.json()).find(item => item.type === "page");
  });
  socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((done, reject) => {
    socket.addEventListener("open", done, {once: true});
    socket.addEventListener("error", reject, {once: true});
  });
  cdp = cdpConnection(socket);
  await cdp("Page.enable");
  await cdp("Runtime.enable");
  await cdp("Network.enable");
  await cdp("Emulation.setDeviceMetricsOverride", {width: 1320, height: 1120, deviceScaleFactor: 1, mobile: false});
  await cdp("Page.navigate", {url: host + "/"});

  const js = async expression => {
    const output = await cdp("Runtime.evaluate", {expression, returnByValue: true, awaitPromise: true});
    if (output.exceptionDetails) throw new Error(output.exceptionDetails.exception?.description || output.exceptionDetails.text);
    return output.result.value;
  };
  const ready = (label, expression) => waitFor(label, () => js(expression));
  const tap = selector => js("document.querySelector(" + JSON.stringify(selector) + ").click()");
  const content = (selector, word) => ready(
    selector + " includes " + word,
    "document.querySelector(" + JSON.stringify(selector) + ")?.textContent.includes(" + JSON.stringify(word) + ")",
  );

  async function picture(file) {
    await js("document.fonts.ready.then(() => window.scrollTo(0, 0))");
    const {data} = await cdp("Page.captureScreenshot", {format: "png", captureBeyondViewport: false});
    await writeFile(join(screenshots, file), Buffer.from(data, "base64"));
  }
  async function current() {
    const response = await fetch(host + "/api/v1/runs/current");
    return (await response.json()).snapshot;
  }
  async function ops(path, payload) {
    const response = await fetch(host + "/api/v1/admin/simulation/" + path, {
      method: "POST", headers: {"Content-Type": "application/json", Authorization: "Bearer " + token},
      body: JSON.stringify({...payload, idempotency_key: randomBytes(14).toString("hex")}),
    });
    assert.equal(response.status, 200);
    return (await response.json()).data;
  }

  await ready("automatic default cards",
    'document.querySelector("[data-primary]")?.dataset.bundle === "B_A_DUCK" && document.querySelectorAll(".meal-card").length === 2');
  assert.equal(await js('document.querySelector("#share").checked'), false);
  await picture("FORKCAST_M2.2_01_today.png");
  console.log("D01 browser PASS: default duck, chicken alternative, sharing off, no sign-in.");

  await tap('[data-primary] .button.primary');
  await content("#intent", "烤鸭饭");
  const ref = await current();
  await ops("events", {run_id: ref.run_id, expected_snapshot_id: ref.snapshot_id, event_type: "SET_SOLD_OUT",
    payload: {dish_id: "D_A_DUCK"}});
  await ready("automatic sold-out replan", 'document.querySelector("[data-primary]")?.dataset.bundle === "B_B_CHICKEN"');
  await content("#changes", "烤鸭饭售罄");
  await content("#intent", "原意向：烤鸭饭");
  await content("#intent", "请确认是否另选");
  await picture("FORKCAST_M2.2_02_replan.png");
  await tap('[data-primary] .button.primary');
  await content("#intent", "原意向：鸡腿饭");
  await js('const select = document.querySelector("#meal-report"); select.selectedIndex = 1; select.dispatchEvent(new Event("change"))');
  await tap("#dined");
  await content("#feedback-status", "已记录实际用餐");
  console.log("D09 browser PASS: automatic replan, factual explanation, original intent retained, replacement explicit.");

  const ticket = await ops("demo-enrollments", {run_id: ref.run_id});
  await js('document.querySelector("#demo-form").closest("details").open = true;document.querySelector("#ticket").value='
           + JSON.stringify(ticket.demo_enrollment_token) + ';document.querySelector("#demo-form").requestSubmit()');
  await content("#session-info", "演示会话");
  await content("#privacy-status", "分享默认关闭");
  await tap("#share");
  await content("#privacy-status", "分享设置已开启");
  await tap('[data-primary] .button.primary');
  await content("#intent", "鸡腿饭");
  await tap("#share");
  await content("#privacy-status", "已撤回分享");
  await picture("FORKCAST_M2.2_04_sharing.png");
  await tap("#share");
  await content("#privacy-status", "分享设置已开启");
  console.log("S04 browser PASS: demo joined, shared and opted out; account remained usable.");

  await cdp("Network.emulateNetworkConditions", {offline: true, latency: 0, downloadThroughput: 0, uploadThroughput: 0});
  await content("#network", "旧数据");
  assert.equal(await js('document.querySelector("[data-primary] .button.primary").disabled'), true);
  await ops("reset", {scenario_id: "PREFERENCE_CONFLICT"});
  await cdp("Network.emulateNetworkConditions", {offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1});
  await content("#scene", "指名偏好与排队");
  await content("#session-info", "普通匿名试用");
  await ready("online again", 'document.querySelector("#network").hidden');
  assert.equal(await js('document.querySelector("#share").checked'), false);
  assert.equal(await js('document.querySelector("#intent").textContent'), "还没有选择。");
  console.log("A06 browser PASS: offline cache label, new run discovery, no old identity, sharing or intention.");

  await js('document.querySelector("#budget").value="20"; document.querySelector("#minutes").value="45";'
    + 'document.querySelector("[name=profile][value=FAST]").checked=true;'
    + 'document.querySelector("[name=taste][value=rice]").checked=true;'
    + 'document.querySelector("#wanted").value="D_A_DUCK"; document.querySelector("#needs-form").requestSubmit()');
  await content('[data-bundle="B_A_DUCK"]', "原意愿");
  await content('[data-bundle="B_A_DUCK"]', "预计排队较久");
  await picture("FORKCAST_M2.2_03_preference.png");
  await tap('[data-primary] .button.secondary');
  await content("#feedback-status", "已记录拒绝");
  await tap('[data-bundle="B_A_DUCK"] .button.primary');
  await content("#intent", "原意向：烤鸭饭");
  await tap("#clear-intent");
  await content("#feedback-status", "已清除意向");

  await js('window.originalForkcastFetch = window.fetch; window.waitedRequest = false; window.holdRequest = true;'
    + 'window.fetch = async (url, settings) => { const response = await window.originalForkcastFetch(url, settings);'
    + 'if (window.holdRequest && url === "/api/v1/recommend") { window.holdRequest=false; window.waitedRequest=true;'
    + 'await new Promise(resolve => {window.releaseForkcastReply = resolve;}); } return response; };'
    + 'document.querySelector("#minutes").value="1"; document.querySelector("#needs-form").requestSubmit()');
  await ready("old response delayed", "window.waitedRequest");
  await js('document.querySelector("#minutes").value="45"; document.querySelector("#needs-form").requestSubmit()');
  await content('[data-bundle="B_A_DUCK"]', "原意愿");
  await ready("new response accepted", 'document.querySelector("#loading").textContent === ""');
  await js("window.releaseForkcastReply(); window.fetch=window.originalForkcastFetch");
  await delay(350);
  assert.equal(await js('Boolean(document.querySelector("[data-primary]"))'), true);
  await js('document.querySelector("#minutes").value="1"; document.querySelector("#needs-form").requestSubmit()');
  await content("#cards", "当前没有可行餐次");
  await content("#cards", "预计总用餐时间超过可用时间");
  console.log("E02 and generation browser PASS: adjust budget/time/taste, inspect preference risk, reject, clear, late reply ignored, no-feasible reason visible.");
} catch (error) {
  console.error(logs);
  console.error(browserLogs);
  throw error;
} finally {
  if (cdp && socket?.readyState === WebSocket.OPEN) {
    const closed = new Promise(done => socket.addEventListener("close", done, {once: true}));
    cdp("Browser.close").catch(() => {});
    await Promise.race([closed, delay(1200)]);
    socket.close();
  }
  if (chrome && chrome.exitCode === null) chrome.kill();
  if (server.exitCode === null) server.kill();
  await delay(300);
  await rm(ownedScratch, {recursive: true, force: true, maxRetries: 3, retryDelay: 150}).catch(error => {
    console.warn("Temporary browser directory could not be cleaned:", error.code);
  });
}
