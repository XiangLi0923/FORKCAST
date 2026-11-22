const get = id => document.getElementById(id);
const labels = {
  taste: "口味", time: "时间", price: "价格", exploration: "尝新", nutrition: "营养参考",
  OVER_BUDGET: "超预算", OVER_TIME: "超过可用时间", SOLD_OUT: "售罄",
  UNAVAILABLE: "当前不供应", INSUFFICIENT_STOCK: "库存不足", VENUE_CLOSED: "食堂关闭",
  OUTSIDE_SERVICE_WINDOW: "来不及取餐", HANDOFF_TIME_UNKNOWN: "取餐时间未知",
  SUPPLY_UNVERIFIED: "供应待核实", PRICE_UNKNOWN: "价格未知",
  RESTRICTED_INGREDIENT: "包含限制食材", RESTRICTED_ALLERGEN: "包含限制过敏原",
  RESTRICTION_UNVERIFIED: "忌口证据不足",
};
const presets = {
  duck: {budget: 20, minutes: 45, dish: "D_A_DUCK", profile: "DEFAULT"},
  budget: {budget: 15, minutes: 45, dish: "D_A_DUCK", profile: "VALUE"},
  fast: {budget: 20, minutes: 20, dish: "", profile: "FAST"},
  explore: {budget: 20, minutes: 45, dish: "", profile: "EXPLORE"},
};
const money = cents => cents == null ? "未知" : "¥" + (cents / 100).toFixed(2);
const minutes = range => range ? range.min + "–" + range.max + " 分钟" : "未知";
let pending;
let fullTrace = [];
let chosen = null;
let opsPending;
let guideStep = -1;
const guide = [
  {title: "选一份午餐", text: "试试烤鸭饭，点卡片上的「就选这份」。想选其他餐次也可以。", target: "#results .choose-meal"},
  {title: "切到食堂视角", text: "共享由你决定。点「梳理需求数据」，查看想吃、打算去吃和实际吃过三类信号。", target: "#summarize"},
  {title: "得出备餐方案", text: "点「生成备餐方案」，看看缺口、原料和产能如何决定可追加份数。", target: "#generate-plan"},
  {title: "完成双端体验", text: "已选好一餐，也拿到了备餐方案。还可以模拟批准，查看下一窗的供给与推荐。", target: "#show-approved"},
];
const stageValue = () => document.querySelector('[name="stage"]:checked').value;
const stageExplanation = {
  CURRENT: "当前供餐窗口",
  W2_BASE: "下一供餐窗口 · 未批准",
  W2_APPROVED: "下一供餐窗口 · 已批准",
};

function node(tag, value) {
  const el = document.createElement(tag);
  if (value !== undefined) el.textContent = value;
  return el;
}

function card(candidate, rank) {
  const el = node("article");
  el.className = "demo-card";
  if (rank === "首选") el.classList.add("featured-card");
  el.append(node("small", rank), node("h3", candidate.name),
    node("p", candidate.venue + " · " + money(candidate.price_cents)),
    node("p", "排队 " + minutes(candidate.queue) + " · 全程 " + minutes(candidate.time)),
    node("strong", candidate.score / 10 + " / 100"));
  const details = node("details");
  const list = node("ul");
  for (const factor of candidate.factors) {
    list.append(node("li", labels[factor.name] + " " + factor.points + " / " + factor.weight
      + (factor.estimated ? "（缺少偏好或事实，按中性值）" : "")));
  }
  details.append(node("summary", "查看分项"), list);
  const choose = node("button", chosen?.id === candidate.id ? "已选这份" : "就选这份");
  choose.type = "button";
  choose.className = "choose-meal";
  choose.dataset.meal = candidate.id;
  choose.setAttribute("aria-pressed", String(chosen?.id === candidate.id));
  choose.addEventListener("click", () => chooseMeal(candidate));
  el.append(choose, details);
  return el;
}

