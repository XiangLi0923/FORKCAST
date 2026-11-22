# FORKCAST（食序）V0.3 M4 演示手册

C 端、B 端与 MCP 共享同一个 SQLite 模拟运行和虚拟时钟。MCP 客户端使用 stdio 和协议版本 `2025-11-25`。

## 1. 在 Windows 启动

在项目根目录使用 Python 3.12、PowerShell 7.1+。Node 用于前端测试。依赖安装完成后可离线运行。

```powershell
python -m pip install -r requirements-mcp-demo.txt
$env:FORKCAST_DATABASE = Join-Path $env:TEMP 'forkcast-m4-demo.sqlite3'
$env:FORKCAST_DATA_MODE = 'SIMULATION'
$env:FORKCAST_OPS_TOKEN = Read-Host -MaskInput '设置用于这次本机演示的 OPS 凭据'
python -B -m forkcast --port 8000
```

打开学生端 http://127.0.0.1:8000/ 和运营台 http://127.0.0.1:8000/ops。在运营台输入同一 OPS 凭据。专用空库启动后进入 `CLOSED_LOOP`。C 端自动创建普通试用会话。B 端可以重置为新运行。

## 2. 绑定三类宿主

第二个 PowerShell 终端也进入项目根目录，设置相同的 `FORKCAST_DATABASE` 和 `FORKCAST_DATA_MODE`。执行 OPS 或 EXTERNAL_AGENT 绑定时，`FORKCAST_OPS_TOKEN`、`FORKCAST_OPS_VENUES` 和 `FORKCAST_OPS_RUN_CONTROL` 应与 HTTP 启动配置一致。默认授权 `V_A,V_B` 和运行控制。

| 角色 | 绑定命令 | 验证资料 | 可调用能力 |
| --- | --- | --- | --- |
| EXTERNAL_AGENT | `python -B -m forkcast.mcp_admin bind EXTERNAL_AGENT` | 拥有整个运行控制权限的 OPS | 菜单、推荐、本人重规划 |
| STUDENT | `python -B -m forkcast.mcp_admin bind STUDENT` | 本人现行浏览器会话 Cookie | 菜单、推荐、本人重规划、凭回执提交本人反馈 |
| OPS | `python -B -m forkcast.mcp_admin bind OPS` | 该 OPS 的现有凭据 | 菜单、授权需求、运营提案、凭回执确认备餐 |

STUDENT 绑定时，在 Edge 开发者工具的 Application → Storage → Cookies 定位本人现行学生页的 `forkcast_student`，输入绑定命令的隐藏提示框。命令返回本机宿主凭据。在运行测试宿主的终端通过隐藏输入设置凭据。将命令中的示例角色替换为已绑定角色。

```powershell
$env:FORKCAST_MCP_TOKEN = Read-Host -MaskInput '输入本次绑定返回的宿主凭据'
python -B -m forkcast.mcp_host EXTERNAL_AGENT
```

通用 MCP 客户端使用 [配置示例](docs/m4/mcp-config.example.json)，设定项目根目录和相同数据库路径，由宿主启动 `python -B -m forkcast.mcp_server`。客户端退出会关闭 stdio，测试宿主每次执行完毕自动关闭。HTTP 服务用 Ctrl+C 停止。

撤销使用 `python -B -m forkcast.mcp_admin revoke <ROLE>`，按提示输入对应身份凭据和旧宿主凭据。轮换时先撤销旧宿主，再绑定新宿主。运行重置后重新创建学生会话，并重新绑定三个角色。终端使用完后清除 `Env:FORKCAST_MCP_TOKEN` 和 `Env:FORKCAST_OPS_TOKEN`。

## 3. External Agent 推荐和重规划

```powershell
$base = 'http://127.0.0.1:8000'
$ref = (Invoke-RestMethod "$base/api/v1/runs/current").snapshot
@{ run_id = $ref.run_id; snapshot_id = $ref.snapshot_id } |
  ConvertTo-Json -Compress | python -B -m forkcast.mcp_host EXTERNAL_AGENT get_dining_options
$request = @{
  run_id = $ref.run_id
  request = @{
    constraints = @{ budget_max_cents = 2000; time_budget_min = 45 }
    preferences = @{ requested_dish_id = 'D_A_DUCK' }
  }
} | ConvertTo-Json -Depth 8 -Compress
$rec = $request | python -B -m forkcast.mcp_host EXTERNAL_AGENT recommend_meal | ConvertFrom-Json
```

