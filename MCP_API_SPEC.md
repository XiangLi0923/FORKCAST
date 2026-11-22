# MCP_API_SPEC.md

> FORKCAST（食序）V0.3 · M0 修订 r1 · 2026-11-21（北京时间）

本文件定义学生端、运营端与外部 Agent 的能力合约。Web 界面走本地 HTTP，外部 Agent 走 MCP。两种入口调用同一个应用服务、身份权限、快照、反馈账和备餐事务。字段语义以 [DATA_SCHEMA.md](DATA_SCHEMA.md)为准。

## 1. 协议和部署

- MCP 兼容基线采用 `2025-11-25`，P0 用本地 `stdio`。支持 `initialize`、能力声明、`notifications/initialized`、`tools/list`、`tools/call` 和客户端关闭。stdout 只用于协议消息，日志进 stderr。工具描述声明真实读写行为，服务逐次验证权限，清理输出和日志。每认证主体每滚动 60 秒最多 120 次 `tools/call`，未认证连接按连接计数，超限为 `RATE_LIMITED`。
- 七个工具均发布 JSON Schema `inputSchema` 和 `outputSchema`。顶层输入为对象，拒绝未声明字段、无效类型与越界值。成功及业务错误使用可校验的 `structuredContent`，同时提供 JSON 文本内容以兼容只读文本的客户端。协议封装错误按 JSON-RPC，工具执行错误置 `isError=true`。
- HTTP 业务前缀为 `/api/v1`，请求响应 UTF-8 JSON。读取最新快照时锁定一次版本，返回相同数据语义和数据模式。对于能取得数据的成功结果，快照信息随响应返回。B 端读取聚合和生成提案分别固定同一时点的供应快照及信号版本，不拼接并发更新中的内容。
- Web 后端与 MCP 适配器只连接本地共享应用服务。外部校方平台集成、公开远程部署与 Streamable HTTP 留待单独评审。P0 用一个兼容客户端或测试宿主实际完成初始化和首选/重规划调用。

## 2. 身份与授权

| 身份 | 获得方式 | 默认能力 |
| --- | --- | --- |
| `STUDENT` | 本地 C 端匿名会话。运行内生成不透明主体标识，默认 `USABILITY_TEST`，凭有效一次性演示凭据加入 `DEMO` | 查询菜单、推荐与重规划，按本人操作记录意向、自报反馈与分享同意 |
| `OPS` | 本地独立演示运营凭证，服务端限定运行和场所范围。禁止共享 C 端匿名权限 | 查固定窗聚合信号、生成备餐提案、人工确认，以及限定的演示控制 |
| `EXTERNAL_AGENT` | 本地 MCP 宿主由演示运营者配置受限凭证，限定运行 | 查询菜单、推荐、重规划自己的推荐。禁止写反馈、读汇总或改供应 |
| 授权 MCP 会话 | 强制将本地宿主配置映射到已存在的 `STUDENT` 或 `OPS` 主体，使用角色与工具范围 | 仍逐次检查会话、场所及写操作确认，不能获得宿主配置范围之外的能力 |

- 认证在服务端完成。客户端提交的角色、头部明文主体标识或用户提示词都不能授予权限。凭证不放查询字符串、日志和工具参数，Web 写入遵守同源与会话防伪要求。
- 运营模式仅开放聚合后的数据和运营操作记录，C 和外部 Agent 无法枚举别人的推荐记录。读未拥有的推荐 ID 一律表现为不存在。MCP 工具发现按会话范围过滤，直接调用也经过相同权限门。
- Web 写反馈和确认备餐要求界面里的明确动作。对于被授予 MCP 写权限的会话，`submit_feedback` 与 `confirm_ops_action` 另要求 `interaction_receipt`。服务只向已认证、同源的受信 Web 会话发动作回执，绑定主体、运行、工具、规范化参数摘要、期限和幂等键。用户点击认可后获取，并经本地宿主转交。默认 120 秒失效，首次成功消费，幂等重试可回放成功结果。回执不授予额外权限。Agent 自行传入 `confirmed=true` 等字段无效。
- 运营数据分享授权单独管理。能写反馈并不意味着可写运营信号，分享开关与授权版本在服务端判定。开启同意、撤回、重新同意均需本人会话和明确操作。`share_applied` 按数据 §5 判定是否进入授权账，与小样本可见性无关。

