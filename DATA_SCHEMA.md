# DATA_SCHEMA.md

> FORKCAST（食序）V0.3 · M0 修订 r1 · 2026-11-21（北京时间）

本文件定义统一字段、数据来源、所有权和跨模块不变量。搭配 [决策引擎](DECISION_ENGINE_SPEC.md)、[虚拟智慧食堂](SIMULATION_SPEC.md)、[服务接口](MCP_API_SPEC.md)使用。以一份完整餐次为推荐单位，以菜品标准份为库存与备餐单位。

## 1. 版本、单位和命名

| 项 | V0.3 约定 |
| --- | --- |
| 版本 | `schema_version = "0.3"`。每个推荐另带 `rules_version`、`config_version`；MCP 协议版本独立 |
| `data_mode` | `SIMULATION` / `LIVE`。V0.2 的 `DEMO` 迁移到 `SIMULATION`。识别两种模式，P0 只启用 `SIMULATION`。运行创建时绑定模式，直至关闭。具体拒绝、来源及隔离见 MCP/API §7 |
| 时间 | 带时区的 RFC 3339 字符串。比较使用绝对时刻。校园营业与运营窗以 `Asia/Shanghai` 显示，区间为左闭右开 |
| 单位 | 人民币整数分（`*_cents`）、整数分钟（`*_min`）、非负整数标准份（`*_portions`）。等候时间区间含上下界 |
| 缺失 | `?` 表示可省略的输入字段，`T \| null` 表示可空。必填字段缺失或枚举未知按无效输入处理。空列表表示已知为空 |
| 标识 | ID 为大小写敏感的不透明字符串，长度 1–64，字符限定英数、点、下划线和连字符。目录 ID 稳定，运行内 ID 不得被另一运行复用 |
| 范围 | 目录可跨运行复用。动态数据、用户会话、反馈、运营提案均按 `run_id` 隔离，枚举与字段统一使用本文件名称 |

## 2. 来源、时间与快照

**`Fact<T>`** 包含 `value: T | null`、`source_kind`、`source_ref: string | null`、`as_of: timestamp | null`、`valid_until: timestamp | null`、`confidence: HIGH | MEDIUM | LOW | UNKNOWN`。来源枚举为 `AUTHORIZED`（授权校方数据）、`PUBLIC`（公开资料）、`ESTIMATED`（估算）、`SYNTHETIC`（模拟）、`UNKNOWN`。历史菜单保留原出处、采集日期及适用范围。由历史菜单配出的虚拟价格、库存、队列等各自记录来源。用不含个人数据的 `source_ref` 或公开引用标识来源。

- `freshness` 派生自快照时钟。无值、动态事实缺任一时间戳或 `as_of > clock_at` 为 `UNKNOWN`；其余若 `valid_until != null` 且 `clock_at >= valid_until` 为 `STALE`，否则为 `FRESH`。有效期存在时须 `as_of < valid_until`。静态已知值须保留适用日期与来源，是否过期仍按同一规则判断。有值且有效的动态事实必须有截止；缺档测试可缺时间并按未知处理。静态事实可无截止，但仍须记录来源和资料日期。未知值用 `null`，未知库存和队列保留未知。
- `HIGH` 适用于适用时段内有证据的测量或核验；`MEDIUM` 适用于有规则和可追溯输入的估算，以及模拟器确定的已知虚拟状态；`LOW` 适用于近似或超期事实；关键值未知时为 `UNKNOWN`。等级是证据质量，数据可信度在模拟运行内成立。
- `SnapshotRef` 必填 `run_id, snapshot_id, revision, clock_at, data_mode, catalog_version, schema_version`。`revision` 为同一运行内递增整数，从 1 开始。快照不可变，引用指向一致的目录、时钟、菜品份数、营业状态和队列。信号汇总使用独立的 `signals_revision`，反馈写入不引发供应快照变化。
- `SupplySnapshot` 含 `ref: SnapshotRef, active_window_id: ID | null, venue_states: VenueState[], dish_states: DishState[]`。服务端另持只供 B 端的同版本计划和容量。窗外 `active_window_id=null`，当窗菜品状态为空。
- 规则评估以快照 `clock_at` 为准。服务使用实际提交时间处理凭证和技术日志。重放按 `run_id + snapshot_id + catalog_version + rules_version + config_version` 找回输入。
- `DataProvider` 读取指定 `run_id` 最新或指定 `snapshot_id` 的完整快照，及其固定版本目录。虚拟供应使用 `MockProvider` 实现。未来授权适配器遵守同一契约，并保留字段级来源。接口请求不能自行切换 `data_mode`。

