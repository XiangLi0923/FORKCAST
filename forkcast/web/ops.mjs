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
  UNAUTHENTICATED: "OPS 凭据无效，请重新连接。", FORBIDDEN: "当前凭据无权执行此操作。",
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
const evidence = code => (reason[code] || code) + " (" + code + ")";

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
  $("signals").textContent = "信号版本 " + state.demand.data.signals_revision;
  const analysis = new Map((state.report?.data.analyses || []).map(row => [row.dish_id, row]));
  $("demand-rows").replaceChildren(table(
    ["菜品", "P", "I", "A", "代理", "计划", "缺口", "可追加", "未覆盖", "原料复核", "依据"],
    state.demand.data.rows.map(row => {
      const a = analysis.get(row.dish_id);
      return [label(row.dish_id), row.preferred_sessions, row.intended_sessions, row.dined_sessions,
        a?.demand_proxy_portions ?? null, a?.planned_supply_portions ?? null, a?.shortage_portions ?? null,
        a?.feasible_extra_portions ?? null, a?.uncovered_portions ?? null, a?.ingredient_review_portions ?? null,
        a?.evidence_codes.map(evidence).join("、") ?? null];
    }),
  ));
}

function drawProposals() {
  const cards = [];
  for (const proposal of state.report?.data.proposals || []) {
    const card = element("article", undefined, "panel ops-card");
    card.dataset.proposal = proposal.proposal_id;
    card.append(element("h3", label(proposal.dish_id) + " · PROPOSED"));
    card.append(element("p", "代理 " + proposal.demand_proxy_portions + " · 计划 " + proposal.planned_supply_portions
      + " · 缺口 " + proposal.shortage_portions + " · 建议追加 " + proposal.proposed_extra_portions
      + " 份 · 预计 " + timestamp(proposal.expected_effective_at), "quiet"));
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
        notice("确认已记录，可在运营状态中查看。");
      });
    });
    card.append(form);
    cards.push(card);
  }
  if (!cards.length) cards.push(element("p", "当前无待确认提案。可在当前窗口生成最新建议。", "quiet"));
  $("proposal-list").replaceChildren(...cards);
}

function drawExecution(menu) {
  const {data, snapshot} = state.execution;
  $("execution-version").textContent = (state.history ? "历史快照 " : "最新快照 ")
    + snapshot.revision + " · " + timestamp(snapshot.clock_at);
  $("action-list").replaceChildren(...data.actions.map(action => {
    const card = element("article", undefined, "panel ops-card");
    card.dataset.status = action.status;
    const confirmed = data.audit.find(row => row.proposal_id === action.proposal_id && row.action === "OPS_CONFIRMED");
    const applied = action.status === "APPLIED";
    card.append(element("h3", label(action.dish_id) + " · " + action.status));
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
  for (const row of data.audit) audit.append(element("li",
    row.action + " · " + timestamp(row.at) + " · " + row.snapshot_id + " · " + row.result_code));
  $("audit-list").replaceChildren(audit);
  const stock = new Map(menu.data.options.filter(row => row.items.length === 1)
    .map(row => [row.anchor_dish_id, row.available_bundle_portions]));
  $("plan-list").replaceChildren(table(["菜品", "W2 基线", "W2 批准追加", "W2 上限", "该快照的现货"],
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