## 3. 七项核心能力

除另有说明，入参均为命名字段。HTTP 中的 `run_id` 与表中核心工具参数同名。支持 `snapshot_id` 的工具省略时读取最新，显式快照只在同一运行且可见时可读。详情类型详见 [数据契约](DATA_SCHEMA.md)。

| 工具 / HTTP 对应 | 允许身份 | 输入 | 成功返回与语义 |
| --- | --- | --- | --- |
| `get_dining_options` · `GET /api/v1/dining-options` | C / B / Agent | `run_id`，可选 `snapshot_id, venue_id, cursor, limit` | `DiningOptions` 含可见食堂及当窗目录事实、`next_cursor`。可见供应状态含未知与售罄，调用方需通过推荐工具判断可执行性 |
| `recommend_meal` · `POST /api/v1/recommend` | C / Agent | `run_id, request: DecisionRequest`，可选 `snapshot_id` | `DecisionResult`，在服务端关联本次会话、原始条件与服务时钟。无可行解是成功业务结果 |
| `replan_meal` · `POST /api/v1/replan` | C / Agent | `run_id, previous_recommendation_id`，可选 `max_options` | `ReplanResult`，使用最新快照与原有条件。C 包含自己已选的原意向、复核状态和变化解释。自动刷新不写反馈 |
| `submit_feedback` · `POST /api/v1/feedback` | C | `run_id, event_type, recommendation_id?, bundle_id?, dish_id?, idempotency_key`。MCP 加 `interaction_receipt` | `FeedbackReceipt` 含 `event_id, event_type, window_id, share_applied, signals_revision`。按类型校验必填项与归属 |
| `get_demand_insights` · `GET /api/v1/admin/demand-insights` | B | `run_id, reference_window_id`，可选 `venue_id` | `DemandInsights` 含 `signals_revision, observed_until, rows: DemandInsight[]`。服务按权限范围裁剪场所并应用渠道阈值 |
| `generate_ops_recommendations` · `POST /api/v1/admin/ops-proposals` | B | `run_id, reference_window_id, target_window_id`，可选 `venue_id` | `OpsReport` 含 `signals_revision, analyses: OpsAnalysis[], proposals: OpsProposal[]`。计算供需缺口、可备量及原料复核量。至此只建立提案 |
| `confirm_ops_action` · `POST /api/v1/admin/ops-confirmations` | B | `run_id, proposal_id, approved_extra_portions, expected_snapshot_id, idempotency_key`。MCP 加 `interaction_receipt` | `OpsConfirmation`，原子地预约虚拟备餐资源并发布快照。目标窗口开始时生效 |

**响应投影。** `DiningOptions` 的 `venues[]` 含 `venue_id,name,location_label,opening_hours,service_status,queue_min`，目录 `options[]` 含 `bundle_id,venue_id,label,items,anchor_dish_id,price_cents,available_bundle_portions,supply_state`，状态枚举 `AVAILABLE, SOLD_OUT, UNAVAILABLE, UNKNOWN`。食堂字段按事实返回。餐次价、份数与状态附 `evidence[]`；页面分页按 `bundle_id` 升序，`limit=20`，范围 1–50。签发的游标绑定场景、快照、食堂过滤和分页位置。续页锁定绑定快照，省略过滤或页大小时继承游标。显式 `snapshot_id`、`venue_id` 或 `limit` 与绑定冲突报 `INVALID_INPUT`。组餐任一组件未知或过期使 `supply_state=UNKNOWN`，新鲜的确定阻断优先。任一组件当前明确暂停为 `UNAVAILABLE`；否则任一组件当前确认售罄或已知库存不足一份餐次时为 `SOLD_OUT`；其余已知且均可售为 `AVAILABLE`，剩余情况为 `UNKNOWN`。该状态仅说明供应，完整可行性由引擎判断。新请求无快照也无游标时看最新状态。