function show(data) {
  const {candidates, result, supply_comparison: supply} = data;
  get("supply-detail").textContent = "烤鸭饭的模拟信号：想吃 " + supply.preference + "、打算去吃 "
    + supply.intent + "、自报吃过 " + supply.reported_meal + "。建议追加 " + supply.proposal_portions
     + " 份；当前所选快照中可售 " + supply.current_duck_stock + " 份。首选："
    + (result.primary?.label ?? "当前无解") + "。";
  const eligible = candidates.filter(row => row.score !== null);
  const byId = new Map(candidates.map(row => [row.id, row]));
  const select = get("dish");
  const known = new Set(Array.from(select.options, option => option.value));
  for (const row of candidates) {
    if (!known.has(row.dish_id)) select.add(new Option(row.name, row.dish_id));
  }
  get("demo-status").textContent = [
    stageExplanation[data.stage],
    data.venue_count + " 个模拟食堂",
    candidates.length + " 个完整餐次",
    candidates.filter(row => row.available).length + " 个当前可供",
    eligible.length + " 个满足本次条件",
  ].join(" · ");

  const offers = [result.primary, ...result.alternatives].filter(Boolean);
  get("results").replaceChildren(...offers.map((item, i) => card(byId.get(item.bundle_id), i ? "备选 " + i : "首选")));
  if (result.requested_option && !offers.some(row => row.bundle_id === result.requested_option.bundle_id)) {
    get("results").append(card(byId.get(result.requested_option.bundle_id), "你想吃的餐次"));
  }
  const wanted = candidates.find(row => row.dish_id === select.value);
  const wanting = wanted?.blocks.length
    ? " 「" + wanted.name + "」未入选：" + wanted.blocks.map(code => labels[code] ?? code).join("、") + "。" : "";
  const best = eligible.reduce((top, row) => top === null || row.score > top.score ? row : top, null);
  const primary = result.primary && byId.get(result.primary.bundle_id);
  if (!primary) {
    get("explanation").textContent = "这次没有满足所有硬约束的餐次。" + wanting;
  } else if (best.score > primary.score) {
    get("explanation").textContent = best.name + "的匹配分高出 "
      + (best.score - primary.score) / 10 + " 分，差距在 " + data.near_optimal / 10
      + " 分以内。系统在近似等优的餐次里优先选排队较短的方案。" + wanting;
  } else {
    get("explanation").textContent = primary.name + "通过硬约束，并在当前条件下获得首选。"
      + "备选会避开重复的食堂与主菜品类组合。" + wanting;
  }
  fullTrace = candidates;
  showTrace();
  highlight();
}

function showTrace() {
  const phrase = get("candidate-filter").value.trim().toLowerCase();
  const visible = fullTrace.filter(row => (row.name + " " + row.venue).toLowerCase().includes(phrase));
  get("trace-body").replaceChildren(...visible.map(row => {
    const tr = node("tr");
    const outcome = row.score === null ? row.blocks.map(code => labels[code] ?? code).join("、")
      : row.score / 10 + " 分";
    tr.append(...[row.name + " · " + row.venue, money(row.price_cents), minutes(row.queue),
      minutes(row.time), outcome].map(value => node("td", value)));
    return tr;
  }));
}

