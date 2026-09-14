# FQPlanner 大脑 Agent × DREAM 导航组 Codex 联调交接

日期：2026-08-21  
适用分支：`robocasa-on-3dgs`  
基线提交：`aa5faf5`（工作区含未提交联调修改，操作前必须先看 `git status`）  
目标：导航组 Codex 通过 SSH 理解、启动、观察 FQPlanner 大脑，并配合完成网页自然语言导航验收。

> 安全边界：本文不包含任何密码、Token 或 API Key。SSH 认证信息线下提供。导航组 Codex 不得读取、打印或复制项目 `.env`。未经现场人员明确授权，不得开启大脑真机动作安全门，不得发送导航、细检或取消命令。

## 1. 主机与网络

| 角色 | 主机/系统 | 地址 | 用途 |
|---|---|---|---|
| FQPlanner 大脑 | Windows `DESKTOP-AKGJQCO` | `192.168.0.108/24` | Master、Slaver、Web、DREAM 适配器 |
| DREAM 导航主机 | Linux | `192.168.0.185` | Agent HTTP、9882、Rerun、真实导航 |

大脑 SSH：

```text
host: 192.168.0.108
port: 22
user: Administrator
shell: PowerShell 7
repository: C:\Program Files (x86)\brain
python: C:\Program Files (x86)\brain\.venv\Scripts\python.exe
```

Windows OpenSSH `sshd` 已设置为自动启动，并监听 IPv4/IPv6 的 22 端口。认证方式由大脑负责人线下提供。

SSH 登录后第一步：

```powershell
Set-Location -LiteralPath 'C:\Program Files (x86)\brain'
git branch --show-current
git rev-parse --short HEAD
git status --short
```

工作区不是干净状态；禁止执行 `git reset --hard`、`git checkout -- .` 或批量覆盖用户修改。

## 2. 端口拓扑

```text
用户浏览器 :8888
  → Deploy Web :8888
  → Master :5000
  → Redis :6379 (仅 127.0.0.1)
  → Slaver/FQrobot (Redis pub/sub + 本地 MCP 子进程)
  → robot_api
  → serve_dream :5006
  → DREAM Agent HTTP 192.168.0.185:8001
  → DREAM 9882 / Gateway / Token / 真机导航
```

| 地址 | 服务 | 所属方 | 说明 |
|---|---|---|---|
| `192.168.0.108:22` | OpenSSH | 大脑 | 导航组 Codex 登录入口 |
| `127.0.0.1:6379` | Redis | 大脑 | Agent 注册、任务和状态；不对局域网开放 |
| `0.0.0.0:5000` | Master | 大脑 | 规划、任务队列、经验接口 |
| `0.0.0.0:5006` | `serve_dream` | 大脑 | FQPlanner 与 DREAM 8001 的协议适配和安全门 |
| `0.0.0.0:8888` | Deploy Web | 大脑 | 用户唯一正式任务下发入口 |
| `192.168.0.185:8001` | Agent HTTP | 导航组 | 世界、状态、异步导航、相机、细检 |
| `192.168.0.185:9876` | Rerun Web | 导航组 | 感知/关系图可视化 |
| `192.168.0.185:9877` | Rerun gRPC | 导航组 | Rerun 数据源 |
| `192.168.0.185:9882` | DREAM Web/安全状态 | 导航组 | 定位审核、Gateway/Token、现场安全状态 |

仿真端口 `5001`（RoboCasa）和 `5002`（3DGS）不属于本轮真机导航栈；`active_backend=dream` 时不要同时把仿真作为动作后端启动。

## 3. 当前配置真值

### 3.1 大脑后端

文件：`robot_api/config.yaml`

```yaml
active_backend: dream
backends:
  dream:
    enabled: 1
    provide_state: 1
    accept_action: 1
    required: 1
    url: http://127.0.0.1:5006
    timeout: 200
```

独立 `navigation` 路由保持关闭，导航由 active DREAM 后端的 `/nav` 处理。

### 3.2 DREAM 适配器

文件：`serve_dream/config.yaml`

