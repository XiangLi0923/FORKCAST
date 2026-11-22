# Windows 本地运行

克隆仓库或解压 GitHub 下载的源码，进入含 requirements.txt 的项目根目录。建议 Python 3.12 x64，PowerShell 5.1 或 7。Node 仅用于前端测试。已安装相同版本依赖的环境可直接启动。

~~~powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-mcp-demo.txt
~~~

终端无法解析 PyPI 时，使用已配置的解释器或已下载核验的 Windows x64 / CPython 3.12 wheelhouse。wheelhouse 未内置。仅启动 C+B 网页可安装 requirements.txt，MCP Host 与完整测试需要 requirements-mcp-demo.txt。

~~~powershell
# Set this to the full path of your pre-verified wheelhouse.
$forkcastWheelhouse = Read-Host 'Verified wheelhouse path'
.\.venv\Scripts\python.exe -m pip --isolated install --no-index --only-binary=:all: --find-links $forkcastWheelhouse -r requirements-mcp-demo.txt
~~~

## 启动与验证

在 Edge 所在的原生 Windows 桌面新开 PowerShell，进入解压目录。若走已配好的系统 Python，第一行用 'python'。启动段每次创建新的比赛演示 SQLite 文件。用一个仅供本次比赛演示的 OPS 值，输入时隐藏，禁止复用个人密码。

~~~powershell
$forkcastPython = '.\.venv\Scripts\python.exe'
$forkcastDemoPort = 8000
if (Get-NetTCPConnection -State Listen -LocalPort $forkcastDemoPort -ErrorAction SilentlyContinue) {
    throw 'Port is in use. Select a free port and update the browser URLs.'
}
$env:FORKCAST_DATABASE = Join-Path $env:TEMP ('forkcast-competition-' + [guid]::NewGuid().ToString('N') + '.sqlite3')
if (Test-Path -LiteralPath $env:FORKCAST_DATABASE) { throw 'Demo database already exists.' }
$env:FORKCAST_DATA_MODE = 'SIMULATION'
$env:FORKCAST_OPS_VENUES = 'V_A,V_B'
$env:FORKCAST_OPS_RUN_CONTROL = '1'
$forkcastSecret = Read-Host 'Set a local demo-only OPS token' -AsSecureString
try { $env:FORKCAST_OPS_TOKEN = [System.Net.NetworkCredential]::new('', $forkcastSecret).Password }
finally { $forkcastSecret.Dispose(); Remove-Variable forkcastSecret }
if (-not $env:FORKCAST_OPS_TOKEN) { throw 'A local demo OPS token is required.' }
Write-Host ('Demo database: '+$env:FORKCAST_DATABASE)
& $forkcastPython -B -m forkcast --port $forkcastDemoPort
~~~

在同一桌面 Edge 打开 [健康检查](http://127.0.0.1:8000/health)、[学生端](http://127.0.0.1:8000/) 和 [运营端](http://127.0.0.1:8000/ops)，保持这个服务窗口运行。预期健康检查为 ready。B 端使用本次启动所设的 OPS 凭据。在新原生 PowerShell 用以下命令单次判定端口是否可达；端口变更须统一修改三个链接和命令。

~~~powershell
Invoke-RestMethod 'http://127.0.0.1:8000/health' -TimeoutSec 5
~~~

## 演示和停止

空库启动进入 CLOSED_LOOP。B 端发放一次性凭据，C 端使用该凭据在 120 秒内加入演示。学生自行开启分享与选餐。想验证双端联动时，在运营端将烤鸭饭标记为售罄，查看学生端的替代推荐。随后在运营端生成并确认下一供餐窗的备餐提案，推进供餐窗口，再核对学生端推荐与运营端审计。

使用结束后，在本次启动服务的终端按 Ctrl+C。服务停止后网页断开，SQLite 文件保留。不要复位或停止原始验收服务，除非负责人已确认。可仅在比赛终端清除凭据环境变量。

~~~powershell
Remove-Item Env:FORKCAST_OPS_TOKEN -ErrorAction SilentlyContinue
~~~
