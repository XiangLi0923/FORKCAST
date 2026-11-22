import {DecisionGate} from "./decision_gate.mjs";
import {clearReceipt, showReceipt} from "./mcp-receipt.mjs";

const $ = id => document.getElementById(id);
const gate = new DecisionGate();
const studentOptionCount = 2;
const state = {
  session: null, epoch: 0, scenario: "", decision: null, intent: null, intentVersion: 0,
  previousCards: [], changes: [], request: {}, needsNew: false, busy: false, pendingWrite: null,
  offline: false, discover: true, polling: false, catalog: null, lastError: "", pending: false,
};

const scenarios = {CLOSED_LOOP: "校园模拟场景", TIME_PRESSURE: "午餐时间紧", PREFERENCE_CONFLICT: "指名偏好与排队"};
const reasons = {
  BUDGET_FIT: "符合预算上限", TIME_FIT: "预计可在限定时间内用餐",
  TASTE_MATCH: "符合本次口味或指名偏好", NEW_FAMILY: "能尝试没常吃的品类",
  LOWER_QUEUE: "在匹配度相近的餐次中排队更短", LONG_QUEUE: "预计排队较久",
  LOW_STOCK: "预计剩余份数较少", PRICE_UNKNOWN: "价格暂未核实",
  STALE_FACT: "部分事实已过有效期", VENUE_CLOSED: "场所当前不营业或来不及取餐",
  OUTSIDE_SERVICE_WINDOW: "已不在可取餐时段", HANDOFF_TIME_UNKNOWN: "取餐时间信息不足",
  OVER_BUDGET: "超过预算", OVER_TIME: "预计总用餐时间超过可用时间",
  RESTRICTED_INGREDIENT: "含已排除食材", RESTRICTED_ALLERGEN: "含已排除过敏原",
  RESTRICTION_UNVERIFIED: "无法核对所声明的饮食限制", SOLD_OUT: "售罄", UNAVAILABLE: "暂不供应",
  INSUFFICIENT_STOCK: "现有份数不足", SUPPLY_UNVERIFIED: "供应状态暂无法核实",
  REQUESTED_BUNDLE_NOT_REGISTERED: "指名菜品没有登记的完整餐次",
  QUEUE_CHANGED: "排队预估已更新", SUPPLY_RESTORED: "供应已恢复",
  SERVICE_WINDOW_CHANGED: "供餐时段已经切换", RANKING_CHANGED: "匹配排序已更新",
  RESTRICTIONS_NOT_DECLARED: "未声明饮食限制，可按需自行设置。",
  ORIGIN_ASSUMED: "步行时间以模拟默认起点估算。",
};
const names = {HIGH: "高", MEDIUM: "中", LOW: "低", UNKNOWN: "未知"};
const timeFormat = new Intl.DateTimeFormat("zh-CN", {
  hour: "2-digit", minute: "2-digit", timeZone: "Asia/Shanghai", hour12: false,
});

class ApiError extends Error {
  constructor(body, status) {
    super(body?.error?.message ?? "服务暂不可用。");
    this.code = body?.error?.code;
    this.snapshot = body?.snapshot;
    this.status = status;
  }
}

async function api(path, {method = "GET", payload} = {}) {
  const response = await fetch("/api/v1" + path, {
    method, credentials: "same-origin", cache: "no-store", signal: AbortSignal.timeout(7000),
    ...(payload ? {headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)} : {}),
  });
  const body = await response.json();
  if (!response.ok || !body.ok) throw new ApiError(body, response.status);
  return body;
}

function route(path, fields = {}) {
  return path + "?" + new URLSearchParams({run_id: gate.run, ...fields});
}

function node(tag, text = "", className = "") {
  const element = document.createElement(tag);
  element.textContent = text;
  if (className) element.className = className;
  return element;
}

function banner(id, text) {
  const area = $(id);
  area.textContent = text;
  area.hidden = !text;
}

function setNetwork(offline) {
  state.offline = offline;
  banner("network", offline ? "连接中断。当前显示的是上次取得的旧数据，恢复连接后会自动刷新。" : "");
  updateActions();
}