**运营投影。** `OpsAnalysis` 含 `venue_id, dish_id, reference_window_id, target_window_id, demand_proxy_portions, planned_supply_portions, shortage_portions, feasible_extra_portions, uncovered_portions, ingredient_review_portions, evidence_codes[]`。三种信号均压制时代理、缺口、可备量、未覆盖及原料复核量为 `null`，不得回推隐匿数量。规则证据采用 `DEMAND_FROM_PREFERENCE`、`DEMAND_FROM_INTENT`、`DEMAND_FROM_SELF_REPORT`、`INSUFFICIENT_SIGNAL`、`DISH_CAPACITY_LIMIT`、`PREP_CAPACITY_LIMIT`、`INGREDIENT_LIMIT`，分别标记用于最大值的可见渠道或限制，平手可同时标记。

## 4. 辅助 HTTP 合约

| 路径 | 身份与请求 | 结果 |
| --- | --- | --- |
| `GET /health` | 本机 | 就绪状态、应用版本、识别与启用的数据模式，字段见 §7。排除凭证和路径 |
| `POST /api/v1/session` | 本地 C，`run_id, demo_enrollment_token?` | `SessionInfo{session_kind: DEMO｜USABILITY_TEST, share_for_ops: bool, consent_revision: int}`。同源会话 Cookie 设凭据；未交票据建 `USABILITY_TEST`。新会话同意关闭、版本 0 |
| `GET /api/v1/snapshot` | 当前运行 C 或 B，传 `run_id` | 当前 `SnapshotRef`。学生界面前台每 2 秒比较一次版本，失败短暂退避并提示刷新，后台页可在恢复可见时立即检查 |
| `PUT /api/v1/consent/ops-sharing` | 本人 C，`run_id, share_for_ops, expected_consent_revision, idempotency_key` | 新同意版本及 `signals_revision`。撤回同步影响 B 端查询与待确认提案 |
| `POST /api/v1/interaction-receipts` | 限定的本地受信 Web 会话，`run_id, tool, action_arguments` | 校验动作与权限后签发短时回执，仅用于授权 MCP 宿主的写能力测试与演示 |
| `GET /api/v1/runs/current` | 本地同源运行发现入口 | `CurrentRun{scenario_id: ID}` 和公共当前快照，无旧主体或需求数据 |
| `POST /api/v1/admin/simulation/demo-enrollments` | 有运行控制权的 B，`run_id, idempotency_key` | `DemoEnrollment{demo_enrollment_token: Token, expires_at: Timestamp}`，实际时间 120 秒有效 |
| `GET /api/v1/admin/ops-state` | B，`run_id, target_window_id, venue_id?, snapshot_id?` | `OpsState`，按同一可见快照读回计划、确认、生效与相关审计 |
| `POST /api/v1/admin/simulation/reset` | 授权演示 B，`scenario_id, idempotency_key` | 新运行和初始快照，旧运行失效。旧 C 会话通过 `runs/current` 发现当前运行，再创建新会话。演示身份和同意须在新运行重新取得 |
| `POST /api/v1/admin/simulation/events` | 授权演示 B，`run_id, expected_snapshot_id, event_type, payload, idempotency_key` | 原子提交模拟控制，返回新快照。界面允许售罄、队列和时钟三类操作。按 [模拟规范](SIMULATION_SPEC.md) §2.1 校验字段与模式 |

HTTP 下 `SET_SOLD_OUT` 需要 `dish_id`，可带 `reason`，`SET_QUEUE` 需要 `venue_id, queue_depth, throughput_per_min`，`ADVANCE_CLOCK` 需要 `to_clock_at`。对外控制器拒绝夹具内部事件及直接补造库存。面向评委的 B 端展示待生效的计划和已生效库存，控时与实际供给执行是可见操作。

## 5. 公共响应、错误与重试

