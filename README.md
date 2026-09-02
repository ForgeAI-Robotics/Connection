# Connection

**把任务入口、大脑编排、DREAM 导航和 VLA 操作接成可追踪、可验证的协同链路。**

本仓库是当前大脑工作区的**脱敏源码快照**：包含 Master、Slaver、Web 控制台、DREAM/VLA 客户端、飞书入口，以及仿真、示教与记忆相关源码。它不是包含模型、真实地图和所有机器人驱动的整机镜像。

本文件是仓库的统一总纲。历史交接文档和现场记录不随版本发布；历史记录中曾经成功的步骤，也不会被写成“整个系统已经验收”。

> **安全默认值：**真机接待关闭、物理机器人桥接关闭、飞书 `dry_run`。示例地址与路线均为人工合成，不能直接用于真实机器人。不要把本仓库的 HTTP 控制端口暴露到公网。

## 1. 系统边界与两条执行路径

| 组件 | 职责 | 入口 / 默认端口 |
| --- | --- | --- |
| Web / Deploy | 提交任务、预检、状态与结果展示、示教界面 | `deploy/run.py` · 8888 |
| Master | 任务路由、通用规划、固定真机接待编排、任务状态 | `master/run.py` · 5000 |
| Slaver | MCP 工具执行、语义动作适配、机器人注册 | `slaver/run.py` |
| Redis | 通用 Master–Slaver 消息与状态协作 | 6379 |
| 飞书桥接 | 消息去重、运动任务二次确认、调用大脑、回传状态 | `integrations/feishu/run.py` |
| DREAM 兼容适配 | 文件交换 / Agent HTTP 适配到统一 Robot API | `serve_dream/main.py` · 5006 |
| DREAM 导航服务 | 世界状态、定位审核、导航命令与结果 | **外部部署** · 8001；操作页面 9882 |
| VLA Brain Bridge | pick/place 接口、动作交接、证据与持有状态 | **外部部署** · 8091 |
| VLA action relay | 导航动作转发与顺序释放 / 归还 | **外部部署** · 5556 |
| NX / SONIC | 机器人低层控制、相机、反馈、手桥 | **外部部署** · 5555 / 5557 |

```text
网页 / 飞书 / 可选语音
            │ HTTP
            ▼
      Deploy :8888 → Master :5000
                       ├─ 通用任务 → Redis ↔ Slaver → MCP / Robot API
                       └─ 固定真机接待 → reception_real
                                            ├─ HTTP → DREAM :8001
                                            └─ HTTP → VLA :8091

DREAM :5556 → VLA 主机的 sequential relay → VLA 主机 :5556 → NX SONIC
                         释放端口 ↔ VLA pick/place 动作客户端
```

固定真机接待由 **Master 内的 `ReceptionRealRunner` 直接调用 DREAM/VLA**，不是每一步都经 Slaver。通用任务继续走既有 Redis/MCP 工具路径。两条路径不能混写成同一个状态机。

大脑不直接发送 5556 动作帧、不操作 SONIC 接管、不代替现场定位审核。SSH 用于部署和管理，不是高层任务协议；高层是 HTTP/JSON，底层采用 ROS2 交接信号与 ZeroMQ 动作流。

## 2. 当前真机接待合同

协议版本：`fq/reception-lan/v1`。主线采用导航侧整合后的 **navigation-only** 方案：DREAM 专注导航，不启动旧 `/nav_done → 自动 VLA` 高层流程。

```text
只读预检
 → table2 导航成功
 → 跳过 DREAM inspection（navigation-only）
 → VLA pick
 → 策略停止、5556 归还导航
 → relay2 门外接近
 → relay3 横移穿门
 → table1 导航成功
 → VLA place
 → 策略停止、5556 归还导航
 → 最终结果
```