function updateActions() {
  const blocked = state.offline || state.busy || !state.session;
  for (const control of document.querySelectorAll("[data-action]")) {
    const cardPending = state.needsNew && control.hasAttribute("data-card-action");
    control.disabled = blocked || cardPending;
  }
  $("record-preference").disabled = blocked || !$("wanted").value;
  $("dined").disabled = blocked || !$("meal-report").value;
  $("not-dined").disabled = blocked || !($("meal-report").value || state.decision?.primary);
}

function catalogueNames(directory) {
  const dishes = new Map();
  for (const bundle of directory.options) {
    for (const item of bundle.evidence) {
      if (item.field === "name" && item.freshness === "FRESH") dishes.set(item.entity_id, item.value);
    }
  }
  const venues = new Map(directory.venues.map(row => [row.venue_id, row.name.value || row.venue_id]));
  return {dishes, venues, bundles: new Map(directory.options.map(row => [row.bundle_id, row]))};
}

function renderDirectory() {
  const select = $("wanted");
  const prior = select.value;
  select.replaceChildren(new Option("没有指定", ""));
  const dishes = [...state.catalog.names.dishes].sort((a, b) => a[1].localeCompare(b[1], "zh-CN"));
  for (const [id, label] of dishes) select.append(new Option(label, id));
  select.value = prior;
}

async function directory(snapshot) {
  let page = await api(route("/dining-options", {snapshot_id: snapshot.snapshot_id}));
  const data = {...page.data, options: [...page.data.options]};
  while (page.data.next_cursor) {
    page = await api(route("/dining-options", {cursor: page.data.next_cursor}));
    data.options.push(...page.data.options);
  }
  return {id: snapshot.snapshot_id, names: catalogueNames(data)};
}

function factsText(row) {
  const base = reasons[row.code];
  if (!base) return null;
  if (row.code === "SOLD_OUT" && row.value === "AVAILABLE") return null;
  const dish = state.catalog?.names.dishes.get(row.entity_id);
  return dish && ["SOLD_OUT", "UNAVAILABLE", "SUPPLY_RESTORED"].includes(row.code) ? dish + base : base;
}

function distinctMessages(rows) {
  return [...new Set(rows.map(factsText).filter(Boolean))];
}

function appendMessages(parent, rows, className) {
  const messages = distinctMessages(rows);
  if (messages.length) {
    const list = node("ul", "", className);
    for (const explanation of messages) list.append(node("li", explanation));
    parent.append(list);
  }
}

function visibleOptions(result) {
  const entries = [[result.primary, "首选"], ...result.alternatives.map(option => [option, "备选"])];
  if (result.requested_option) entries.push([result.requested_option, "原意愿"]);
  return entries.filter(([option]) => option);
}

function visibleCards(result) {
  const merged = new Map();
  for (const [option, label] of visibleOptions(result)) {
    if (merged.has(option.bundle_id)) merged.get(option.bundle_id).labels.push(label);
    else merged.set(option.bundle_id, {option, labels: [label]});
  }
  return [...merged.values()];
}

function timeRange(value) {
  return !value ? "未知" : value.min === value.max ? value.min + " 分钟" : value.min + "–" + value.max + " 分钟";
}

function actionButton(text, className, handle) {
  const button = node("button", text, "button " + className);
  button.type = "button";
  button.setAttribute("data-action", "");
  button.setAttribute("data-card-action", "");
  button.addEventListener("click", handle);
  return button;
}

