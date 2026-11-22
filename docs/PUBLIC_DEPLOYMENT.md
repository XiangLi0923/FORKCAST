# FORKCAST · 食序 V1.0 公网部署

学生端用 https://aks-forkcast.netlify.app/，运营端用 https://aks-forkcast.netlify.app/ops。两个页面及 API 由同一个 Render Starlette 实例提供，Netlify 将同源请求代理过去。

| 方案 | 路由与静态资源 | 会话与发布 | 采用结果 |
| --- | --- | --- | --- |
| Netlify 静态页面，Render 仅提供 API | 分别配置页面、资产目录和 API 代理 | 仍需验证 Cookie 和 Origin，各站点独立发布容易出现版本错配 | 适合日后需要独立发布两端的场景 |
| Render 提供整个应用，Netlify 代理 | 保留 Starlette 的页面、资产和 API 路由，配置一条 HTTPS 反代 | 页面与 API 一起发布，保留同源路径，需要验证 Netlify 到 Render 的实际转发 | 采用 |

## 在 Render 创建服务

1. 登录 Render 并授权访问 GitHub 公开仓库 XiangLi0923/FORKCAST。使用分支 feat/forkcast-v1-upgrade 创建 Blueprint。若已有适用服务，也可使用 render.yaml 的构建、启动、健康检查和环境配置更新该服务。
2. 在 Render 后台设置 FORKCAST_OPS_TOKEN。使用高熵随机值，通过比赛指定的私密渠道交给有权操作的评委。运营页面加载后才输入，凭据只在该页面内存中使用。凭据持有者可以操作所授权的模拟场所和时钟，避免把凭据放进 GitHub、Netlify 环境或 URL。
3. 等待 Render 部署并打开其 onrender.com 地址的 /health，要求 HTTP 200 且 ready 为 true。Render 自动给出 RENDER_EXTERNAL_HOSTNAME。仅在需要手动指定上游 Host 时，设置 FORKCAST_UPSTREAM_HOST 为准确的 DNS 主机名。

## 更新现有 Netlify 站点

1. 在现有 aks-forkcast 站点的 Build & deploy 设置中连接 XiangLi0923/FORKCAST，将部署分支设为 feat/forkcast-v1-upgrade，仓库根目录为 Base directory。保留原站点和站点域名。
2. 在该站点的环境变量中新增 FORKCAST_RENDER_ORIGIN，仅填 Render 提供的 HTTPS origin，例如 https://forkcast-example.onrender.com。变量应对 Builds 生效。仓库中的 netlify.toml 生成强制代理配置。
3. 部署后实际访问 Netlify 的 /、/ops 和 /health。检查两个页面加载同源资产，并在浏览器验证学生会话 Cookie 有 Secure、HttpOnly、SameSite=Strict、Path=/ 且无 Domain。确认运营端错误凭据被拒，合法凭据可完成读写。用独立 SIMULATION 运行完成一次售罄、人工审批和下窗复验。
4. 执行完毕后记录两端当前部署 SHA。恢复上线前的版本时，使用 Netlify 的已有部署回滚，再按需要选择 Render 的上一版本。比赛 Tag 和 ANS 记录独立保留。

## 数据与安全

- 公网容器使用明确的 HTTPS Origin 以及两个精确主机名。启用 TrustedHost，忽略来自请求的代理头，严格验证回执 Origin。浏览器使用 Netlify HTTPS 入口。
- 免费 Render 方案从 /tmp/forkcast.sqlite3 创建模拟数据库，重复启动可重用同一文件，但服务重启、休眠或重部署可能丢失该文件。首次部署和状态丢失均由已批准的场景夹具初始化。免费实例冷启动可能超过 Netlify 代理 26 秒限制。现场演示前确认 Render /health 已就绪，再打开正式入口。
- 持久化须由账户持有人批准费用，再在 Render 挂载持久磁盘至 /var/data 并设置 FORKCAST_DATABASE=/var/data/forkcast.sqlite3。保持单实例单 worker，重新部署后核对运行 ID 和已写入的供给审计。仅验证健康检查不足以证明数据持久化。
- MCP 工具仍使用本地 stdio。HTTP 审批与 MCP 回执确认是各自的验收路径。本地宿主须能访问同一数据库和授权凭据才能消费其回执。

平台文档：[Render 免费方案](https://render.com/docs/free) · [Render 健康检查](https://render.com/docs/health-checks) · [Netlify 重写与代理](https://docs.netlify.com/manage/routing/redirects/rewrites-proxies/)