- 成功信封含 `schema_version, ok=true, data_mode, snapshot: SnapshotRef, data`。返回类型自身也含引用时必须一致。运行尚未解析的错误用 `data_mode=null, snapshot=null`。错误信封为 `schema_version, ok=false, data_mode, snapshot, error={code,message,retryable,details?}`。错误详情只暴露对当前主体可见的引用。健康检查例外，只返回健康字段。
- 业务请求的模式由服务端选择，所有非健康成功响应标识 `SIMULATION`。C、B 与外部工具都应保留该标识与事实的来源字段。允许自然语言转述，不能把估算写成实测或在无解时捏造菜品。
- 幂等键长度 8–128，仅允许可见 ASCII。统一作用域为认证主体、运行与操作；新建运行的重置键作用域为运营主体与操作。相同键、相同业务负载回放同一结果，不重复写。相同键、不同业务负载返回冲突。幂等记录与成功变更原子提交，认证和对象作用域检查后才查询幂等，幂等先于版本或回执过期。业务摘要排除回执和幂等键，回执单独绑定键。旧运行或已撤销的权限不能用回放绕过。重复重置只回放其原结果，客户端以 `runs/current` 判定现行运行。若响应超时，先用原键和原业务负载重试。

| 业务码 | HTTP | 客户端行动 |
| --- | ---: | --- |
| `INVALID_INPUT` | 400 | 修正字段、枚举、范围或无效目标窗 |
| `UNAUTHENTICATED` | 401 | 重新建立会话 |
| `FORBIDDEN` / `CONFIRMATION_REQUIRED` / `MODE_DISABLED` | 403 | 授权不符、动作未确认或模式不可用。无副作用 |
| `NOT_FOUND` | 404 | 找不到或无权看到相关对象 |
| `RUN_REPLACED` / `STALE_SELECTION` | 409 | 切至当前运行重开会话，或点击失败后重规划；带可见的最新快照 |
| `SNAPSHOT_CONFLICT` / `STALE_PROPOSAL` / `OPS_ALREADY_CONFIRMED` | 409 | 按最新供应与信号重新读取或生成提案 |
| `IDEMPOTENCY_CONFLICT` | 409 | 确认新动作后使用新键 |
| `RATE_LIMITED` | 429 | 等待窗口释放再重试 |
| `DEPENDENCY_UNAVAILABLE` | 503 | 短退避后重试，页面保留已读到的旧数据并提示 |
| `INTERNAL_ERROR` | 500 | 本地日志记录追踪号，响应仅含安全信息 |

`NO_FEASIBLE_OPTION`、`UNCHANGED`、隐匿需求以及无法补备的缺口都是正常业务状态。只有并发失败、权限等合约错误使用错误信封。MCP 的未知工具或协议层形态错误使用 JSON-RPC 错误，已识别工具在执行中报错使用业务码与 `isError=true`。

## 6. 端到端调用顺序

1. 学生/Agent 调 `get_dining_options` 查菜单，或直接 `recommend_meal` 得到卡片。学生端按本人意愿开启分享并通过 `submit_feedback` 记录。
2. B 端用虚拟时钟或供给事件造成原餐次售罄。C 前台探测新版本自动重规划。Agent 可按相同推荐引用显式 `replan_meal`。
3. B 查看已授权并去标识汇总，生成提案，人工确认。模拟时钟进入下个供餐窗后，计划转为可售数量，学生端与 Agent 看到新的快照。
4. 验收时B 以管理 HTTP 读回计划、确认和生效审计，以 HTTP 与 MCP 在同一可见快照核对已生效供给和重规划业务字段。C、B 与外部 Agent 三种权限覆盖用例见 [交接与开发计划](CODEX_DEV_PLAN.md)。

协议参考：[MCP Tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools) · [Transports](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports) · [Lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle)。

## 7. 模式、运行和授权边界（F04）