function renderCard({option, labels}, rec) {
  const {dishes, venues, bundles} = state.catalog.names;
  const card = node("article", "", "meal-card" + (labels.includes("首选") ? " primary-card" : ""));
  card.dataset.bundle = option.bundle_id;
  if (labels.includes("首选")) card.dataset.primary = "true";
  const top = node("div", "", "card-top");
  top.append(node("span", labels.join(" · "), "category"),
    node("span", "匹配 " + (option.match_score / 10).toFixed(1) + " / 100 · 可信度 " + names[option.data_confidence], "score"));
  const where = venues.get(option.venue_id) || option.venue_id;
  const items = option.items.map(item => (dishes.get(item.dish_id) || item.dish_id) + " × " + item.portions + " 份");
  card.append(top, node("h3", option.label), node("p", where, "where"), node("p", items.join("、"), "items"));
  const grid = node("dl", "", "metrics");
  for (const [label, value] of [
    ["价格", option.price_cents === null ? "未知" : "¥" + (option.price_cents / 100).toFixed(2)],
    ["排队预估", timeRange(option.queue_min)], ["用餐总耗时", timeRange(option.total_time_min)],
    ["可用份数", option.available_bundle_portions === null ? "未知" : option.available_bundle_portions + " 份"],
  ]) {
    const group = node("div");
    group.append(node("dt", label), node("dd", value));
    grid.append(group);
  }
  card.append(grid);
  appendMessages(card, option.evidence, "reasons");
  appendMessages(card, option.warnings, "risks");
  const sources = [...option.evidence, ...option.warnings, ...(bundles.get(option.bundle_id)?.evidence || [])];
  const known = [...new Set(sources.filter(e => e.source_ref).map(e => {
    const asOf = e.as_of ? " · " + timeFormat.format(new Date(e.as_of)) : "";
    return e.source_ref + asOf;
  }))];
  const details = node("details");
  details.append(node("summary", "数据来源 · 模拟"), node("p", known.join("；") || "当前模拟快照", "sources"));
  card.append(details);
  const controls = node("div", "", "buttons");
  controls.append(
    actionButton("就吃这个", "primary", () => feedback("INTENT_SELECTED", {rec, option})),
    actionButton("不想吃这个", "secondary", () => feedback("REJECTED", {rec, option})),
    actionButton("授权 Agent 记录意向", "secondary", () => feedback("INTENT_SELECTED", {rec, option}, true)),
  );
  card.append(controls);
  return card;
}

function cardEntry(rec, option) {
  return {rec, option};
}

function renderMeals(current) {
  const select = $("meal-report");
  const old = select.value;
  const entries = [...(current ? visibleCards(current).map(({option}) => cardEntry(current.recommendation_id, option)) : []),
    ...state.previousCards];
  if (state.intent) entries.push(cardEntry(state.intent.rec, state.intent.option));
  const options = new Map(entries.filter(entry => entry.rec).map(entry => [entry.rec + ":" + entry.option.bundle_id, entry]));
  select.replaceChildren(new Option("选择实际吃过的餐次", ""));
  for (const [key, entry] of options) {
    const label = entry.option.label + (entry.rec === current?.recommendation_id ? "" : "（先前推荐）");
    select.append(new Option(label, key));
  }
  if (options.has(old)) select.value = old;
  return options;
}

let mealChoices = new Map();

function paint() {
  const result = state.decision;
  $("scene").textContent = scenarios[state.scenario] || "校园模拟场景";
  const list = $("cards");
  list.replaceChildren();
  if (result) {
    $("clock").textContent = timeFormat.format(new Date(result.snapshot.clock_at));
    $("revision").textContent = "模拟时钟 · 快照 " + result.snapshot.revision;
    if (result.primary) {
      for (const card of visibleCards(result)) list.append(renderCard(card, result.recommendation_id));
    } else {
      const empty = node("article", "", "panel");
      empty.append(node("h3", "当前没有可行餐次"), node("p", "可以调整预算、时间或其他非安全条件，再试一次。"));
      appendMessages(empty, result.conflicts, "risks");
      list.append(empty);
    }
    banner("global-warnings", distinctMessages(result.warnings).join(" "));
  } else {
    $("clock").textContent = "连接中";
    $("revision").textContent = "";
    banner("global-warnings", "");
  }
  const changes = distinctMessages(state.changes);
  banner("changes", changes.length ? changes.join("；") + "。" : "");
  banner("decision-errors", state.lastError);
  $("loading").textContent = state.pending ? "正在更新推荐…" : "";
  const sharing = state.session?.session_kind;
  $("session-info").textContent = sharing === "DEMO"
    ? "演示会话。只有主动授权后，新的运营信号才会计入。"
    : "普通匿名试用。即使打开分享，本会话也不会计入演示运营信号。";
  $("share").checked = state.session?.share_for_ops === true;
  const intent = $("intent");
  const needsReselect = state.intent?.status === "NEEDS_RESELECT";
  const intentIsOffered = result && visibleOptions(result)
    .some(([option]) => option.bundle_id === state.intent?.option.bundle_id);
  intent.classList.toggle("stale", needsReselect);
  intent.textContent = state.intent ? (
    "原意向：" + state.intent.option.label +
    (needsReselect
      ? (intentIsOffered ? "。当前可以重新选择这份餐次，请主动确认。" : "。请根据当前推荐确认是否另选。")
      : "。已记录打算去吃。")
  ) : "还没有选择。";
  $("clear-intent").hidden = !state.intent?.rec;
  mealChoices = renderMeals(result);
  updateActions();
}