```yaml
exchange:
  mode: http
  protocol: agent_http_v1
  base_url: http://192.168.0.185:8001
  nav_url: http://192.168.0.185:8001
nav:
  result_timeout_sec: 1800
  result_poll_interval_sec: 0.5
safety:
  real_control_enabled: false
  ack_env: FQPLANNER_DREAM_REAL_CONTROL_ACK
  ack_value: AGENT_HTTP_NAVIGATION_AUTHORIZED
```

`real_control_enabled=false` 是默认安全状态。此时 GET/read-only 接口可用，`POST /nav`、`POST /inspection`、`POST /nav/cancel` 必须返回 HTTP 403。

真机动作需要两个条件同时满足：

1. `safety.real_control_enabled: true`；
2. `serve_dream` 进程环境变量：

```text
FQPLANNER_DREAM_REAL_CONTROL_ACK=AGENT_HTTP_NAVIGATION_AUTHORIZED
```

导航组 Codex 不得自行满足这两个条件；必须由现场负责人明确授权。

## 4. 关键代码与资料

| 路径 | 作用 |
|---|---|
| `serve_dream/service/server.py` | 大脑侧 DREAM HTTP 适配、动作安全门、状态代理 |
| `serve_dream/adapters/agent_http.py` | 8001 异步命令客户端；202 接收后轮询 command_id |
| `serve_dream/reviewed_targets.py` | 自然语言 table1/table2 别名解析 |
| `serve_dream/dream_navigation_sop.yaml` | table2/table1 审核点和窄门横移 SOP |
| `serve_dream/network_preflight.py` | 只读网络、世界、状态和关系图预检 |
| `serve_dream/test_agent_http.py` | 8001 异步合同离线测试，不接触真机 |
| `serve_dream/integration/20260821/` | 导航组交付的地图、关系图和原始接口说明 |
| `slaver/robot/module/base.py` | `navigate_to_target`；DREAM 模式解析审核目标 |
| `robot_api/runtime.py` | 语义动作到 active backend 的路由 |
| `master/run.py` | Master HTTP 服务 |
| `deploy/run.py` | 网页控制台及 Master 代理 |
| `serve_dream/start_agent_stack.ps1` | Redis、Master、Slaver、Deploy 一键重启 |
| `serve_dream/restart_agent_bridge.ps1` | 重启 5006 适配器并注入 ACK 环境变量 |

## 5. 服务启动顺序

### 5.1 只读预检 DREAM

该脚本只发送 GET，不会让机器人移动：

```powershell
& '.\.venv\Scripts\python.exe' -X utf8 '.\serve_dream\network_preflight.py' `
  --base-url 'http://192.168.0.185:8001' --timeout 8
```

成功标准：

- TCP 8001 可连接；
- `/v1/world` 的 `frame_id=map`；
- `interface_online=true`；
- `world_state_available=true`；
- 在线关系图包含 `table_2`；
- 穿门验收前还必须包含 `door_1`。

### 5.2 启动 5006 适配器

安全门关闭时也可以启动。现有脚本会给进程注入 ACK，但配置中的 `real_control_enabled=false` 仍会阻止动作：

```powershell
powershell -ExecutionPolicy Bypass -File '.\serve_dream\restart_agent_bridge.ps1'
```

确认：

```powershell
Test-NetConnection 127.0.0.1 -Port 5006
Invoke-RestMethod 'http://127.0.0.1:5006/dream/status' | ConvertTo-Json -Depth 8
Invoke-RestMethod ([uri]::EscapeUriString(
  'http://127.0.0.1:5006/reviewed_target/导航到table2')) |
  ConvertTo-Json -Depth 8
```

### 5.3 启动大脑核心栈

`start_agent_stack.ps1` 会停止并重启匹配到的 Redis、Master、Slaver、Deploy 进程。只有确认可以重启当前任务时才运行：

```powershell
powershell -ExecutionPolicy Bypass -File '.\serve_dream\start_agent_stack.ps1'
```

预期端口：

```powershell
$ports = 5000,5006,6379,8888
Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
  Where-Object { $ports -contains $_.LocalPort } |
  Select-Object LocalAddress,LocalPort,OwningProcess |
  Sort-Object LocalPort