- 创建运行时由服务绑定 `data_mode`、`DataProvider` 和 `run_id`。客户端无权更改模式。出现未声明的 `data_mode` 参数报 `INVALID_INPUT`。P0 只能通过 `MockProvider` 创建 `SIMULATION`，配置请求 `LIVE` 时拒绝创建、报告 `MODE_DISABLED` 并标记服务未就绪。`GET /health` 返回 `{ready: bool, app_version: ID, recognized_modes: DataMode[], enabled_modes: DataMode[]}`，P0 分别为 `[SIMULATION,LIVE]`、`[SIMULATION]`。
- 快照、会话、推荐、同意、反馈、提案、幂等、审计和缓存按服务确定的模式和运行隔离。公开静态目录可按目录版本复用。重置使原运行及全部会话和票据失效。旧运行的本人凭据可得到 `RUN_REPLACED`，只附其可见信息，未知或无权知晓的运行按 `NOT_FOUND`。C 页面通过 `runs/current` 取得现行运行，每次切换废弃旧请求代次，重新建立会话。MCP 由宿主重新绑定运行与权限。
- 未来 `LIVE` 授权适配器沿用数据来源和快照契约。失联返回 `DEPENDENCY_UNAVAILABLE`；客户端标明所保留的旧快照和来源。模式之间禁止自动回退。模拟控制收到有效非模拟运行时返回 `MODE_DISABLED`。
- B 查询无场所过滤时只看授权范围，显式过滤范围外场所报 `FORBIDDEN`，访问无权看到的提案或推荐 ID 报 `NOT_FOUND`。场所控制校验菜品所属场所。推进时钟、重置和发演示凭据要求运行控制权，且须已授权该运行全部场所。C / EXTERNAL_AGENT 仅获学生公开目录及本主体记录。
- 演示凭据服务端保存摘要，绑定运行与到期时间。由现场学生同源提交，消费与建立新 `DEMO` 会话原子完成。坏凭据、过期、重放报 `FORBIDDEN`。未交凭据的会话按普通试测处理。凭据、动作回执仅在必要请求体或授权发放结果中传输，从查询参数、错误及日志剔除。B 同键重试原发放结果可能已经过期，须用新键发新凭据。

## 8. MCP 严格参数与响应契约（F03）

下表及数据 §7 是 JSON Schema 的规范来源。对象拒绝额外字段，带 `?` 才可省略，显式 null 仅允许在标记可空处。 `Key` 为 8–128 个可见 ASCII 字符（0x21–0x7E），`Token` 和 `Cursor` 为 16–512 个 URL 安全字符 `[A-Za-z0-9_-]`。整数不做字符串强制转换，HTTP 查询字符串按十进制整数字面量解析后应用同一范围。

| 工具 | 完整顶层输入字段 | 成功 `data` |
| --- | --- | --- |
| `get_dining_options` | `run_id: ID, snapshot_id?: ID, venue_id?: ID, cursor?: Cursor, limit?: int[1,50]=20` | `DiningOptions{venues: VenueProjection[], options: BundleProjection[], next_cursor: Cursor｜null}` |
| `recommend_meal` | `run_id: ID, request: DecisionRequest, snapshot_id?: ID` | `DecisionResult` |
| `replan_meal` | `run_id: ID, previous_recommendation_id: ID, max_options?: int[1,4]` | `ReplanResult` |
| `submit_feedback` | `run_id: ID, event_type: EventType, recommendation_id?: ID, bundle_id?: ID, dish_id?: ID, idempotency_key: Key, interaction_receipt: Token`，另按下表校验 | `FeedbackReceipt{event_id: ID, event_type: EventType, window_id: ID, share_applied: bool, signals_revision: int≥0}` |
| `get_demand_insights` | `run_id: ID, reference_window_id: ID, venue_id?: ID` | `DemandInsights{signals_revision: int≥0, observed_until: Timestamp, rows: DemandInsight[]}` |
| `generate_ops_recommendations` | `run_id: ID, reference_window_id: ID, target_window_id: ID, venue_id?: ID` | `OpsReport{signals_revision: int≥0, analyses: OpsAnalysis[], proposals: OpsProposal[]}` |
| `confirm_ops_action` | `run_id: ID, proposal_id: ID, approved_extra_portions: positive int, expected_snapshot_id: ID, idempotency_key: Key, interaction_receipt: Token` | `OpsConfirmation` |

HTTP 对应写入省略 `interaction_receipt`。下表未列的反馈对象引用一律拒绝。确认与反馈的回执签发采用同一参数骨架，`action_arguments` 需包含幂等键及除回执外的全部动作参数，且顶部与内部的运行 ID 一致。推荐和显式重规划可持久化本人推荐记录，生成提案可持久化未确认提案。三者不能改变供应或写入学生意向。