- 一个顶层 `task_id` 串起各段 `command_id`；导航分段维持 `leg_index` 和 `route_phase`。
- `goal_xyt` 在 `map` 坐标系下，yaw 为弧度。门外接近与横移出门必须是两条命令，不能合成普通折线路径。
- 导航 `202/accepted` 只是接收，不是到达。必须等到终态，并验证成功、目标身份和停止证据。
- VLA 请求带上真实导航成功凭证。动作结束后必须验证策略停止及导航端口归还，才允许下一段导航。
- POST 结果不确定时，只查询原 `command_id`；不能通过新 ID 重发来掩盖网络不确定性。
- 服务重启发现未完成任务时进入 `RECOVERY_REQUIRED`；不自动重放动作。
- 原方案中的“细检 + 相机交接”仍有代码分支，但 navigation-only 模式使用 `dream_inspection_enabled: false`。相机由下游已有部署管理，大脑不抢占驱动。

### 动作仲裁的准确含义

当前下游方案是 **sequential relay 的顺序端口交接**：导航时 relay 绑定 VLA 主机的 5556；pick/place hook 请求释放并取得端口；结束后归还给 relay。它不是所有动作生产者同时连接的常驻多输入 mux。

NX 始终订阅 VLA 主机的统一 5556 出口。VR、Pico、数据采集或其他动作发布器不能同时占用该端口。仅看到 TCP 监听也不能证明监听者就是正确 relay。

### `hand_state_only` 不等于物体搬运验收

默认结果策略为 `hand_state_only`，抓取/放置照片判真分别关闭：

- pick：左手先 OPEN，再达到稳定 CLOSED；默认阈值 0.70，至少 3 帧。
- place：左手先 CLOSED，再稳定 OPEN；默认阈值 0.30，至少 3 帧。
- 两者均要求策略停止、5556 归还，并校验任务和物体身份。
- 该证据主要来自手部目标状态，**不证明物体真实在手或落在目标桌面**。
- 全链在该策略下完成使用 `COMPLETED_HAND_STATE_ONLY`，不能对外等同于严格物体验收。
- 需要严格验收时，必须单独启用并验证照片/物体证据链；不能仅把结果字段改成 `true`。

下游 place 使用非交互动作适配器，但 Bridge 仍可能配置 `waiting_operator_approval`。非交互执行脚本不意味着可以跳过 Bridge 的现场批准。

## 3. 当前代码地图

| 目录 / 文件 | 内容 |
| --- | --- |
| `master/agents/` | 通用规划、接待任务路由、重复任务保护、门段路由约束 |
| `master/sop/reception_real.py` | 固定真机编排、只读预检、路由合同与动作结果校验 |
| `master/integrations/` | DREAM/VLA HTTP 客户端、连接不确定性处理、可选 VLM/LLM 验证 |
| `master/sop/reception_store.py` | 原子状态文件、事件和验证记录 |
| `master/sop/` 其他模块 | mock 补货闭环、SOP、工作点生成、视频示教、记忆与反思 |
| `slaver/`、`agent/` | MCP 工具、感知、语义动作、工具匹配与 Redis 协作 |
| `robot_api/` | 多后端选择、统一状态与动作接口 |
| `deploy/` | 8888 前端、任务发布/预检代理、状态和时间线 |
| `integrations/feishu/` | 飞书长连接、确认卡片、SQLite 审计、任务进度回传 |
| `serve_dream/` | Agent HTTP 与旧文件交接适配、审核路线解析、离线测试 |
| `serve_desk/` | 无真实机器人依赖的桌面 mock 服务 |
| `serve/`、`serve_3dgs/` | MuJoCo / 3DGS 仿真与场景工具源码；资源需另配 |
| `nav2/`、`docker/nav2/` | 地图/工作点算法与 ROS2 导航桥 |
| `serve_real/`、`services/` | 其他硬件桥接、ACT/pi0.5 推理服务源码；默认禁用真机 |
| `teleop/`、`nx_voice/` | 采集、训练、推理与语音入口；需要独立环境和授权 |
| `assets/` | 机器人定义与资源处理源码；不包含完整 mesh/点云 |
| `PBD_AG_大脑端轻量客户端_v0.1.0/` | PBD 客户端示例与测试；原二进制交付包不包含在本仓库 |
| `scripts/` | 本地配置初始化、发布隐私/语法检查 |

