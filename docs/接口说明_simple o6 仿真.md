# 接口说明：simple o6 仿真

更新日期：2026-10-09。范围为 Connection 与 SIMPLE 独立物理执行服务的接入、配置、调用和核验。当前代码与配置保留 `simple_o7` 名称；实际 O6 物体为校准汤罐。接口已按本仓库实现核对，历史实跑结果见末节，不表示本次重新启动或验收远端服务。

## 1. 两条执行链路

| 项目 | 通用单步抓取 | 固定单罐接待 |
| --- | --- | --- |
| 配置模块 / 后端 | `execution` / `simple_o7` | `reception` / `reception_simple` |
| 大脑适配 | `SimpleO7Adapter`，Planner 生成单步计划 | `BodyAdapter` + `DreamClient` / `VlaClient`，固定接待 SOP |
| HTTP 合同 | `connection/simple-o7/v1`，`/v1/*` | `fq/reception-lan/v1`，`/reception/nav/*` 与 `/reception/vla/*` |
| 当前能力 | `pick_hold_can`：抓取、抬升、稳定持有 | 导航到 table2 → pick → relay2 → relay3 横移 → table1 → place |
| 物理控制 | O6 原生抓取、CuRobo、AMO、手臂力矩 PD | O6 + SONIC v1.1 的连续 MuJoCo 回合 |
| 状态连续性 | 单动作实验，保留末态文件，不支持后续导航和放置 | 同一任务各段共用内存中的物理世界，命令之间仿真时钟暂停 |
| 对象 | `can` 对应 `graspnet1b:2` | 合同 `cola_can_1` 映射为同一校准汤罐，并非可乐模型 |

“通用执行只支持抓取”不限制接待后端的六段流程。两条链路都复用大脑 Runtime 与 Verifier，没有“仿真即通过”分支。接待服务严格校验段序，不支持跳过抓取后直接搬运，也不能把大脑的仅导航 SOP 默认视为本仿真已支持的路径。

服务不调用 GR00T 5555，不把静态文件端口当执行接口。旧 O7 场景仍可保留历史命令查询，其失败与 O6 结果分开；当前两条 O6 链路均未接入实时相机或现场描述。

## 2. 部署与代码归属

服务默认端口 `18770`；基地址以 `config/robot_api.yaml` 的 `backends.simple_o7.url` 为准，SSH 参数为 `.env` 中的 `SIMPLE_SSH_TARGET`、`SIMPLE_REMOTE_DIR`，使用密钥或 `SIMPLE_SSH_PASSWORD`。不要把历史实验地址当作固定机器身份。

| 代码 | 职责 |
| --- | --- |
| `src/brain/adapters/simple_o7.py` | 通用抓取的能力约束、调用、查询与证据转换 |
| `src/brain/adapters/ports.py`、`src/shared/execution_profile.py` | 后端选择、身份保存、接待端点前缀 |
| `src/ops/simple_remote.py` | 远端健康、日志、独立容器管理 |
| `simulation/backends/simple_o7/service/server.py`、`store.py`、`worker.py`、`o6.py` | 服务入口、单步抓取账本及物理证据 |
| 同目录 `reception_http.py`、`reception_store.py`、`reception_worker.py` | 接待协议、持久命令与每任务回合工作进程 |
| 同目录 `reception_task_info.py`、`reception_task.py`、`reception_episode.py` | 语义映射、物理分段、测量与恢复 |
| `scripts/simple_o7_experiment.py` | 隔离账本的通用抓取实验入口 |

将 `simulation/backends/simple_o7/` 部署到 `<REMOTE_WORKSPACE>/connection-simple-o7`，与上游 `code/` 平级。复制 `config.example.json` 为 `config.json`，在远端 `.env` 配置 `SIMPLE_O7_TOKEN`，本机配置相同令牌。上游代码只读挂载，服务的输出、缓存、账本与轨迹写入自己的 `runtime/`，不修改上游源码或管理其他容器。部署细节见[执行服务 README](../simulation/backends/simple_o7/README.md)。

首次创建执行 `bash start.sh`；已有容器执行 `python3 manage.py start` / `python3 manage.py stop`。容器名 `connection-simple-o7`；停止脚本检查未核清命令并封闭新命令入口，存在未结束命令时拒绝停止。面板使用同一管理脚本。`docker stop` 不是业务取消，会中断进程；不能删账本绕过检查。未配置系统自启。

单步账本为 `runtime/commands.sqlite3`，输出为 `runtime/episodes/<episode_id>/`；接待账本为 `runtime/reception.sqlite3`，输出为 `runtime/reception_episodes/<task_id>-<suffix>/`。`docker logs --tail 100 connection-simple-o7` 查看服务日志，物理阶段与轨迹看对应回合目录。

## 3. 通用抓取接口与核验

以下 `/v1/*` 接口除 `/health` 外要求 `Authorization: Bearer <SIMPLE_O7_TOKEN>`。

