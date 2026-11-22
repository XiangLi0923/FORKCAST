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
  el.append(details);
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
  if (select.options.length === 1) {
    for (const row of candidates) select.add(new Option(row.name, row.dish_id));
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
document.querySelectorAll("[data-preset]").forEach(button => button.addEventListener("click", () => {
  const preset = presets[button.dataset.preset];
  for (const [id, value] of Object.entries(preset)) get(id).value = value;
  get("sold-out").checked = false;
  get("sold-out").disabled = false;
  get("taste").value = "";
  get("recent-family").value = button.dataset.preset === "explore" ? "noodles" : "";
  document.querySelector('[name="stage"][value="CURRENT"]').checked = true;
  get("experiment-form").requestSubmit();
}));
get("candidate-filter").addEventListener("input", showTrace);
get("stage-controls").addEventListener("change", () => {
  get("sold-out").disabled = stageValue() !== "CURRENT";
  get("experiment-form").requestSubmit();
});
get("sold-out").addEventListener("change", () => get("experiment-form").requestSubmit());
refresh();