外部 DREAM 完整导航工程、VLA Bridge/hook/relay、GR00T 权重、SONIC、手桥和相机驱动**不在本地工作区源码范围内**。本仓库不包含那些远端工程，也没有用本地旧 `.vla_patch` 代替导航侧最终版本。

## 4. 配置与私有信息

先创建本地配置副本：

```bash
python scripts/bootstrap_local.py
```

该操作不覆盖已有文件、不启动服务。生成的文件被 Git 忽略：

| 公共模板 | 本地文件 |
| --- | --- |
| `.env.example` | `.env` |
| `master/config.example.yaml` | `master/config.yaml` |
| `slaver/config.example.yaml` | `slaver/config.yaml` |
| `robot_api/config.example.yaml` | `robot_api/config.yaml` |
| `serve_real/config.example.yaml` | `serve_real/config.yaml` |
| `serve_dream/config.example.yaml` | `serve_dream/config.yaml` |
| `serve_dream/dream_navigation_sop.example.yaml` | `serve_dream/dream_navigation_sop.yaml` |

`.env` 中只在本地填写 `CLOUD_API_KEY`、`VLM_API_KEY`、实际 DREAM/VLA 地址及可选飞书凭证。Master/Slaver 配置加载器支持 `${ENV_VAR}`；其他 YAML 加载器并非都支持此语法，不要盲目把所有字段改成变量表达式。

公开地址约定：`192.0.2.10` 为大脑、`.20` 为 DREAM、`.30` 为 VLA、`.40` 为 NX。它们只是文档地址。站点用户名、个人路径、真实 IP、现场地图与日志已移除或替换。

**路线也已合成化。** 发布版 `reception_real.py`、测试和导航 SOP 中的数值只用于展示合同结构。部署新现场必须由导航方审核真实路线，重新对齐大脑的固定路线校验和下游关系图；不得把示例坐标下发给机器人。

### 明确不发布

- 旧 README、交接文档、会话记录和终端日志；本 README 是唯一展示文档。
- `.env`、SSH 密钥、账号密码、API/飞书凭证、生产配置。
- 任务持久状态、`holding`、飞书 SQLite、用户消息、个人示教记忆、现场截图/视频。
- 真实地图/关系图、生成场景、大模型权重、虚拟环境、编译产物和大二进制资源。
- 绑定特定 Windows 用户目录、或会广泛杀进程的旧启动/清理脚本。
- 原工作区 `.git` 与旧提交历史；只发布本次审核后的干净快照。

保留原有 `LICENSE` 和源码版权声明。第三方模型、机器人资源和外部工程仍受各自许可约束，不随本仓库自动授权。

## 5. 本地开发与离线测试

建议把大脑/API、GPU 仿真、ROS2/SONIC、VLA 推理分开配置环境。

```bash
python -m venv .venv
# 激活该虚拟环境后执行：
python -m pip install -r requirements-core.txt
python scripts/bootstrap_local.py
```

`requirements-core.txt` 面向大脑/API 开发。既有 `requirements.txt` 与 `pyproject.toml`/`uv.lock` 保留为仿真/训练依赖参考，后者限定 Linux x86_64、Python 3.10 和特定 CUDA/渲染包；它们不是所有平台通用的一键安装清单，也不保证无需补充 RoboCasa/ROS2 等外部依赖。

最小离线合同测试只需 Python、PyYAML、Flask；飞书测试还需该目录依赖：