| `EventType` | 必填引用 | 可选引用 |
| --- | --- | --- |
| `PREFERENCE` | `dish_id` | 无 |
| `INTENT_SELECTED` | `recommendation_id, bundle_id` | 无 |
| `INTENT_CLEARED` | `recommendation_id` | 无 |
| `DINED` | `recommendation_id, bundle_id` | 无 |
| `NOT_DINED` | `recommendation_id` | 无 |
| `REJECTED` | `recommendation_id` | `bundle_id` |

- `VenueProjection` 的六字段见 §3，`venue_id: ID`，其余为数据 §3 对应事实。 `BundleProjection` 含 §3 的八字段及 `evidence: Evidence[]`。组件、标签和 ID 取目录，价钱及份数为非负整数或 null，供应状态按 §3 枚举。证据附在各餐次对象上，单次读取依绑定快照求值。窗外保留目录餐次，供给为 `UNAVAILABLE`，当窗缺失的价钱和库存均为 null。
- `OpsAnalysis` 见 §3。场所、菜品和窗口均为 ID，`planned_supply_portions` 为非负整数，其余份数为非负整数或按压制规则为 null，`evidence_codes: string[]`。历史需求查询的固定参考窗须已开启；提案参考窗须等于当前窗，目标窗须相邻且尚未开启。
- `OpsState = {target_window_id: ID, plans: PlanProjection[], actions: ActionProjection[], audit: AuditProjection[]}`。计划项 `{venue_id, dish_id, base_planned_portions, confirmed_extra_portions, max_served_portions}`，三个数量字段为非负整数。动作项 `{proposal_id, venue_id, dish_id, status: CONFIRMED｜APPLIED, approved_extra_portions: positive int, confirmation_snapshot_id, application_snapshot_id: ID｜null, applied_at: Timestamp｜null}`。审计项 `{audit_id, venue_id, proposal_id, action: OPS_CONFIRMED｜OPS_APPLIED, result_code: ID, at: Timestamp, snapshot_id}`。各 ID 按数据 §1。计划按场所/菜品、动作按场所/菜品/提案 ID、审计按快照版本/审计 ID 排序。固定历史快照只读该版已发生的状态。只返回受权已确认动作的审计，保留历史物理量，排除个人与历史需求明细。默认 Agent 不拥有管理读回入口。
- 各工具 `outputSchema` 为以 `ok` 区分的成功／错误闭合信封。成功的 `data_mode`、`snapshot` 非空且一致，业务 `data` 按工具类型校验。失败含 §5 的错误对象，`code` 取该节枚举，`message: string, retryable: bool`。可再试的情况为暂时失联、限流或重读后可提交的版本冲突。`details` 可含 `field: string`（仅输入错误、无原值）、`retry_after_seconds: positive int`（仅限流）或 `trace_id: ID`（仅内部错误）。无解仍是 `ok=true`。文本块序列化相同信封。协议层真正未知工具或不合 `CallToolRequest` 消息走 JSON-RPC；已识别工具的输入校验、业务失败走信封及 `isError=true`，成功含 `isError=false` 或省略该标志。未知工具的协议错误与 JSON-RPC 形态错误依协议处理。
- 判定顺序为消息形态/工具识别 → 认证及工具权限 → 参数形态 → 运行与对象范围 → 幂等检查 → 回执及业务前置条件 → 原子执行。同键同负载的授权成功回放不再消费回执。确认优先级为已确认 → `expected_snapshot_id` 与当前快照不符 → 提案引用的快照/信号过期或目标已开 → 批准量及资源校验。分别对应 `OPS_ALREADY_CONFIRMED`、`SNAPSHOT_CONFLICT`、`STALE_PROPOSAL`、`INVALID_INPUT` 或资源已变化时的 `STALE_PROPOSAL`。

样例推荐输入为 `{"run_id":"run.1","request":{"constraints":{"budget_max_cents":2000},"preferences":{"requested_dish_id":"D_A_DUCK"}}}`。选择记录须含有效的本人推荐 ID 与餐次 ID，仅传菜品 ID 的 `INTENT_SELECTED` 请求报 `INVALID_INPUT`，账目不变。成功业务响应中的 `NO_FEASIBLE_OPTION` 与执行错误的形态不同，客户端按信封的 `ok` 区分。