```

预期应用状态：

```powershell
Invoke-RestMethod 'http://127.0.0.1:5000/system_status'
Invoke-RestMethod 'http://127.0.0.1:5000/robot_status'
Invoke-RestMethod 'http://127.0.0.1:8888/api/auto_tools'
```

`robot_status` 应出现 `FQrobot`；工具列表应包含 `navigate_to_target`。

## 6. 正式任务交互规则

用户已明确规定：真实任务必须从大脑网页端下发，不把直接调用 8001 当作正式验收。

正式入口：

```text
http://192.168.0.108:8888/
```

大脑本机也可用：

```text
http://127.0.0.1:8888/
```

自然语言任务链：

```text
网页输入任务
→ Deploy POST /publish_task
→ Master 规划子任务
→ Redis 发布给 FQrobot
→ Slaver 选择 navigate_to_target
→ reviewed_targets 解析审核目标
→ robot_api(active_backend=dream)
→ serve_dream:5006 POST /nav
→ DREAM:8001 POST /v1/navigation/goals
→ 轮询 /v1/commands/{command_id}
→ 终态返回 Slaver → Master → 网页
```

调试时可以读取 API，但除非现场负责人明确要求，不得用 API 绕过网页发布真实任务。

## 7. table2 首条验收任务

网页自然语言：

```text
导航到table2
```

别名解析也支持：`table_2`、`桌子2`、`二号桌`、`2号桌`。

必须生成的审核合同：

```json
{
  "target_id": "table_2",
  "frame_id": "map",
  "goal_xyt": [
    0.9948137550501258,
    1.402057782965935,
    -0.3193204258080712
  ],
  "yaw_unit": "radians",
  "motion_mode": "forward_path",
  "require_final_orientation": true
}
```

只读检查目标解析：

```powershell
Invoke-RestMethod ([uri]::EscapeUriString(
  'http://127.0.0.1:5006/reviewed_target/导航到table2')) |
  ConvertTo-Json -Depth 8
```

8001 正式 payload 还应带有唯一 `command_id`、`task_id` 和 `leg_index`。同一个 `command_id` 重复 POST 是幂等查询；失败重试必须换新 ID。

## 8. 8001 状态语义

只读：

```powershell
$s = Invoke-RestMethod 'http://192.168.0.185:8001/v1/status'
$s | Select-Object interface_online,world_state_available,motion_ready,
  motion_blockers,localization_initialized,localization_approved,
  gateway_ready,token_ready,active_command_id
```

重要区别：

- `interface_online=true`：Agent HTTP 可交互；
- `world_state_available=true`：地图/关系图可读取；
- `motion_ready=false`：当前真实运动门尚未全部打开，不等于网络断开；
- `motion_blockers`：例如 `gateway_not_ready`、`token_not_ready`；
- `active_command_id` 非空：已有活动命令，禁止再提交第二条；
- HTTP 202/`accepted`：只表示命令已接收，绝不是任务成功；
- 只有 `state=succeeded` 且 `result.success=true` 才算到达；
- `failed`、`cancelled`、`stalled`、`timed_out`、`aborted` 必须原样上报。

按当前 DREAM 合同，命令可以在接口在线、世界状态可用时先排队，之后等待现场 Approve、Plan、Arm、Gateway/Token 和 Execute。大脑适配器超时为 1800 秒。不得把等待安全门解释为通信失败，也不得伪造成功。

## 9. 窄门横移 SOP

单一真值源：`serve_dream/dream_navigation_sop.yaml`。在线时优先核对关系图：

```text
door_1.evidence.navigation_contract
```

穿门必须拆成三条独立命令：

1. 门外接近：

```json
{
  "target_id": "door_1",
  "goal_xyt": [3.653832809989912, 6.334157606471382, 2.7083544235256447],
  "motion_mode": "forward_path",
  "require_final_orientation": true
}
```

2. 等待第一段 `succeeded`，使用相同 `task_id`、递增 `leg_index` 横移出门：

```json
{
  "target_id": "door_1",
  "goal_xyt": [4.18386295816721, 7.799881831115017, 2.7083544235256447],
  "motion_mode": "lateral_path_aligned",
  "require_final_orientation": true
}
```

3. 等待横移成功，另发 `forward_path` 到 table1：

```json
{
  "target_id": "table_1",
  "goal_xyt": [3.0873798986272165, 8.279995338440145, 1.175238157458919],
  "motion_mode": "forward_path",
  "require_final_orientation": true
}
```

禁止事项：

- 不得把三个点合成普通折线路径；
- `lateral_path_aligned` 不得用于 `door_1` 以外的目标；
- 不得跳过门外接近段；
- 不得在上一段未成功时发送下一段。

## 10. 大脑侧主要 HTTP 接口

### Master `:5000`

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/system_status` | Master/Redis 基本状态 |
| GET | `/robot_status` | 已注册机器人状态 |
| GET | `/api/task_status` | 当前任务及子任务结果 |
| POST | `/publish_task` | 调试发布；正式真机验收用网页 |
| GET | `/api/experiences` | 经验库 |