function setFailure(error) {
  if (error.code === "RUN_REPLACED" || error.code === "UNAUTHENTICATED") {
    state.discover = true;
    state.lastError = "会话已变化，正在重新连接。";
  } else if (!(error instanceof ApiError) || error.status === 503) {
    state.discover = true;
    setNetwork(true);
    state.lastError = "";
  } else {
    state.lastError = error.code === "INVALID_INPUT" ? "请检查填写的条件。" : "暂时无法刷新推荐，请重试。";
  }
  paint();
}

function applyReplan(info, previous, capturedIntent) {
  if (capturedIntent !== state.intentVersion) return;
  if (!info.selected_bundle_id) {
    state.intent = null;
    return;
  }
  const option = state.intent?.option.bundle_id === info.selected_bundle_id ? state.intent.option
    : visibleCards(previous).find(row => row.option.bundle_id === info.selected_bundle_id)?.option
    || state.catalog.names.bundles.get(info.selected_bundle_id);
  if (!option) return;
  const rec = state.intent?.option.bundle_id === info.selected_bundle_id ? state.intent.rec
    : visibleCards(previous).some(row => row.option.bundle_id === info.selected_bundle_id) ? previous.recommendation_id : null;
  state.intent = {rec, option, status: info.selection_state};
}

async function compute() {
  const ticket = gate.start();
  const previous = state.decision;
  const isReplan = Boolean(previous) && !state.needsNew;
  const capturedIntent = state.intentVersion;
  state.pending = true;
  state.lastError = "";
  paint();
  try {
    const payload = isReplan ? {
      run_id: ticket.run, previous_recommendation_id: previous.recommendation_id, max_options: studentOptionCount,
    }
      : {run_id: ticket.run, request: state.request};
    const reply = await api(isReplan ? "/replan" : "/recommend", {method: "POST", payload});
    if (!gate.current(ticket)) return;
    const catalog = state.catalog?.id === reply.snapshot.snapshot_id ? state.catalog : await directory(reply.snapshot);
    if (!gate.accept(ticket, reply.snapshot)) {
      if (gate.current(ticket)) setTimeout(() => gate.current(ticket) && compute(), 80);
      return;
    }
    state.catalog = catalog;
    renderDirectory();
    if (previous) state.previousCards = visibleCards(previous).map(({option}) => cardEntry(previous.recommendation_id, option));
    state.changes = isReplan ? [...reply.data.change_reasons, ...reply.data.result.conflicts] : reply.data.conflicts;
    if (isReplan) applyReplan(reply.data, previous, capturedIntent);
    state.decision = isReplan ? reply.data.result : reply.data;
    state.needsNew = false;
    state.pending = false;
    setNetwork(false);
    paint();
  } catch (error) {
    if (gate.current(ticket)) {
      state.pending = false;
      setFailure(error);
    }
  }
}

async function startSession(discovered, demoToken) {
  clearReceipt();
  const snapshot = discovered.snapshot;
  const payload = {run_id: snapshot.run_id, ...(demoToken ? {demo_enrollment_token: demoToken} : {})};
  // Do not let a failed enrollment discard the active anonymous identity.
  const opened = await api("/session", {method: "POST", payload});
  gate.switchRun(opened.snapshot);
  state.epoch += 1;
  Object.assign(state, {
    session: opened.data, scenario: discovered.data.scenario_id, decision: null, intent: null,
    previousCards: [], changes: [], request: {}, needsNew: true, pendingWrite: null,
    catalog: null, discover: false, lastError: "",
  });
  $("needs-form").reset();
  $("ticket").value = "";
  $("feedback-status").textContent = "";
  $("privacy-status").textContent = "";
  await compute();
}