| 方法与路径 | 作用 |
| --- | --- |
| `GET /health` | 服务与场景清单可读性，不代表物理验收 |
| `GET /v1/scene` | 初始场景模板、版本与能力 |
| `POST /v1/commands` | 提交一条抓取命令 |
| `GET /v1/commands/{command_id}` | 查原命令；不存在返回 404，不重放 |
| `POST /v1/commands/{command_id}/cancel` | 请求停止原命令；受理不等于停止确认 |
| `GET /v1/logs?lines=100` | 有界日志与命令摘要 |

提交字段为 `contract_version`、`command_id`、`task_id`、`step_id`、`action`、`object_id`、`scene_revision`。O6 使用 `pick_hold_can` / `can`；旧 O7 使用 `pick_hold_coke` / `coke`，不得混用。通用计划只允许一项抓取，其他物体、导航、放置、桌面整理和多步计划在提交前拒绝。

同命令同载荷返回原记录，不同载荷返回 409。同一任务不能换新命令或 `-r1` 重置物理场景；需要新实验时使用新任务。SQLite 持久化命令，HTTP 重建只读原记录，不自动重放；容器重启会中断在途工作进程，不能假定物理状态可续跑。

工作进程取消自己创建的子进程组并等待退出后，才确认 `stopped/resources_released`；缺少证据时保留未核清记录。大脑网页或实验脚本的“取消任务”则只结束本地编排，不调用该远端接口；需要停止仿真动作时必须区分这两个操作。

`/v1/scene` 是 `native_task_template`，标记 `new_episode_template_not_live_camera`，不是实时相机。命令 `observation` 来自实际末帧遥测。成功要求身份、时间、终态、停止及资源证据，同时从 `diagnostic_trajectory.npz` 核查：抬升至少 8 cm、手部接触、无桌面支撑、速度不高于 2 cm/s、连续稳定至少 1 s；原生任务通过，最终状态有限，受检穿透不高于 3 mm、倾角不高于 20°，包含物理子步碰撞峰值。单独进程退出或手闭合不构成成功。

## 4. 接待接口与连续回合

基地址自动从 SIMPLE URL 派生，分别添加 `/reception/nav` 和 `/reception/vla`。这些接待接口沿用机器人局域网合同，不校验上述 Bearer 令牌；部署网络访问范围须与此一致。

| 基地址前缀 | 主要路径 |
| --- | --- |
| `/reception/nav` | `GET /health`、`GET /v1/status`、`GET /v1/world`、`GET /total_scene_graph_latest.json`、`POST /v1/navigation/goals`、`GET /v1/commands/{command_id}`、`POST /v1/navigation/cancel` |
| `/reception/vla` | `GET /health`、`GET /v1/vla/control/status`、`POST /v1/vla/tasks`、`GET /v1/vla/tasks/{command_id}`、`POST /v1/vla/tasks/{command_id}/cancel` |

字段合同见[导航接口](接口说明_导航NAV.md)、[操控接口](接口说明_操控VLA.md)。本服务的健康回包标记 `simulation.kind=simple_physics`。检查/快照不支持，返回 `UNSUPPORTED_CAPABILITY`；相机状态为未就绪。世界与关系图提供语义布局，不代表在线视觉感知。

### 场景与物理核验

真机地图坐标不复用。合同里的目标按语义映射到正式房间（`formal_room`）坐标：

| 合同目标 | 仿真对象 | 仿真站位 (x, y, yaw) |
|---|---|---|
| `table_2` | 上游源桌 `table`（中心 0.30, 0.00） | (-0.62, 0.00, 0) |
| `door_1` relay2 → relay3 | 两个中继点，relay3 为保持朝向的横移 | (-0.70, 0.75, π/2) → (-0.90, 0.75, π/2) |
| `table_1` | 上游目标桌 `table2`（中心 0.30, 2.00） | (-0.50, 2.00, 0) |
| `cola_can_1` | `graspnet1b:2` 汤罐（O6 已标定的抓取物体） | 起点 (-0.30, 0.08) |

机器人从 (-0.90, 0.90) 出生。物体是汤罐而不是可乐模型：O6 抓取只对该资产标定过，换物体需要新的抓取标定。

### 命令执行与恢复

一个任务对应一个回合；第一条 `table_2` 导航启动 worker，之后每条命令推进回合中的一段，命令之间仿真时钟暂停，物理状态留在内存中不重置。

