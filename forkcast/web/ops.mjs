import {clearReceipt, showReceipt} from "./mcp-receipt.mjs";

const $ = id => document.getElementById(id);
const localTime = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
});
const state = {token: "", snapshot: null, demand: null, report: null, execution: null, history: "", busy: false, names: new Map()};
const approvalKeys = new Map();
const reason = {
  DEMAND_FROM_PREFERENCE: "需求代理来自偏好", DEMAND_FROM_INTENT: "需求代理来自意向",
  DEMAND_FROM_SELF_REPORT: "需求代理来自用餐自报", INSUFFICIENT_SIGNAL: "可见需求不足",
  DISH_CAPACITY_LIMIT: "达到菜品供应上限", PREP_CAPACITY_LIMIT: "食堂产能限制", INGREDIENT_LIMIT: "原料限制",
};
const messages = {
  UNAUTHENTICATED: "运营凭据无效，请重新连接。", FORBIDDEN: "当前凭据无权执行此操作。",
  SNAPSHOT_CONFLICT: "供应已更新，请刷新后重新生成建议。", STALE_PROPOSAL: "提案已失效，请重新生成。",
  OPS_ALREADY_CONFIRMED: "此提案已确认，最新结果可在运营状态查看。",
  NOT_FOUND: "当前运行中未找到该记录。", INVALID_INPUT: "请求与当前窗口或允许的数量不匹配。",
};

function element(tag, value, classes = "") {
  const dom = document.createElement(tag);
  if (value !== undefined) dom.textContent = String(value ?? "—");
  if (classes) dom.className = classes;
  return dom;
}

function notice(text) {
  $("notice").textContent = text;
  $("notice").hidden = !text;
}

function markConnection(stale) {
  $("connection").hidden = !stale || !state.snapshot;
  $("connection").textContent = stale ? "暂时无法更新。当前显示上次读取的数据，恢复连接后可继续操作。" : "";
  document.querySelectorAll("[data-live]").forEach(button => { button.disabled = stale; });
  $("generate").disabled = stale || $("clock").textContent >= "12:00:00";
}