## 3. 静态目录与运行状态

| 实体 / 主键 | 字段与约束 |
| --- | --- |
| `Venue` / `venue_id` | `name: Fact<string>`，`location_label: Fact<string>`，`opening_hours: Fact<TimeRange[]>`，`aliases: string[]`。场所别名须经核对；模拟营业时间独立标来源 |
| `Dish` / `dish_id` | `venue_id`，`name: Fact<string>`，`family_id`，`taste_tags: string[]`，`serving_label`，`ingredients: Fact<string[]>`，`allergens: Fact<string[]>`，`kcal_range: Fact<IntRange>`。配料与过敏原分别给出，列出已核字段；缺完整记录用 `null` |
| `MealBundle` / `bundle_id` | `venue_id`，`label`，`anchor_dish_id`，`items: [{dish_id, portions}]`，`meal_role: COMPLETE_MEAL`。同一场所、1–3 个不同菜品，每个标准份数为正整数，包含主菜 `anchor_dish_id`。套餐与单品完整餐由目录明确登记 |
| `OriginWalk` / `(origin_zone_id, venue_id)` | `walk_min: Fact<IntRange>`。支持匿名校园区域及虚拟默认起点 `DEMO_DEFAULT`，不采集精确轨迹 |
| `ServiceWindow` / `window_id` | `starts_at, ends_at`、`label`。同一运行的用餐窗互不交叠，供给状态属于指定窗。持久化键合并运行 ID，窗 ID 按运行命名空间生成 |
| `VenueState` / `(snapshot_id, venue_id)` | `service_status: Fact<OPEN \| CLOSED>`，`queue_min: Fact<IntRange>`，`service_min: Fact<IntRange>`。排队、取餐分开计时 |
| `DishState` / `(snapshot_id, window_id, dish_id)` | `service_status: Fact<AVAILABLE \| SOLD_OUT \| UNAVAILABLE>`，`stock_portions: Fact<int>`，`price_cents: Fact<int>`。有保障的可售状态要求 `AVAILABLE` 且可售份数大于零。售罄表示可售份数为零 |
| `SupplyPlan` / `(run_id, window_id, dish_id)` | `base_planned_portions, confirmed_extra_portions, max_served_portions`；按菜品标准份管理。已确认加量与基线之和不超过菜品上限 |
| `PrepResources` / `(run_id, window_id, venue_id)` | `extra_prep_capacity_portions` 与 `reserved_prep_portions`。同窗同食堂的追加备餐共用产能 |
| `IngredientPool` / `(run_id, window_id, dish_id)` | `extra_ingredient_portions, reserved_ingredient_portions`，以该菜可再制作的标准份数表示。虚拟场景给各菜独立原料池 |

- `IntRange` 含 `min, max`，均为非负整数且 `min <= max`。`TimeRange` 含 `starts_at, ends_at`，前者必须早于后者。
- 每个组件用 `DishState.price_cents × portions` 计价。餐次价格之和有任一未知或过期即输出未知。可组成的最大餐次份数为各组件 `floor(stock_portions / portions)` 的最小值，任一组件库存未知时输出未知。供应窗外的剩余份数不会自动移入下一窗。
- 标签与食材使用版本化字典。组合成本、时间、营养和含有的已知食材由组件事实推导；任何推导值保留主要证据和不确定性。各组件热量区间齐全时方可相加。实际称重价格需要适配成明确的标准份估价，禁止把估价当作小票实付。

## 4. 决策上下文与结果