async function refresh() {
  pending?.abort();
  const controller = new AbortController();
  pending = controller;
  const dish = get("dish").value;
  const taste = get("taste").value;
  const stage = stageValue();
  const family = get("recent-family").value;
  const request = {
    constraints: {budget_max_cents: Math.round(Number(get("budget").value) * 100),
      time_budget_min: Number(get("minutes").value)},
    preferences: {profile: get("profile").value, ...(dish ? {requested_dish_id: dish} : {}),
      ...(taste ? {taste_tags: [taste]} : {}), ...(family ? {recent_family_ids: [family]} : {})},
    max_options: 4,
  };
  get("demo-status").textContent = "正在重新计算…";
  for (const button of document.querySelectorAll(".choose-meal")) button.disabled = true;
  try {
    const response = await fetch("/api/v1/demo/decision", {
      method: "POST", headers: {"Content-Type": "application/json"}, signal: controller.signal,
      body: JSON.stringify({request, stage, duck_sold_out: stage === "CURRENT" && get("sold-out").checked}),
    });
    if (!response.ok) throw new Error("request failed");
    const payload = await response.json();
    if (pending === controller) show(payload.data);
  } catch (error) {
    if (!controller.signal.aborted) {
      get("demo-status").textContent = "暂时无法计算，请点击重新计算。";
      fullTrace = [];
      for (const id of ["explanation", "results", "trace-body", "supply-detail"]) get(id).replaceChildren();
    }
  }
}

get("experiment-form").addEventListener("submit", event => {
  event.preventDefault();
  refresh();
});
function usePreset(key) {
  const preset = presets[key];
  for (const [id, value] of Object.entries(preset)) {
    if (id === "dish" && value && !Array.from(get(id).options).some(option => option.value === value)) {
      get(id).add(new Option("烤鸭饭", value));
    }
    get(id).value = value;
  }
  for (const button of document.querySelectorAll("[data-preset]")) {
    button.setAttribute("aria-pressed", String(button.dataset.preset === key));
  }
  get("sold-out").checked = false;
  get("sold-out").disabled = false;
  get("taste").value = "";
  get("recent-family").value = key === "explore" ? "noodles" : "";
  document.querySelector('[name="stage"][value="CURRENT"]').checked = true;
  return refresh();
}
document.querySelectorAll("[data-preset]").forEach(button =>
  button.addEventListener("click", () => usePreset(button.dataset.preset)));
get("candidate-filter").addEventListener("input", showTrace);
get("stage-controls").addEventListener("change", () => {
  get("sold-out").disabled = stageValue() !== "CURRENT";
  get("experiment-form").requestSubmit();
});
get("sold-out").addEventListener("change", () => get("experiment-form").requestSubmit());

function highlight() {
  document.querySelector(".guide-target")?.classList.remove("guide-target");
  if (guideStep >= 0) document.querySelector(guide[guideStep].target)?.classList.add("guide-target");
}

function setGuide(step) {
  guideStep = step;
  get("coach").hidden = step < 0;
  if (step < 0) {
    highlight();
    return;
  }
  get("coach-title").textContent = guide[step].title;
  get("coach-text").textContent = guide[step].text;
  get("coach-progress").textContent = step === 3 ? "完成 ✓" : "第 " + (step + 1) + " / 3 步";
  highlight();
}

function locateGuide() {
  if (guideStep < 0) return;
  const target = document.querySelector(guide[guideStep].target) ||
    get(guideStep === 0 ? "student-stop" : "ops-stop");
  target.scrollIntoView({block: "center"});
  if (target instanceof HTMLButtonElement && !target.disabled) target.focus({preventScroll: true});
}

function clearOps() {
  opsPending?.abort();
  opsPending = null;
  get("signals").hidden = true;
  get("generate-plan").hidden = true;
  get("generate-plan").disabled = false;
  get("plan-output").hidden = true;
  get("summarize").disabled = !chosen;
  get("ops-demo-status").textContent = chosen ? "已选 " + chosen.name + "。可以开始梳理需求。" : "请先在学生视角选一餐。";
}

function chooseMeal(candidate) {
  chosen = candidate;
  get("selection-receipt").hidden = false;
  get("chosen-label").textContent = "你选了 " + candidate.name;
  get("share-intent").checked = false;
  get("share-intent").disabled = candidate.dish_id !== "D_A_DUCK";
  get("share-label").textContent = candidate.dish_id === "D_A_DUCK"
    ? " 将本次烤鸭饭意向纳入模拟汇总" : " 此演示仅汇总烤鸭饭意向";
  for (const button of document.querySelectorAll(".choose-meal")) {
    const selected = button.dataset.meal === candidate.id;
    button.setAttribute("aria-pressed", String(selected));
    button.textContent = selected ? "已选这份" : "就选这份";
  }
  clearOps();
  if (guideStep >= 0) setGuide(1);
  if (!get("share-intent").disabled) get("share-intent").focus({preventScroll: true});
}