async function api(path, {query, method = "GET", body} = {}) {
  const address = "/api/v1/" + path + (query ? "?" + new URLSearchParams(query) : "");
  const response = await fetch(address, {
    method, cache: "no-store", signal: AbortSignal.timeout(5000), headers: {
      Authorization: "Bearer " + state.token,
      ...(body ? {"Content-Type": "application/json"} : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  const reply = await response.json();
  if (!response.ok || !reply.ok) {
    const error = new Error(messages[reply.error?.code] || "操作未完成，请检查连接及输入后再试。");
    error.code = reply.error?.code;
    throw error;
  }
  return reply;
}

const runFields = () => ({run_id: state.snapshot.run_id});
const windowId = label => state.snapshot.run_id + "." + label;
const timestamp = value => value ? localTime.format(new Date(value)) : "—";
const label = dish => state.names.get(dish) || dish;
const evidence = code => reason[code] || code;

function table(headings, rows) {
  const dom = element("table");
  const head = element("thead"), heading = element("tr");
  for (const value of headings) heading.append(element("th", value));
  head.append(heading);
  dom.append(head);
  const body = element("tbody");
  for (const values of rows) {
    const row = element("tr");
    for (const value of values) row.append(element("td", value));
    body.append(row);
  }
  dom.append(body);
  return dom;
}

function drawDemand() {
  $("signals").textContent = "授权信号 · 第 " + state.demand.data.signals_revision + " 版";
  const report = state.report?.data;
  const rows = state.demand.data.rows;
  const signal = count => count === null ? "未披露" : count;
  const shown = rows.filter(row => row.preferred_sessions !== null || row.intended_sessions !== null
    || row.dined_sessions !== null);
  const hidden = rows.filter(row => !shown.includes(row));
  const buildTable = list => table(["菜品", "想吃", "打算去吃", "自报吃过"],
    list.map(row => [label(row.dish_id), signal(row.preferred_sessions),
      signal(row.intended_sessions), signal(row.dined_sessions)]));
  const signalArea = $("demand-rows");
  // 先展示有信号的菜品，把「未达披露门槛」的行折叠起来，避免运营者被整张表淹没。
  signalArea.replaceChildren(shown.length ? buildTable(shown)
    : element("p", "这个窗口还没有达到披露门槛的授权信号。", "quiet"));
  if (hidden.length) {
    const fold = element("details");
    fold.append(element("summary", "查看其余 " + hidden.length + " 道菜（信号不足，计数未披露）"),
      buildTable(hidden));
    signalArea.append(fold);
  }
  const area = $("supply-rows");
  area.replaceChildren();
  const analyses = [...(report?.analyses || [])].sort((a, b) =>
    (b.shortage_portions ?? -1) - (a.shortage_portions ?? -1));
  for (const row of analyses) {
    const line = element("article", undefined, "supply-line");
    line.append(element("h4", label(row.dish_id)), element("p",
      "系统估计需求 " + (row.demand_proxy_portions ?? "未披露") + " · 计划供应 " + row.planned_supply_portions
      + " · 缺口 " + (row.shortage_portions ?? "未知")));
    if (row.shortage_portions > 0) {
      const meter = element("progress");
      meter.max = row.shortage_portions;
      meter.value = row.feasible_extra_portions;
      meter.setAttribute("aria-label", "可追加份数占缺口的比例");
      line.append(meter);
    }
    line.append(element("p", "可追加 " + (row.feasible_extra_portions ?? "未知")
      + " · 未覆盖 " + (row.uncovered_portions ?? "未知") + " · 原料复核 "
      + (row.ingredient_review_portions ?? "未知"), "quiet"));
    line.append(element("p", row.evidence_codes.map(code => reason[code] || code).join(" · "), "quiet"));
    area.append(line);
  }
  if (!analyses.length) area.append(element("p", report ? "当前没有可分析的菜品。" : "点击生成运营建议，核对下一窗口的缺口、原料和追加产能。", "quiet"));
  const peak = analyses.find(row => Number.isFinite(row.shortage_portions));
  $("top-shortage").textContent = peak ? peak.shortage_portions + " 份" : report ? "未披露" : "待分析";
  $("focus-dish").textContent = peak?.shortage_portions > 0 ? label(peak.dish_id) : "";
  $("pending-count").textContent = report ? String(report.proposals.length) : "待分析";
  // fixture v03.2 的窗口为 W1 11:30–12:00、W2 12:00–12:30；运行摘要只提供模拟时钟，
  // 不含内部窗口 ID，因此按校园时间标注，避免读取不存在的字段。
  const hhmm = String(state.snapshot.clock_at).slice(11, 16);
  $("active-window").textContent = hhmm >= "12:30" ? "已收餐"
    : hhmm >= "12:00" ? "正段窗口" : "前段窗口";
  $("active-window-time").textContent = hhmm >= "12:30" ? "供餐已结束"
    : hhmm >= "12:00" ? "12:00–12:30" : "11:30–12:00";
}

function drawProposals() {
  const cards = [];
  for (const proposal of state.report?.data.proposals || []) {
    const card = element("article", undefined, "panel ops-card");
    card.dataset.proposal = proposal.proposal_id;
    card.append(element("h3", label(proposal.dish_id) + " · 待审批"));
    card.append(element("p", "估计需求 " + proposal.demand_proxy_portions + " · 计划 " + proposal.planned_supply_portions
      + " · 缺口 " + proposal.shortage_portions + " · 建议追加 " + proposal.proposed_extra_portions
      + " 份 · 目标窗口 " + timestamp(proposal.expected_effective_at), "quiet"));
    card.append(element("p", proposal.evidence_codes.map(evidence).join(" · "), "quiet"));
    const form = element("form", undefined, "compact-form");
    const field = element("label", "批准份数");
    const input = element("input");
    input.type = "number";
    input.id = "amount-" + proposal.proposal_id;
    field.htmlFor = input.id;
    input.required = true;
    input.min = "1";
    input.max = String(proposal.proposed_extra_portions);
    input.step = "1";
    input.placeholder = "1–" + proposal.proposed_extra_portions;
    field.append(input);
    const confirm = element("button", "确认备餐", "button primary");
    const delegate = element("button", "授权本地 Agent", "button secondary");
    confirm.dataset.live = "";
    delegate.dataset.live = "";
    form.append(field, confirm, delegate);
    form.addEventListener("submit", event => {
      event.preventDefault();
      act(async () => {
        const amount = Number(input.value);
        const operation = proposal.proposal_id + ":" + amount;
        if (!approvalKeys.has(operation)) approvalKeys.set(operation, crypto.randomUUID());
        const args = {
          ...runFields(), proposal_id: proposal.proposal_id, approved_extra_portions: amount,
          expected_snapshot_id: proposal.based_on_snapshot_id, idempotency_key: approvalKeys.get(operation),
        };
        if (event.submitter === delegate) {
          clearReceipt();
          const reply = await api("interaction-receipts", {method: "POST", body: {
            ...runFields(), tool: "confirm_ops_action", action_arguments: args,
          }});
          if (args.run_id === state.snapshot?.run_id) showReceipt(args, reply.data);
          return;
        }
        await api("admin/ops-confirmations", {method: "POST", body: args});
        await refresh("");
        notice("已记录人工批准，可在下方供给执行与审计中查看状态。");
      });
    });
    card.append(form);
    cards.push(card);
  }
  if (!cards.length) cards.push(element("p", "暂无待审批建议。点击「生成运营建议」，核对缺口后再决定批准份数。", "quiet"));
  $("proposal-list").replaceChildren(...cards);
}

function drawExecution(menu) {
  const {data, snapshot} = state.execution;
  $("applied-count").textContent = state.history ? "历史视图" : String(data.actions.filter(row => row.status === "APPLIED").length);
  $("execution-version").textContent = (state.history ? "历史视图 · " : "当前视图 · ")
    + "第 " + snapshot.revision + " 版 · " + timestamp(snapshot.clock_at);
  $("action-list").replaceChildren(...data.actions.map(action => {
    const card = element("article", undefined, "panel ops-card");
    card.dataset.status = action.status;
    const confirmed = data.audit.find(row => row.proposal_id === action.proposal_id && row.action === "OPS_CONFIRMED");
    const applied = action.status === "APPLIED";
    card.append(element("h3", label(action.dish_id) + (applied ? " · 已生效" : " · 已确认，等待生效")));
    card.append(element("p", applied ? "已生效 " + action.approved_extra_portions + " 份"
      : "已确认，等待目标供餐窗口生效。批准 " + action.approved_extra_portions + " 份", "intent"));
    card.append(element("p", "确认时间 " + timestamp(confirmed?.at) + " · 确认快照 " + action.confirmation_snapshot_id, "quiet"));
    const count = action.approved_extra_portions;
    const detail = applied
      ? "生效时间 " + timestamp(action.applied_at) + " · 生效快照 " + action.application_snapshot_id
        + " · 已消耗原料 " + count + " 份 · 已释放产能预留 " + count + " 份"
      : "本动作已预留原料 " + count + " 份和备餐产能 " + count + " 份。";
    card.append(element("p", detail, "quiet"));
    return card;
  }));
  if (!data.actions.length) $("action-list").append(element("p", "尚无确认或生效记录。", "quiet"));
  const audit = element("ul", undefined, "ops-audit");
  const auditNames = {OPS_CONFIRMED: "运营确认备餐", OPS_APPLIED: "供给变更生效"};
  for (const row of data.audit) {
    const line = element("li", (auditNames[row.action] || row.action) + " · "
      + timestamp(row.at) + " · 快照 " + row.snapshot_id);
    line.title = row.action + " / " + row.result_code;
    audit.append(line);
  }
  $("audit-list").replaceChildren(audit);
  const stock = new Map(menu.data.options.filter(row => row.items.length === 1)
    .map(row => [row.anchor_dish_id, row.available_bundle_portions]));
  $("plan-list").replaceChildren(table(["菜品", "计划供应", "已批准追加", "供应上限", "该快照现货"],
    data.plans.map(row => [label(row.dish_id), row.base_planned_portions, row.confirmed_extra_portions,
      row.max_served_portions, stock.get(row.dish_id)])));
}

async function refresh(requestedHistory = state.history) {
  let snapshot, switched, history, execution, demand, menu;
  try {
    ({snapshot} = await api("runs/current"));
    const run = {run_id: snapshot.run_id};
    switched = state.snapshot?.run_id !== snapshot.run_id;
    history = switched ? "" : requestedHistory;
    [execution, demand, menu] = await Promise.all([
      api("admin/ops-state", {query: {...run, target_window_id: snapshot.run_id + ".W2",
        ...(history ? {snapshot_id: history} : {})}}),
      api("admin/demand-insights", {query: {...run, reference_window_id: snapshot.run_id + ".W1"}}),
      api("dining-options", {query: {...run, snapshot_id: history || snapshot.snapshot_id, limit: "50"}}),
    ]);
    if (demand.snapshot.snapshot_id !== snapshot.snapshot_id
        || execution.snapshot.snapshot_id !== menu.snapshot.snapshot_id) {
      throw new Error("刷新期间供应发生变化，请再次刷新。");
    }
  } catch (error) {
    if (switched || error.code !== "NOT_FOUND" || requestedHistory === state.history) markConnection(true);
    throw error;
  }
  if (switched) {
    clearReceipt();
    state.report = null;
    approvalKeys.clear();
    state.names.clear();
    $("demo-ticket").value = "";
    $("ticket-label").hidden = $("copy-ticket").hidden = true;
    drawProposals();
  }
  if (switched || state.history !== history) $("history-snapshot").value = history;
  Object.assign(state, {snapshot, history, demand, execution});
  const selected = $("sold-dish").value;
  const options = new Map();
  for (const bundle of menu.data.options) {
    if (!options.has(bundle.anchor_dish_id) || bundle.items.length === 1) {
      options.set(bundle.anchor_dish_id, bundle.label);
      state.names.set(bundle.anchor_dish_id, bundle.label);
    }
  }
  $("sold-dish").replaceChildren(...Array.from(options, ([dish, name]) => new Option(name, dish)));
  if (options.has(selected)) $("sold-dish").value = selected;
  else if (options.has("D_A_DUCK")) $("sold-dish").value = "D_A_DUCK";
  $("run").textContent = state.snapshot.run_id;
  $("clock").textContent = timestamp(state.snapshot.clock_at);
  $("snapshot").textContent = "供应快照 " + state.snapshot.revision;
  if (state.report && (state.report.snapshot.snapshot_id !== state.snapshot.snapshot_id
      || state.report.data.signals_revision !== demand.data.signals_revision)) {
    state.report = null;
    drawProposals();
  }
  drawDemand();
  drawExecution(menu);
  markConnection(false);
}

async function act(operation, quiet = false) {
  if (state.busy || !state.token) return;
  state.busy = true;
  if (!quiet) notice("");
  try {
    await operation();
  } catch (error) {
    if (error.code === "UNAUTHENTICATED") {
      clearReceipt();
      state.token = "";
      $("dashboard").hidden = true;
      $("authenticate").hidden = false;
      $("connection").hidden = true;
    }
    if (!quiet || error.code === "UNAUTHENTICATED") {
      notice(error instanceof TypeError || error.name === "TimeoutError" ? "连接中断，请刷新后重试。" : error.message);
    }
  } finally {
    state.busy = false;
  }
}

$("authenticate").addEventListener("submit", event => {
  event.preventDefault();
  state.token = $("ops-token").value;
  $("ops-token").value = "";
  act(async () => {
    await refresh();
    $("authenticate").hidden = true;
    $("dashboard").hidden = false;
  });
});

$("refresh").addEventListener("click", () => act(refresh));
$("history-form").addEventListener("submit", event => {
  event.preventDefault();
  act(() => refresh($("history-snapshot").value.trim()));
});
$("generate").addEventListener("click", () => act(async () => {
  const report = await api("admin/ops-proposals", {method: "POST", body: {
    ...runFields(), reference_window_id: windowId("W1"), target_window_id: windowId("W2"),
  }});
  await refresh();
  if (report.snapshot.snapshot_id !== state.snapshot.snapshot_id
      || report.data.signals_revision !== state.demand.data.signals_revision) {
    throw new Error("生成期间需求或供应已变化，请重新生成。");
  }
  state.report = report;
  drawDemand();
  drawProposals();
}));

async function simulate(event_type, payload) {
  await api("admin/simulation/events", {method: "POST", body: {
    ...runFields(), expected_snapshot_id: state.snapshot.snapshot_id,
    idempotency_key: crypto.randomUUID(), event_type, payload,
  }});
  await refresh();
}

$("clock-form").addEventListener("submit", event => {
  event.preventDefault();
  act(() => simulate("ADVANCE_CLOCK", {
    to_clock_at: state.snapshot.clock_at.slice(0, 10) + "T" + $("advance-clock").value + ":00+08:00",
  }));
});
$("sold-form").addEventListener("submit", event => {
  event.preventDefault();
  act(() => simulate("SET_SOLD_OUT", {dish_id: $("sold-dish").value}));
});
$("reset").addEventListener("click", () => act(async () => {
  if (!window.confirm("开始新的模拟运行？当前学生会话及演示凭据将失效。")) return;
  await api("admin/simulation/reset", {method: "POST", body: {
    scenario_id: "CLOSED_LOOP", idempotency_key: crypto.randomUUID(),
  }});
  await refresh();
  drawProposals();
  notice("已建立新模拟运行。请重新发放学生演示凭据。");
}));
$("issue-ticket").addEventListener("click", () => act(async () => {
  const reply = await api("admin/simulation/demo-enrollments", {method: "POST", body: {
    ...runFields(), idempotency_key: crypto.randomUUID(),
  }});
  $("demo-ticket").value = reply.data.demo_enrollment_token;
  $("ticket-label").hidden = $("copy-ticket").hidden = false;
}));
$("copy-ticket").addEventListener("click", () => act(async () => {
  await navigator.clipboard.writeText($("demo-ticket").value);
  notice("已复制演示凭据。");
}));
setInterval(() => {
  if (!document.hidden && !$("dashboard").hidden) act(refresh, true);
}, 2500);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && !$("dashboard").hidden) act(refresh, true);
});