| 类型 | 字段 |
| --- | --- |
| `DecisionRequest` | `constraints?: Constraints = {}`，`preferences?: Preferences = {}`；`origin_zone_id?: ID = DEMO_DEFAULT`（P0），`max_options?: int = 2`（1–4，保留是否显式提交）；外层 `run_id` 由服务请求传入 |
| `Constraints` | `budget_max_cents?: int`，`time_budget_min?: int`，`avoid_ingredients?: string[]`，`avoid_allergens?: string[]`。上限 0–100000 分、0–180 分钟，列表各最多 20，元素为字典键，提交原始列表先校验长度，再按键去重 |
| `Preferences` | `taste_tags?: string[]`，`requested_dish_id?: ID`，`profile?: DEFAULT \| FAST \| VALUE \| EXPLORE`，`recent_family_ids?: string[]`，`nutrition_goal?: LIGHT`。味型最多 10，近期品类最多 5，只采用户当次主动提供的值。列表元素为该目录绑定字典版本中的键；空数组表示没有声明；按键去重，提交原始列表先校验长度 |
| `MealOption` | `bundle_id, venue_id, anchor_dish_id, label, items, price_cents: int \| null, queue_min: IntRange \| null, total_time_min: IntRange \| null, available_bundle_portions: int \| null, match_score: int, data_confidence, evidence[], warnings[]` |
| `Evidence` | `code, entity_id, field, source_kind, source_ref, as_of, freshness`。可选 `value` 仅传与判断有关且可公开的值，排除禁忌原词等个人信息 |
| `DecisionResult` | `recommendation_id, snapshot: SnapshotRef, rules_version, config_version, status: OK \| NO_FEASIBLE_OPTION, primary: MealOption \| null, alternatives: MealOption[], requested_option: MealOption \| null, conflicts: Evidence[], warnings: Evidence[]` |
| `ReplanResult` | `previous_recommendation_id, result: DecisionResult, outcome: UNCHANGED \| UPDATED \| NO_FEASIBLE_OPTION, change_reasons: Evidence[], selected_bundle_id: ID \| null, selection_state: NONE \| FEASIBLE \| NEEDS_RESELECT` |

`RecommendationRecord` 内部持有 `recommendation_id, run_id, principal_id, snapshot: SnapshotRef, origin_window_id: ID | null, normalized_request: DecisionRequest, explicit_max_options: bool, deadline_at: Timestamp | null, selectable_bundle_ids: ID[], result: DecisionResult, previous_recommendation_id: ID | null, rules_version, config_version`。`origin_window_id` 是这次结果快照的窗口。每次重规划结果有自己的窗口，继承原绝对截止。`selectable_bundle_ids` 为首选、备选和可行指名项去重后的集合。候选排除原因、评分分项和原意愿阻断原因可重放。对外只返回结果字段。证据码、排序、旧选项处理以 [决策引擎](DECISION_ENGINE_SPEC.md)为准。

## 5. 同意、反馈和 B 端汇总

**授权记录**按 `(run_id, principal_id)` 保存 `share_for_ops: boolean`（默认 `false`）、`consent_revision`、`updated_at`。授权用途为向模拟 B 端提供去标识化需求信号。`Session` 内部含 `run_id, principal_id, role: STUDENT | OPS | EXTERNAL_AGENT, session_kind: DEMO | USABILITY_TEST | OPS | EXTERNAL_AGENT, allowed_venue_ids: ID[]`（运营范围），主体、角色和种类均由服务设定。内置种子和通过一次性演示凭据加入的 C 端会话为 `DEMO`，其他匿名 C 会话为 `USABILITY_TEST`。服务从 `session_kind` 派生 `stream`，仅 `DEMO` 生成 `SIMULATED`；普通试测反馈归 `USABILITY_TEST`。演示中本人提交的“已吃”是虚拟用餐自报。

**`FeedbackEvent`** 由服务写入 `event_id, run_id, principal_id, window_id, occurred_at: Timestamp, commit_seq: int, stream: SIMULATED | USABILITY_TEST, event_type, recommendation_id?, bundle_id?, dish_id?, consent_revision`。客户端只提交其允许字段，归属、时钟和权限取服务状态。