function showSignals(data) {
  const entries = [["想吃", data.signals.preference], ["打算去吃", data.signals.intent], ["自报吃过", data.signals.reported_meal]];
  get("signals").replaceChildren(...entries.map(([label, count]) => {
    const box = node("div");
    box.append(node("span", label), node("strong", count ?? "未披露"));
    return box;
  }));
  get("signals").hidden = false;
  get("ops-demo-status").textContent = data.included
    ? "本次烤鸭饭选择已纳入模拟意向。"
    : "当前汇总使用预设的模拟烤鸭饭数据。";
}

function showPlan(plan) {
  const name = node("span", "建议追加");
  const portions = node("strong", String(plan.feasible_extra_portions ?? 0) + " 份");
  get("plan-head").replaceChildren(name, portions);
  const constraint = plan.evidence_codes.includes("INGREDIENT_LIMIT") ? "原料是本次追加量的限制。" : "";
  get("plan-note").textContent = "估计需求 " + plan.demand_proxy_portions + " 份，已计划 " + plan.planned_supply_portions
    + " 份，缺口 " + plan.shortage_portions + " 份。" + constraint;
  get("plan-output").hidden = false;
  get("plan-output").focus({preventScroll: true});
}

async function operations(action) {
  if (!chosen || opsPending) return;
  const controller = new AbortController();
  opsPending = controller;
  get("summarize").disabled = get("generate-plan").disabled = true;
  get("ops-demo-status").textContent = "正在计算…";
  try {
    const response = await fetch("/api/v1/demo/operations", {
      method: "POST", headers: {"Content-Type": "application/json"}, signal: controller.signal,
      body: JSON.stringify({action, chosen_dish_id: chosen.dish_id, share_intent: get("share-intent").checked}),
    });
    if (!response.ok) throw new Error("operations request failed");
    const {data} = await response.json();
    if (opsPending !== controller) return;
    showSignals(data);
    get("generate-plan").hidden = false;
    if (action === "PLAN") showPlan(data.plan);
    if (guideStep >= 0) setGuide(action === "PLAN" ? 3 : 2);
  } catch (error) {
    if (!controller.signal.aborted) get("ops-demo-status").textContent = "暂时无法读取，请重试。";
  } finally {
    if (opsPending === controller) {
      opsPending = null;
      get("summarize").disabled = get("generate-plan").disabled = false;
    }
  }
}

get("start-guide").addEventListener("click", async () => {
  setGuide(0);
  get("student-stop").scrollIntoView();
  await usePreset("duck");
  if (guideStep === 0) locateGuide();
});
get("coach-close").addEventListener("click", () => {
  const target = document.querySelector(guide[guideStep]?.target ?? "#start-guide");
  setGuide(-1);
  (target ?? get("start-guide")).focus();
});
get("coach-jump").addEventListener("click", locateGuide);
get("share-intent").addEventListener("change", () => {
  clearOps();
  if (guideStep > 1) setGuide(1);
});
get("summarize").addEventListener("click", () => operations("SUMMARIZE"));
get("generate-plan").addEventListener("click", () => operations("PLAN"));
get("show-approved").addEventListener("click", async () => {
  get("comparison").open = true;
  document.querySelector('[name="stage"][value="W2_APPROVED"]').checked = true;
  get("sold-out").disabled = true;
  if (!get("experiment-form").reportValidity()) {
    get("experiment-form").closest("details").open = true;
    return;
  }
  await refresh();
  get("comparison").scrollIntoView({block: "center"});
});
refresh();