### Deploy Web `:8888`

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 大脑任务网页 |
| POST | `/publish_task` | 网页任务发布代理 |
| GET | `/api/task_status` | 网页轮询任务状态 |
| GET | `/api/auto_tools` | Redis 中注册工具 |
| GET | `/api/robot_status` | 机器人状态代理 |

### serve_dream `:5006`

| 方法 | 路径 | 类型 | 说明 |
|---|---|---|---|
| GET | `/health` | 只读 | 适配器、图和地图状态；可能因下载地图稍慢 |
| GET | `/dream/world` | 只读 | 代理 8001 世界清单 |
| GET | `/dream/status` | 只读 | 代理 8001 状态 |
| GET | `/objects` `/fixtures` `/scene` | 只读 | 关系图适配 |
| GET | `/map_data` | 只读 | ROS map 转网格 |
| GET | `/reviewed_target/<name>` | 只读 | 自然语言审核目标解析 |
| GET | `/door_contract` | 只读 | 在线 `door_1` 合同；缺失时返回 SOP 回退和警告 |
| GET | `/commands/<command_id>` | 只读 | 查询 DREAM 命令 |
| POST | `/nav` | 动作 | 导航；本地安全门保护 |
| POST | `/inspection` | 动作 | VLM 细检；本地安全门保护 |
| POST | `/nav/cancel` | 高风险动作 | 软件锁定并撤销定位批准，不得随意调用 |

## 11. 日志与进程检查

启动脚本输出日志：

```text
.log\redis_dream.stdout.log
.log\redis_dream.stderr.log
.log\master_dream.stdout.log
.log\master_dream.stderr.log
.log\slaver_dream.stdout.log
.log\slaver_dream.stderr.log
.log\deploy_dream.stdout.log
.log\deploy_dream.stderr.log
serve_dream\agent_bridge.stdout.log
serve_dream\agent_bridge.stderr.log
```

读取末尾：

```powershell
Get-Content -LiteralPath '.\.log\slaver_dream.stderr.log' -Tail 100
Get-Content -LiteralPath '.\.log\master_dream.stdout.log' -Tail 100
Get-Content -LiteralPath '.\serve_dream\agent_bridge.stderr.log' -Tail 100
```

按命令行识别项目进程：

```powershell
Get-CimInstance Win32_Process |
  Where-Object {
    $_.Name -match 'python|redis' -and
    $_.CommandLine -match 'master\\run.py|slaver\\run.py|deploy\\run.py|serve_dream\\main.py|redis-server'
  } |
  Select-Object ProcessId,Name,CommandLine
```

## 12. 常见问题

### Redis `Error 10061 connecting to 127.0.0.1:6379`

Redis 未监听或被重启，Slaver 会注册失败并退出。不要只重启 Slaver；运行 `start_agent_stack.ps1`，确保顺序为 Redis → Master → Slaver → Deploy。

### Slaver 显示网络检测失败并退回 TF-IDF