| 事件 | 字段与含义 |
| --- | --- |
| `PREFERENCE` | 必填 `dish_id`，提交当次明确希望吃的菜，归于其场所。不得用点击浏览、推荐曝光、自由文本或禁忌推断 |
| `INTENT_SELECTED` | 必填 `recommendation_id, bundle_id`，需通过引擎 R05 的卡片归属、原窗、截止和当前供应复核。以实际选中的餐次记录，表示打算去吃 |
| `INTENT_CLEARED` | 必填 `recommendation_id`，在意向所属窗清除本会话最近意向 |
| `DINED` | 必填 `recommendation_id, bundle_id`，可与原意向不同。须是该会话在所指推荐记录中见过的选项，归入该记录的用餐窗。是自报反馈，模拟器不会据此扣库存 |
| `NOT_DINED` | 必填 `recommendation_id`，遵循 `DINED` 的用餐窗，可覆盖先前自报状态 |
| `REJECTED` | 必填 `recommendation_id`，可带所拒 `bundle_id`，若提供则须在该推荐的可选集合内。仅供本人反馈/产品测试，独立于运营估算 |

- `PREFERENCE` 采用提交时所在窗。无人用餐窗时拒绝新偏好和意向。同一会话、同一窗，每种信号取当前授权版本内最新有效声明，以数据库提交序号决胜。已授权 `DEMO` 的 `PREFERENCE`、`INTENT_SELECTED`、`INTENT_CLEARED`、`DINED`、`NOT_DINED` 成功写入时 `share_applied=true`，推进 `signals_revision` 一次，含会维持相同可见计数的写入。其他事件返回 `false`，版本不动。当前同意从开启变为关闭时，仅 `DEMO` 会话推进运行内 `signals_revision`、同时排除旧贡献，使旧版本未确认提案失效。同意的其他变化仅推进个人 `consent_revision`。同值请求无版本变更。最新 `INTENT_CLEARED` 清除意向，最新 `NOT_DINED` 清除实际用餐计数。意向受供应变化影响时 C 端保留原记录并提示重新选择，B 端继续分列未主动清除的“打算去吃”观察信号，供识别当前缺口。
- 用户撤回分享后，历史贡献立即从 B 端查询和未确认提案依据中排除，旧提案失效。后来再次授权只包含新授权版本下的新声明。授权关闭时新增事件可为本人反馈存储，默认不进入 B 端。向 B 端返回的历史汇总按当前允许的贡献重新计算。已经确认的模拟物理变更保留变更记录。
- `DemandInsight` 的分组键为 `(run_id, reference_window_id, venue_id, dish_id)`，餐次意向与用餐按 `anchor_dish_id` 汇总。返回 `preferred_sessions, intended_sessions, dined_sessions`（各为非负整数或 `null`）、`visibility: {preferred_sessions: Visibility, intended_sessions: Visibility, dined_sessions: Visibility}`，`Visibility = VISIBLE | SUPPRESSED`、`signals_revision, observed_until`。每项按 3 个不同授权会话独立释放；`observed_until` 为当前虚拟时钟和参考窗结束时刻的较早者。`null` 表示未披露。无按个人、分钟、禁忌、原文的筛选或输出。使用固定窗分组。
- 运行期会话、个人反馈和运行快照只在本地保存；演示重置创建新运行并使旧会话和待确认提案失效。V0.3 默认仅保留当前运行和重放用的内置虚拟夹具。个性化历史长期保存留待后续评审。

## 6. B 端提案、确认与审计

| 实体 | 必填字段 |
| --- | --- |
| `OpsProposal` | `proposal_id, run_id, venue_id, dish_id, reference_window_id, target_window_id, based_on_snapshot_id, signals_revision, expected_effective_at, demand_proxy_portions, planned_supply_portions, shortage_portions, proposed_extra_portions, uncovered_portions, evidence_codes[], status: PROPOSED \| CONFIRMED \| APPLIED \| STALE` |
| `OpsConfirmation` | `proposal_id, approved_extra_portions, actor_id, confirmed_at, confirmation_snapshot: SnapshotRef`。非零整数且不超过提案及确认时资源上限 |
| `SimulationEvent` | `event_id, run_id, scheduled_at: Timestamp, sequence: int, event_type: SimulationEventType, target_id: ID | null, payload: EventPayload`。事件类型与变更权限见模拟规范 |
| `AuditRecord` | `run_id, audit_id, action: OPS_CONFIRMED | OPS_APPLIED | SIM_CONTROL_APPLIED, actor_type: OPS | SYSTEM, actor_id: ID, result_code, at: Timestamp, venue_id: ID | null, snapshot_id?, proposal_id?`。只向按场所授权的运营者投影场所事件，`venue_id=null` 的运行级记录留作服务日志。对外审计去掉 `actor_id`，排除凭证、学生身份和历史需求统计 |