11:40 首选烤鸭饭 644 分，备选鸡腿饭 480 分。在 B 端推进至 11:41，标记烤鸭饭售罄。

```powershell
@{ run_id = $ref.run_id; previous_recommendation_id = $rec.data.recommendation_id } |
  ConvertTo-Json -Compress | python -B -m forkcast.mcp_host EXTERNAL_AGENT replan_meal
```

首选变为鸡腿饭，`change_reasons` 含 `SOLD_OUT`。外部宿主提交反馈返回 `FORBIDDEN`，请求其他学生的推荐 ID 返回 `NOT_FOUND`。

## 4. 独立运行演示 OPS 闭环

1. 在 B 端重置为新运行并重新绑定宿主。B 端签发一次性演示凭据，C 端加入 DEMO。学生输入预算 ¥20、时间 45 分钟并指名烤鸭饭，开启分享并明确选择烤鸭饭。
2. 可改用 STUDENT MCP 执行上一步选择。在加入 DEMO 后用现行 Cookie 绑定 STUDENT，点击烤鸭饭卡片的「授权 Agent 记录意向」，将参数交给该宿主执行 `submit_feedback`。B 端烤鸭饭应为 P/I/A = 4/6/3。
3. B 端在 11:41 标记烤鸭饭售罄，推进至 11:42。用下列 OPS 命令读需求和生成 W2 提案。B 端点击「生成运营建议」会复用相同版本的提案。

```powershell
$base = 'http://127.0.0.1:8000'
$ref = (Invoke-RestMethod "$base/api/v1/runs/current").snapshot
$windows = @{ run_id = $ref.run_id; reference_window_id = "$($ref.run_id).W1" }
$windows | ConvertTo-Json -Compress | python -B -m forkcast.mcp_host OPS get_demand_insights
$windows.target_window_id = "$($ref.run_id).W2"
$windows | ConvertTo-Json -Compress | python -B -m forkcast.mcp_host OPS generate_ops_recommendations
```

4. 操作员在烤鸭饭提案填写 3，点击「授权本地 Agent」，再点「复制参数」。120 秒内把整段参数交给本人绑定的 OPS 宿主执行。STUDENT 回执使用相同流程，终端角色和工具换成 `STUDENT submit_feedback`。

```powershell
Get-Clipboard -Raw | python -B -m forkcast.mcp_host OPS confirm_ops_action
Set-Clipboard -Value ''
```

5. 刷新 B 端，确认 `CONFIRMED`、计划追加 3 份、原料与产能各预留 3，W1 烤鸭仍为 0。记下确认快照 ID。
6. 推进至 12:00，B 端变为 `APPLIED`，审计依次出现 `OPS_CONFIRMED` 和 `OPS_APPLIED`。通过历史快照读回复查确认时状态。同一个 W2 快照下，C 端和 MCP 菜单的烤鸭饭均为 3 份，重规划结果为烤鸭饭 555 分、鸡腿饭 383 分。
7. 对照运行重复流程并省略确认。到 12:00 烤鸭饭仍为 0 份，首选仍为鸡腿饭，运营状态无确认或生效审计。

## 5. 自动复验与排查

```powershell
python -B -m unittest discover -v
node --test tests/decision_gate.test.mjs
python -B -m tests.demand_http_smoke
python -B -m tests.ops_http_smoke
python -B -m tests.execution_http_smoke
python -B -m tests.mcp_http_smoke
```

| 表现 | 检查 |
| --- | --- |
| 工具列表为空 | 核对绑定角色、同一个数据库绝对路径、现行运行。重置后重新绑定。 |
| `CONFIRMATION_REQUIRED` | 从受信 C 或 B 页面重新签发一个完整动作。保持参数和幂等键原样，在 120 秒内执行。 |
| `SNAPSHOT_CONFLICT` 或 `STALE_PROPOSAL` | 刷新供给与需求，重新生成提案再签发。 |
| `RATE_LIMITED` | 按 `retry_after_seconds` 等待。同一主体的多个宿主共享配额。 |
| `MODE_DISABLED` | HTTP 与 MCP 的配置均使用 `SIMULATION`。 |
| 浏览器无法访问 | 确认 Windows 终端服务正在运行，打开同一台电脑的 `127.0.0.1` 和启动时指定的端口。 |
| 公共索引找不到已锁定版本 | 本次 Windows 复验使用已备齐固定版本的项目 Python 环境。可在现有环境使用 `python -m pip show pydantic starlette uvicorn httpx2 jsonschema mcp` 核对两个依赖文件的版本。 |