这是工具匹配器对外部网络的降级，不代表 DREAM `192.168.0.185:8001` 不通。只要后续显示 `Connected to robot with 13 tools` 且 Redis 注册成功即可。

### `/api/belief` 返回 500

DREAM 真机后端目前不提供 RoboCasa 的 `scene_state/belief`，不影响 table2 导航验收。应关注 `/objects`、`/fixtures`、`/dream/status`。

### `/base_status` 返回 503

只有 DREAM `/v1/status` 提供 `robot_xyt`、`pose_xyt` 或 `current_pose` 时才能构造底盘位姿。没有该字段不影响按审核目标下发导航，但网页可能无法显示实时底盘坐标。

### `motion_ready=false`

先查看 `motion_blockers`。如果 `interface_online/world_state_available=true`，这是安全门等待，不是 HTTP 断线。不要循环重发相同或新的任务。

### 8001 不通但 SSH 22 正常

在 DREAM 主机检查：

```bash
ss -lntp | grep -E ':(8001|9876|9882)\b'
curl -fsS http://127.0.0.1:8001/v1/world
```

8001 必须监听可被局域网访问的地址，而不是只监听 `127.0.0.1`。

## 13. Codex 操作红线

1. 不读取、不打印、不上传 `.env`、API Key、SSH 凭证。
2. 不直接调用 9882、5556、ROS goal/cmd_vel 或 Token；Agent 组只调用 8001。
3. 不启动第二套 RealSense 驱动；相机通过 8001 只读快照共享。
4. 不在有活动命令时提交第二条命令。
5. 不把 HTTP 202、`accepted`、`waiting_safety_ready`、`navigating` 当成功。
6. 不自行开启 `real_control_enabled`，不自行设置动作 ACK。
7. 不随意调用 `/nav/cancel`；取消会软件锁定并撤销定位批准。
8. 不修改地图 `origin/resolution`，PGM 与 YAML 必须成对使用。
9. 不覆盖在线关系图；`door_1` 只用于审核导航合同。
10. 不清理或重置当前脏工作区，不删除未知日志/数据。

## 14. 最小联调验收清单

### 只读阶段

- [ ] SSH 可登录 `Administrator@192.168.0.108`；
- [ ] 8001 `/v1/world` 返回 `frame_id=map`；
- [ ] `/v1/status` 返回 `interface_online=true`；
- [ ] `/v1/status` 返回 `world_state_available=true`；
- [ ] 在线关系图包含 `table_2` 和 `door_1`；
- [ ] 5006 `/reviewed_target/导航到table2` 返回审核点与 `forward_path`；
- [ ] Redis 6379、Master 5000、serve_dream 5006、Web 8888 均监听；
- [ ] Master `robot_status` 包含空闲 `FQrobot`；
- [ ] Web `/api/auto_tools` 包含 `navigate_to_target`。

### 真机动作阶段（现场负责人明确授权后）

- [ ] 物理急停、肩带和扶持条件满足；
- [ ] 9882 定位已审核；
- [ ] 大脑本地动作安全门明确开启；
- [ ] 网页仅提交一次“导航到table2”；
- [ ] 8001 记录唯一 command_id；
- [ ] 状态按 accepted/等待安全门/navigating/终态演进；
- [ ] 最终仅以 `succeeded + result.success=true` 判成功；
- [ ] 网页显示与 8001 命令终态一致；
- [ ] 验收结束后重新关闭 `real_control_enabled`。

## 15. 当前观察快照（2026-08-21 14:48）

```yaml
brain:
  host: DESKTOP-AKGJQCO
  ip: 192.168.0.108
  ssh: running
  active_backend: dream
  serve_dream_mode: http
  local_real_control_enabled: false
dream:
  base_url: http://192.168.0.185:8001
  interface_online: true
  world_state_available: true
  localization_initialized: true
  localization_approved: true
  motion_ready: false
  motion_blockers:
    - gateway_not_ready
    - token_not_ready
  active_command_id: ""
```

该快照只用于定位上下文，不能替代每次操作前重新读取 `/v1/status`。