```bash
python -m pip install PyYAML Flask
python -m pip install -r integrations/feishu/requirements.txt
python scripts/bootstrap_local.py
python -m unittest master.tests.test_reception_real master.tests.test_dream_route_policy -v
python -m unittest serve_dream.test_agent_http -v
python serve_dream/selftest.py
python -m unittest discover -s integrations/feishu/tests -v
python scripts/check_publication.py
```

上述测试使用临时目录、合成数据、fake HTTP/transport，不发送真机任务。`master/tests/live_dream_navigation.py` 是现场工具，**不属于这组离线测试，不能在 CI 自动运行**。

真实地图、mesh、权重未发布，所以不能把合同测试通过理解为仿真/GPU/真机全栈已安装。生成仿真场景前须自行恢复对应资源和依赖。

## 6. 服务启动与三端联调

### 大脑开发服务

先确保本地配置已初始化、Redis 可用、模型凭证已在本地配置。分别在终端运行：

```bash
redis-server --bind 127.0.0.1
python serve_desk/main.py
python master/run.py
python slaver/run.py
python deploy/run.py
```

控制台：[http://127.0.0.1:8888](http://127.0.0.1:8888)。默认 `RECEPTION_MODE=mock`，不会进入 G1 真机接待。不要在未经核验的共享 Redis 上执行清库；公共模板将 `collaborator.clear` 设为 `false`。

Windows 通过 SSH 后台启动服务时，子进程可能随 SSH 会话结束退出。生产部署应使用现场已审核的进程管理器/任务计划；本仓库不导出含个人路径的计划任务或凭证。不要用全局 `kill python` 式命令清场。

### 真机启动顺序（必须人工审核）

1. 现场完成支撑、防碰撞和实体急停准备；确认机器人没有被 VR/Pico/其他组控制。
2. VLA 工作站启动受管 relay 与 8091；检查唯一 5556 所有者。
3. NX 使用 persistent SONIC，动作入口固定为 VLA 工作站；相机和手桥按已有部署启动。
4. DREAM navigation-only 启动；完成现场接管、肩带确认和定位 Approve。
5. 检查完整运动门、图合同、相机/手桥实际运行状态，而非只检查端口。
6. 审核本地真机配置，启动大脑服务，再通过只读预检。
7. 提交一次新任务并跟踪至终态；place 若要求人工批准，必须由现场人员处理。

现场最终受管入口名为 `g1_agent_vla_integrated_start.sh`（导航侧）和 `run_agent_vla_runtime.sh`（VLA 侧）。它们属于外部部署，**不在本仓库**；不要拿旧自动编排脚本替代。`resume` 只能用于已确认底层所有权健康且 DREAM 会话不存在的情形，不能对未知状态盲目重启。

### 放行条件与失败处理

- DREAM：`interface_online`、`world_state_available`、`localization_approved`、`navigation_transport_ready`、`motion_ready` 均满足，无活动命令。
- VLA：`service_ready=true`、无活动命令、`policy_running=false`、`recovery_required=false`、`holding=null`，且导航端口已经归还。
- 照片验证关闭不代表策略不需要相机：GR00T 原推理本身仍依赖真实图像输入。
- `GATEWAY_NOT_READY`、`TOKEN_NOT_READY`、`sensor_fresh` 失败、无法读取状态、监听者不明，均不能当作可运行。
- 网络异常先验证同一局域网、路由、服务身份和数据新鲜度；不能只延长超时或反复点击“开始接待”。
- 物体人工取下后，先核对现场，再通过对应服务的受控维护流程备份并修正持有状态。不能删除整份历史来掩盖未完成动作。
- 任务取消、进程停止和失去供电不是同一件事；本仓库不承诺网页关闭或飞书停止跟踪会停止机器人。

## 7. HTTP 接口与任务身份

| 大脑入口（8888 转发至 5000） | 用途 |
| --- | --- |
| `POST /api/task_preflight` | 只读检查未来任务，不创建任务、不发送动作 |
| `POST /publish_task` | 提交一个顶层任务 |
| `GET /api/task_status` | 当前任务、阶段、下游命令与结果 |
| `GET /robot_status`（Master） | 机器人注册与状态 |

请求体示意（不要对未审核真机直接执行）：

```json
{"task":"开始接待", "task_id":"example-reception-0001", "refresh":true}
```

先查询预检，仅在 `ready=true` 时发布。发布后验证 `accepted`、`task_id`、`requested_task_id`，避免把旧任务当新任务。状态优先检查 `reception_state.state/runtime_phase`、终态与 `all_done`，不能只看外层 `active`。

DREAM 核心接口：`/health`、`/v1/status`、`/v1/world`、`POST /v1/navigation/goals`、`GET /v1/commands/{command_id}`。VLA 核心接口：`/health`、`/v1/vla/control/status`、`POST /v1/vla/tasks`、`GET /v1/vla/tasks/{command_id}`。

## 8. 飞书、示教与其他能力

飞书桥接是独立进程：只调用 8888，不直接操作 Redis、DREAM 或 VLA。私聊支持文本任务，群聊仅处理 `@机器人 /task ...`；运动或含糊任务要求原发送者二次确认。通过 SQLite 做消息去重、审计和跟踪，默认 `dry_run`；离线/忙碌不排队、不自动补发。

本地安装 `integrations/feishu/requirements.txt`，配置 `.env` 后运行 `python integrations/feishu/run.py`。需要企业自建机器人及消息接收、发送权限；消息事件 `im.message.receive_v1` 和交互回调 `card.action.trigger` 均要配置。应用权限和可见范围应按企业管理员要求审批。

飞书跟踪超时只停止轮询，不取消机器人任务。当前没有统一可靠的网页/飞书整任务取消 API，不提供“远程停止已保证”的承诺。

示教、经验和仿真相关源码保留，但发布版本不携带个人示教视频或学习结果。mock 的“开灯/数缺口/补货/反思”流程与固定 G1 单罐路线是不同能力，不能把 mock 演示当作真实物理验证。

## 9. 验证范围与已知限制

2026-09-02 在隔离的发布副本完成：Master 6 项、DREAM HTTP 7 项、飞书 31 项单元测试全部通过；DREAM 合成数据自测 27 项断言通过，共 71 项离线检查。发布扫描另行检查 200 个 Python 文件的语法和待提交内容的隐私规则。没有运行真机测试或启动现场服务。

- 当前源码包含 navigation-only 路径、hand-only 校验、任务身份对齐、未知 POST 恢复、只读发布预检和飞书确认链。
- 现场历史记录曾确认导航到 table2、VLA hand-only 抓取和动作端口归还；后续门段/全链仍需独立验收。
- 曾出现定位新鲜度失败、导航锁定和跨主机网络失联。这些属于阻塞条件，不能记作任务成功。
- 本次发布的测试结果只代表脱敏源码的离线逻辑，不代表当前现场在线、硬件安全或完成整个搬运闭环。
- 实际共享配置、模型和外部部署需自行恢复。路线、证据策略、相机合同必须三端一致。
- HTTP 服务主要面向受控内网，缺少完整的公网鉴权、授权和 TLS 部署；发布源码不意味着允许公网控制机器人。

维护时先跑离线回归和 `scripts/check_publication.py`，再做现场只读预检，最后才分段执行真机任务。所有新增私有配置、照片、日志、用户数据都应保持在 Git 之外。

## 10. 许可与来源

本源码快照基于 FQPlanner 工作区演进，保留 Apache-2.0 `LICENSE` 以及相关文件中的原版权声明。RoboCasa、MuJoCo、ROS2、GR00T、SONIC、模型和 SDK 等外部组件按各自许可证获取。没有重新发布未包含在源码快照中的模型、交付 wheel 或私有场景资源。