提案存储、确认、产能与原料预留、新快照发布必须遵循 [虚拟智慧食堂](SIMULATION_SPEC.md)的原子规则。模拟事件是现货变动的正式入口。确认事务可发布未来计划的新版本，现货仍由 `ACTIVATE_WINDOW` 生效。所有 C 端求值引用已发布的快照。

## 7. 类型闭合与时间口径（F01）

对象字段均为必填，带 `?` 的字段才可省略。显式 `null` 仅用于标注可空的字段。字符串按 UTF-8，`Timestamp` 为带时区的 RFC 3339，`ID` 使用 §1 字符集。布尔值和整数拒绝字符串强制转换。除特别说明，对象与嵌套对象均拒绝额外字段。

| 类型 | JSON 形态及范围 |
| --- | --- |
| `SnapshotRef` | §2 七个字段。两 ID 为 `ID`，`revision` 为 ≥1 的整数，`clock_at: Timestamp`，`data_mode` 为模式枚举，`catalog_version: ID`，`schema_version` 固定字符串 `0.3` |
| `Fact<T>` | §2 五个必填字段。已知合格事实有来源与适用时间，`UNKNOWN` 可为 `source_ref: null`。读取端按快照派生 `freshness`，不写入事实对象 |
| `Evidence` | `code: string`（引擎 R05–R06 证据码），`entity_id: ID \| null, field: string \| null, source_kind, source_ref: string \| null, as_of: Timestamp \| null, freshness: FRESH \| STALE \| UNKNOWN, value?: PublicEvidenceValue`。系统提示如 `SIMULATION_DATA` 无实体、字段事实时用 null 与 `UNKNOWN`。公开值限布尔、数字、字符串、公开标签字符串数组或 `IntRange`，禁止禁忌原词、凭证和个人标识 |
| `MealOption` | §4 全字段必填。各 ID 按 §1，`label: string`；`items: {dish_id: ID, portions: positive int}[]`，1–3 项；价钱、队列、耗时与份数按 §4 可空；`match_score: int` 0–1000，`data_confidence` 取 Fact 的可信度枚举，`evidence, warnings: Evidence[]` |
| `DecisionResult / ReplanResult` | §4 字段全必填，规则配置版本为 ID，`conflicts, warnings, change_reasons: Evidence[]`。无解时首选、指名项为 null，备选为空。快照需与 API 信封一致 |
| `DemandInsight` | §5 分组键均为 ID。指标按非负整数或 null，`signals_revision` 为运行内 ≥0 整数，`observed_until: Timestamp`。每渠道达到阈值才释放数值 |
| `OpsProposal / OpsConfirmation` | §6 字段全必填，引用和版本同前，`expected_effective_at, confirmed_at: Timestamp`，`evidence_codes: string[]`。量均为非负整数，实际批准量和可确认提案建议量为正整数。`confirmation_snapshot` 指确认落账后发布的快照 |
| `SimulationEvent / EventPayload` | §6 字段全必填，`sequence` 为非负整数。事件枚举、目标键和对应 payload 见模拟 §2.1。固定事件 ID 与顺序；实际运行内事件 ID 带该运行命名空间 |
| `OpsAnalysis / OpsState` | MCP/API §3 定义 API 投影，供应与状态按同一确认快照读回。历史已确认动作只返回物理量、相关窗口、状态与可见审计引用，不返回已撤回需求量 |

`occurred_at` 用提交时的虚拟时钟，迟到的就餐自报以推荐记录归窗。聚合只消费 `occurred_at <= clock_at` 且归属指定窗的事件，因此迟到自报可更新历史窗，但不会进入当前窗或回写已生效计划。`scheduled_at`、`expected_effective_at`、应用时间及推荐截止使用虚拟时钟。`confirmed_at`、授权更新时间、技术审计 `at`、会话及凭证到期使用实际服务时钟。反馈、对整窗状态的推进、确认、授权改变按同一运行事务顺序提交。