async function poll() {
  if (state.polling || state.busy || document.visibilityState === "hidden") return;
  if (!navigator.onLine) { setNetwork(true); return; }
  state.polling = true;
  try {
    const knownRun = gate.run;
    if (state.discover || state.offline || !state.session) {
      const latest = await api("/runs/current");
      if (gate.run !== knownRun) return;
      if (latest.snapshot.run_id !== gate.run || !state.session) {
        await startSession(latest);
        return;
      }
      if (state.discover && !state.offline) {
        state.session = (await api("/session", {method: "POST", payload: {run_id: gate.run}})).data;
      }
      state.discover = false;
    }
    const current = await api(route("/snapshot"));
    if (current.snapshot.run_id !== gate.run) return;
    gate.observe(current.snapshot);
    setNetwork(false);
    if (!state.pending && (state.needsNew || !state.decision || current.snapshot.revision > state.decision.snapshot.revision)) {
      await compute();
    }
  } catch (error) {
    setFailure(error);
  } finally {
    state.polling = false;
  }
}

function input() {
  const form = new FormData($("needs-form"));
  const budget = $("budget").value, minutes = $("minutes").value;
  const restrictions = {};
  const prefs = {};
  if (budget !== "") restrictions.budget_max_cents = Math.round(Number(budget) * 100);
  if (minutes !== "") restrictions.time_budget_min = Number(minutes);
  const taste = form.getAll("taste"), ingredients = form.getAll("ingredients"), allergens = form.getAll("allergens");
  if (ingredients.length) restrictions.avoid_ingredients = ingredients;
  if (allergens.length) restrictions.avoid_allergens = allergens;
  if (taste.length) prefs.taste_tags = taste;
  if (form.get("profile") !== "DEFAULT") prefs.profile = form.get("profile");
  if ($("wanted").value) prefs.requested_dish_id = $("wanted").value;
  return {...(Object.keys(restrictions).length ? {constraints: restrictions} : {}),
    ...(Object.keys(prefs).length ? {preferences: prefs} : {})};
}

async function write(path, fields, apply, statusId, method = "POST") {
  if (state.busy || state.offline || !state.session) return;
  const epoch = state.epoch;
  const payload = {run_id: gate.run, ...fields};
  const signature = path + JSON.stringify(payload);
  const previous = state.pendingWrite;
  const key = previous?.signature === signature ? previous.key : crypto.randomUUID();
  state.pendingWrite = {signature, key};
  state.busy = true;
  updateActions();
  try {
    const reply = await api(path, {method, payload: {...payload, idempotency_key: key}});
    if (epoch !== state.epoch) return;
    state.pendingWrite = null;
    apply(reply);
  } catch (error) {
    if (epoch !== state.epoch) return;
    if (error instanceof ApiError && error.status !== 503) state.pendingWrite = null;
    if (error.code === "STALE_SELECTION") {
      if (error.snapshot) gate.observe(error.snapshot);
      $(statusId).textContent = "旧卡已经不可选，原意向保留。正在更新推荐。";
      await compute();
    } else if (error.code === "SNAPSHOT_CONFLICT" && path.includes("consent")) {
      try {
        state.session = (await api("/session", {method: "POST", payload: {run_id: gate.run}})).data;
        $(statusId).textContent = "分享设置已在别处更新，请重新确认。";
      } catch (refreshError) {
        $(statusId).textContent = "暂时无法读取最新分享设置。";
        setFailure(refreshError);
      }
    } else {
      $(statusId).textContent = error.code === "RUN_REPLACED" ? "运行已重置，正在重连。"
        : !(error instanceof ApiError) ? "提交结果未确认。恢复网络后可再次执行同一操作。" : "操作未完成，请重新确认。";
      if (error.code === "RUN_REPLACED" || error.code === "UNAUTHENTICATED" ||
          !(error instanceof ApiError) || error.status === 503) setFailure(error);
    }
  } finally {
    state.busy = false;
    paint();
  }
}