| 大脑步骤 | 仿真阶段 | 成功条件（全部来自测量） |
|---|---|---|
| NAVIGATING_TO_TABLE2 | 转身、行走、转向、接近取物桌、站稳 | 位置误差 ≤ 8 cm、朝向误差 ≤ 0.15 rad、0.5 s 平均速度 ≤ 8 cm/s、倾角 ≤ 10° |
| VLA_PICKING | 开手、预接近、接近、闭合、抬升保持、搬运姿态 | 上游抬升门（≥ 8 cm、0.5 s 稳定）+ 手部接触 + 无支撑 |
| NAVIGATING_TO_RELAY2 | 后退、转向、持物行走、站稳 | 同导航条件，且全程仍持物 |
| LATERAL_TO_RELAY3 | 保持朝向横移、站稳 | 同上 |
| NAVIGATING_TO_TABLE1 | 持物行走、转向、搬运避让、粗接近、站稳、修正一步、站稳 | 同上 |
| VLA_PLACING | 扶正、放置、修正、开手、撤手 | 上游放置门：落点误差、桌面支撑、直立、开手、手离罐 ≥ 16 cm、静止 0.5 s |

回包使用与协议模拟相同的字段；导航 `final_xyt` 为 `null`（仿真房间坐标不是地图坐标），测得位姿在 `result.simulation.measured_xyt`。操控证据 `evidence.source=simple_o6_physics`。控制器凭证是对“空闲且存活的回合”的实时观测。

恢复语义：导航段未到位（超时或测得超差）或被暂停时，机器人先站稳，回合保留并回退到该段最后一次行走；大脑「继续」用新的 `command_id`（如 `-r1`）重发同一段。物理失败（摔倒、掉罐、穿透超限）、抓取或放置失败会结束回合，新的尝试需要新任务。

接待幂等仍按原 `command_id` 与完整载荷判断，冲突拒绝。首段 table2 创建回合，其后必须按段序；不允许失败后通过换命令重建同一任务。可恢复导航还需回包 `simulation.retry_allowed=true`、已站稳并保持搬运物体，才可在同一回合重发本段；暂停导航也可能保留回合。操控取消及不可恢复失败结束回合。回合状态在 worker 内存中，服务命令持久化不等于容器重启后物理世界能恢复。

放置末态检查使用上游 `placement_accepted`、目标桌接触、手部无接触及手罐距离；导航检查使用测得位置、朝向、速度和倾角。这些场景测量与真实地图坐标、真机控制器接管是不同证据。仅导航 SOP、人工跳过、跨重启物理续跑不在本接待仿真的既有通过范围内。

## 5. 配置与使用

通用抓取选择：

```yaml
modules:
  execution:
    mode: simulation
    simulation_backend: simple_o7
```

接待选择（与通用执行可独立配置）：

```yaml
modules:
  reception:
    mode: simulation
    simulation_backend: reception_simple
```

在面板「运行环境」选择对应模块后预览并应用。接待可用「仅接待 SIMPLE 仿真」，正式网页发布「开始接待」。已应用配置决定执行端点，不能只编辑 YAML 就认为当前任务已切换；环境应用检查在途任务及健康，失败须查看原始原因。各动作和任务控制语义见[大脑接口](接口说明_大脑%20Brain.md)，容器启停与配置切换是两件事。SIMPLE 不额外依赖 Slaver / Redis。

通用抓取也可使用隔离账本，不切换主服务：

```bash
venv/core/bin/python scripts/simple_o7_experiment.py health
venv/core/bin/python scripts/simple_o7_experiment.py run \
  --ledger data/experiments/simple_o7/trial-001 \
  --task '抓起桌上的罐子并稳定持有'
venv/core/bin/python scripts/simple_o7_experiment.py query \
  --ledger data/experiments/simple_o7/trial-001
```

每次新实验换目录和任务。脚本仍调用共用模型、Planner 和 Runtime；`run` 返回 0 表示成功、2 表示未成功。`resume` 核查原命令，不保证失败动作可重试；`cancel` 走当前大脑本地取消语义，不能用于证明远端已停止。实验默认不自动反思和发布经验，不写主任务账本或已应用环境。

网页若仍显示其他观察后端的画面，不能拿来验证 SIMPLE。接待可用 `service.render_reception` 从本回合 qpos 轨迹导出视频；它是离线回放，不是实时视觉输入。

## 6. 已验证范围

| 日期 | 结果与证据入口 |
| --- | --- |
| 2026-09-24 | [O6 三轮抓取通过与旧 O7 失败](多端联调_20260924_simple%20o6%20抓取接入.md)，固定校准物体，不代表泛化 |
| 2026-09-28 | [正式网页抓取、原命令持久化与 Desk 恢复](多端联调_20260928_仿真通用任务验收.md) |
| 2026-10-08 | [连续接待接入与调参](多端联调_20261008_simple%20o6%20仿真.md)，保存的第四轮大脑任务成功 |
| 2026-10-09 | [正式网页完整接待](多端联调_20261009_simple%20o6%20网页接待.md)，六个动作各一次，任务成功并导出回放 |

已接入完整物理接待且有单次通过证据，不等于稳定成功率、换场景能力或真机验收。新增实验继续按真实日期记录，不在接口文档追加调试流水。