function feedback(event, entry, delegate = false) {
  const fields = {event_type: event};
  if (event === "PREFERENCE") fields.dish_id = $("wanted").value;
  else if (entry?.rec) fields.recommendation_id = entry.rec;
  if (["DINED", "INTENT_SELECTED", "REJECTED"].includes(event) && entry?.option) fields.bundle_id = entry.option.bundle_id;
  if (delegate) return grantFeedback(fields);
  return write("/feedback", fields, () => {
    if (event === "INTENT_SELECTED") {
      state.intentVersion += 1;
      state.intent = {...entry, status: "FEASIBLE"};
    }
    if (event === "INTENT_CLEARED") {
      state.intentVersion += 1;
      state.intent = null;
    }
    const messages = {
      INTENT_SELECTED: "已记录打算去吃。", INTENT_CLEARED: "已清除意向。", DINED: "已记录实际用餐。",
      NOT_DINED: "已记录没有吃。", REJECTED: "已记录拒绝。你可以选择别的餐次或调整需求。",
      PREFERENCE: "已记录想吃的菜。",
    };
    $("feedback-status").textContent = messages[event];
  }, "feedback-status");
}

async function grantFeedback(fields) {
  if (state.offline || state.busy || !state.session) return;
  const epoch = state.epoch;
  const args = {run_id: gate.run, ...fields, idempotency_key: crypto.randomUUID()};
  clearReceipt();
  state.busy = true;
  updateActions();
  try {
    const reply = await api("/interaction-receipts", {method: "POST", payload: {
      run_id: args.run_id, tool: "submit_feedback", action_arguments: args,
    }});
    if (epoch === state.epoch) showReceipt(args, reply.data);
  } catch (error) {
    if (epoch === state.epoch) $("feedback-status").textContent = "授权未签发，请检查当前会话并重试。";
  } finally {
    state.busy = false;
    updateActions();
  }
}
$("needs-form").addEventListener("submit", event => {
  event.preventDefault();
  if (state.offline || !state.session) return;
  state.request = input();
  state.needsNew = true;
  gate.changeConditions();
  compute();
});
$("wanted").addEventListener("change", updateActions);
$("record-preference").addEventListener("click", () => feedback("PREFERENCE"));
$("clear-intent").addEventListener("click", () => feedback("INTENT_CLEARED", state.intent));
$("meal-report").addEventListener("change", updateActions);
$("dined").addEventListener("click", () => {
  const choice = mealChoices.get($("meal-report").value);
  if (choice) feedback("DINED", choice);
});
$("not-dined").addEventListener("click", () => {
  const choice = mealChoices.get($("meal-report").value);
  feedback("NOT_DINED", choice || state.intent || {rec: state.decision?.recommendation_id});
});
$("share").addEventListener("change", () => {
  const desired = $("share").checked;
  write("/consent/ops-sharing", {share_for_ops: desired, expected_consent_revision: state.session.consent_revision}, reply => {
    Object.assign(state.session, {share_for_ops: reply.data.share_for_ops, consent_revision: reply.data.consent_revision});
    $("privacy-status").textContent = desired ? "分享设置已开启。"
      : "已撤回分享。原有贡献不会计入后续运营分析。";
  }, "privacy-status", "PUT");
});
$("demo-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (state.busy || state.offline) return;
  state.busy = true;
  updateActions();
  try {
    const latest = await api("/runs/current");
    await startSession(latest, $("ticket").value.trim());
    $("privacy-status").textContent = "已加入演示，分享默认关闭。";
    $("demo-form").closest("details").open = false;
  } catch (error) {
    $("privacy-status").textContent = error.code === "FORBIDDEN" ? "演示凭据无效、已用或过期。" : "暂时无法加入演示。";
  } finally {
    state.busy = false;
    paint();
  }
});
window.addEventListener("offline", () => {state.discover = true; setNetwork(true);});
window.addEventListener("online", () => {state.discover = true; poll();});
document.addEventListener("visibilitychange", () => {if (!document.hidden) poll();});
updateActions();
poll();
setInterval(poll, 2000);
