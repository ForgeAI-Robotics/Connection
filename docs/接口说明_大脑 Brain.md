# 接口说明：大脑 Brain

初版日期：2026-08-27  
大脑实现核对日期：2026-10-09
契约版本：`fq/reception-lan/v1`  
适用范围：共用大脑任务 API、任务控制与状态、单罐接待及仅导航流程、DREAM/VLA 调用与核验。

> 本文的大脑行为已按当前工作区代码核对。网页、飞书和语音文本入口调用大脑 `:5000`，任务网页为 `:8888`。任务取消、人工重试、弱证据与交接规则以本文为准；旧规划和历史联调中的不同规则不作为当前接口依据。本次只更新大脑相关内容，不据此宣布导航、操控的全部合同已实现或真机已验收。

## 1. 大脑侧职责与 API 归属

Runtime 是任务状态唯一权威；Planner 只给计划，Runner 不改业务目标，Verifier 输出 PASS / FAIL / UNKNOWN。接待、通用任务、观察、整理及演示共用 Runtime；真机端口与仿真端口由运行配置选择。大脑不直接实现 L1/L0，不发 NX 裸 START/STOP。交接依据及人工跳过例外见第 8.3 节。

| 接口 | 行为 |
| --- | --- |
| `POST /publish_task` | 保留 task、task_id、refresh、resume 与 accepted 等现有字段；一次只接受一个顶层任务，拒绝时不自动重发 |
| `GET /api/task_status` | 只读返回当前任务、步骤、尝试、失败与控制状态；字段见第 1.3 节 |
| `GET /api/sops` | 返回已登记业务包的 SOP 标识、版本、范围和明确触发词（`packages`），以及只能由明确触发词选中的 SOP 变体（`variants`，如仅导航接待）；不是候选经验文件 |
| `POST /api/task_pause`、`task_continue`、`task_cancel` | 分别暂停流程、继续当前环节、仅取消大脑任务；请求与回执见第 1.2 节 |
| `POST /api/task_skip` | 必填 `task_id`、`step_id`；跳过当前未完成动作，执行中先请求停止，核对停止与资源后推进；保留人工跳过记录，不伪造成功 |
| `POST /api/task_preflight` | 当前默认实现仅返回提示，不实际检查三端就绪；见第 8.1 节 |
| `GET /health`、`/api/execution_profile` | 进程工作器状态与已应用配置版本 |
| `POST /api/reception/run`、`GET /api/reception/report` | 同一 Runtime 的 mock 演示与报告，不另起旧接待循环 |
| `/api/scene_state`、`belief`、`update_scene` | 大脑侧现场读模型；人工修改写入带来源和有效期的线索，不直接伪造已确认事实 |
| `/api/experiences`、`save_experience`、成功/失败经验接口 | 读取或人工保存经验，保持既有字段；不自动发布规则 |
| `/api/demo/parse`、`status`、`result`、`save` | 有界后台示教作业，job_id 关联；保存为未经评测候选，不直接写入生效 SOP |
| `/api/quad_latest`、`robot_status`、`record/*`、`timeline`、`task_timeline/*` | 严格使用选择的观察源；任务画面采集由大脑负责，网页退出不终止采集 |
| `/api/task_events`、`task_intent`、`chat`、`chat/route` | 事件查询、共享分流与飞书闲聊服务；没有绕过 Runtime 的观察执行 |

网页保留原路径，通过 `BrainClient` 转发。配置校验由 Ops `POST /api/validate-config` 提供。反思线程读取任务快照，按任务和账本版本去重；评测、影子和发布继续在学习侧，只有新任务绑定新快照，已开任务保留原版本。

### 1.1 共用任务选择与异常建议

大脑先持久化任务再调用 LLM，任务选择与规划等待期间仍可取消。明确的“开始接待”等登记触发词直接选择 `reception.single_can` v1；其它表述由模型按登记范围选择业务包。模型只能选择包，不能向接待 SOP 注入动作、坐标或跳步。通用任务继续通过 Planner 读取实际能力与场景生成步骤。

`GET /api/task_status` 增加以下只读字段：

| 字段 | 含义 |
| --- | --- |
| `selection` | 包名、选择来源、原因、SOP 标识和版本；执行环境选择后绑定，恢复时不重新选择 |
| `recovery_advice` | 异常分析与建议，包含 `action`、`summary`、`reason`、`source`、原任务／命令／版本、`applicable`；`automatic=false` 表示尚未执行 |
| `handoff_observation` | 原命令停止与现有导航／操控状态接口的只读诊断；通路就绪不等于控制器接管 |
| `subtask_list[].reported_success` | 下游是否自报成功；非此类端口或尚无回包时为 null |
| `subtask_list[].evidence_level` | 下游报告的证据等级或适配器的证据等级 |
| `subtask_list[].effect_verified` | 当前尝试是否已通过 Verifier；不能由 LLM 建议改变 |

建议限于等待、人工协助、取消，以及当前状态允许的查询原命令或继续。LLM 不直接调用控制接口；模型异常或格式非法时保存规则回退建议。建议基于的账本版本变化后 `applicable=false`，迟到结果不能覆盖新状态。当前任务网页只展示失败原因和历史尝试，不把该模型建议显示为确定的失败根因；API 仍保留 `recovery_advice`。

### 1.2 任务控制接口

四个按钮均先作用于大脑，再由大脑决定是否调用当前执行端。任务、步骤和命令是不同层级，`cancelled` 必须结合所属层级解释。

| 接口 | 大脑行为 | 下游调用与完成条件 |
| --- | --- | --- |
| `POST /api/task_pause` | 关闭后续派发，保留当前任务和步骤 | 有在途命令时调用该执行端的命令取消接口，再查询停止；未确认时进入 `recovery_required`，`pause_requested=true`，不假装远端已停 |
| `POST /api/task_continue` | 继续当前环节，不重开整轮任务 | 未下发时重查条件；原命令已成功时核验；确认未完成、停止和资源释放后才准备新尝试。详见第 9.1 节 |
| `POST /api/task_skip` | 人工放弃当前未完成动作，推进下一动作 | 执行中先请求停止；原命令停止、资源和身份核对通过后才推进。保留失败与跳过记录，不生成虚假成功 |
| `POST /api/task_cancel` | 废弃本轮大脑任务，关闭后续派发并结束本地等待 | 不构造执行器，不调用导航或操控的取消接口，不等待远端回执；返回 `scope=task_only`。不声明机器人已停止 |

暂停的实现是结束当前下游命令、保留大脑流程；继续可能使用新命令重新执行未完成步骤，不要求导航具备同一命令的暂停／恢复状态。

请求规则：

- 暂停、继续、取消可传 `task_id`，调用方应绑定已显示的任务，避免操作新任务；传入任务与当前不符时拒绝。
- 跳过必须同时传非空 `task_id` 和 `step_id`。`can_skip=true` 只表示当前步骤可申请跳过，不代表停止检查已经通过。
- 继续可传 `step_id` 和 `expected_command_id`，从状态接口的 `control_step_id`、`control_command_id` 获取；无命令时传空字符串。网页和飞书使用这些字段防止陈旧请求误重试新命令。
- 重复暂停已暂停任务、继续正在运行或核验的任务，以及对匹配的已结束任务执行控制，可返回 `no_op=true`，不会再下发一次动作。
- HTTP 200 不能单独代表控制成功：还需检查 `accepted`、`state`、`error` 及当前任务状态。参数错误可返回 HTTP 400。

取消当前活动任务的请求与响应示例：

```http
POST /api/task_cancel

{"task_id": "reception-example-001"}
```

```json
{
  "accepted": true,
  "completed": true,
  "scope": "task_only",
  "task_id": "reception-example-001",
  "command_id": "nav-table2-example-001",
  "state": "cancelled",
  "error": null,
  "message": "本轮任务已取消，不再执行后续步骤"
}
```

`command_id` 是取消时大脑记录的原在途命令，可为 null；不是新发出的停止命令。大脑归档取消时的远端已知状态，保留尝试历史，并丢弃旧任务的迟到执行／规划结果。可以随后发布新任务，但任务取消本身不是远端停止或资源释放证明。此按钮用于现场废弃整轮任务；导航重启等现场处置独立进行。

文字控制中的“暂停”“停止”“/stop”映射为暂停；“取消任务”“取消”“/cancel”映射为任务取消。

### 1.3 任务状态与失败字段

当前任务状态使用小写：`running`、`verifying`、`paused`、`waiting_human`、`recovery_required`、`succeeded`、`failed`、`cancelled`。旧账本可能出现 `cancelling`，当前本地任务取消不需要等待该中间状态。

| 字段 | 当前含义 |
| --- | --- |
| `active`、`terminal`、`state` | 大脑任务是否处于非终态、是否结束及状态，不等于机器人是否在运动 |
| `phase`、`control_step_id`、`control_command_id` | 当前步骤与最新尝试身份；已准备的新命令号不一定已发送 |
| `can_resume`、`can_skip`、`skip_step_id` | 控制入口的可申请状态；调用时仍需重新检查 |
| `pause_requested` | 已请求暂停；与 `recovery_required` 同时出现时应提示停止尚未确认 |
| `failure_detail` | 当前待恢复原因，含 `kind`、`code`（可缺省）、`message`、`display_message`、`not_started` |
| `failure_detail.kind` | `rejected` 为已识别的提交前拒绝，`execution_failed` 为有失败／取消终态回执，`unknown` 为结果未明确 |
| `failure_detail.not_started` | 只有已识别的提交拒绝才为 true；false 不等于已确认运动发生 |
| `failure_history` | 按尝试保留失败步骤、命令、错误码、原文与中文说明；不是逐行异常日志总数 |
| `attempt_summary` | `failure_count`、`retry_count`、`manual_retry_count`；查询原命令不算新尝试 |
| `manual_skips`、`flow_finished` | 人工跳过及流程是否因含跳过而结束；含人工跳过不记为全部成功 |
| `subtask_list` | 每步的成功、失败、未知、跳过及证据；`done` 可能包含跳过，不能只据此统计成功 |
| `completed`、`total` | 进度节点计数，包含准备／核验节点；不等于实际动作数。成功数量需排除跳过 |
| `all_done` | 仅任务 `succeeded` 为 true；不表示历史上从未失败 |
| `task_cancellation` | 本地取消范围、时间、原命令和当时远端已知状态；不新增物理停止事实 |
| `blocks_new_motion` | 大脑任务是否阻止新任务准入，不代替下游安全检查 |

中文说明与英文错误原文、错误码一起保留。任务后来成功时仍保留历史失败与人工重试；失败历史不能覆盖最终成功，也不能被最终成功抹除。

## 2. 局域网拓扑

| 服务 | 地址 | 调用方向 | 用途 |
|---|---|---|---|
| 大脑 API | `http://<BRAIN_IP>:5000` | 网页／飞书／语音入口 → 大脑 | 任务、控制、状态的统一接口 |
| 任务网页 | `http://<BRAIN_IP>:8888` | 浏览器 → 网页 → 大脑 | 发布、展示和转发控制 |
| 语音文本入口 | `http://<BRAIN_IP>:8890` | 上游识别文本 → 大脑 | 只提交已识别文字 |
| DREAM | `http://<NAV_IP>:8001` | 大脑 → DREAM | 世界、状态、导航 |
| VLA | `http://<VLA_IP>:8091` | 大脑 → VLA | 抓取、放置、可选相机图片 |
| RealSense | NX `<ROBOT_IP>:5555` 唯一相机服务打开 USB | VLA 子系统内部 | HTTP 桥只读订阅；照片开关开启时生成动作后图片 |

地址由 `config/networks.yaml` 的当前网络及显式环境配置解析，表中使用角色占位符。联调采用大脑主动轮询，不要求 DREAM 或 VLA 回调大脑。三台主机需要互相可达，防火墙放行 TCP 8001、VLA 服务端口和大脑网页端口。

## 3. 统一字段规范

### 3.1 标识

- `task_id`：整条接待任务唯一，例如 `reception-20260827-001`；
- `command_id`：每个真实动作唯一，例如 `nav-table2-001`、`vla-pick-001`；
- 同一任务的所有 DREAM/VLA 请求使用同一个 `task_id`；
- 同一个 `command_id` 因网络错误重发时，远端必须返回原事务，不重复执行；
- 本轮失败后不自动生成新命令重试。

### 3.2 时间

- 所有 JSON 时间使用 ISO 8601，并包含时区；
- 示例：`2026-08-27T14:30:05.123+08:00`；
- VLA 的 `completed_at` 和相机的 `captured_at` 必须可直接比较；
- 相机判真要求 `captured_at > completed_at`。

### 3.3 HTTP

- JSON 请求：`Content-Type: application/json`；
- JSON 响应：`Content-Type: application/json; charset=utf-8`；
- 推荐请求头：`X-Contract-Version: fq/reception-lan/v1`；
- HTTP 202 只表示已接收异步命令，不表示动作成功；
- 成功必须通过状态查询接口轮询到终态。

### 3.4 统一错误格式

```json
{
  "success": false,
  "error": {
    "code": "MACHINE_READABLE_CODE",
    "message": "可读错误说明",
    "retryable": false,
    "details": {}
  }
}
```

## 4. 大脑侧配置

- `config/execution.yaml` 与已应用的 `data/system/execution.json` 决定接待、通用执行和观察各自的后端；存在已应用配置时优先按该路由选择。
- 真机接待端点由 `reception_real.dream_base_url`、`vla_base_url` 提供，网络配置可解析并写入运行配置；HTTP 客户端使用 `contract_version`、`request_timeout_sec`。
- `reception_real.kernel_enabled` 是真机派发许可。2026-10-09 本机已用于真机仅导航联调，但文档不把开关值作为所有部署的默认值或验收结果；查询生效配置与现场条件后判断。
- 无已应用路由时，接待后端依次读取 `RECEPTION_MODE`、`brain.reception_backend`，再按 `reception_real.enabled` 回退选择；不能仅凭模板中的环境变量推断当前后端。
- `dream_inspection_enabled=true` 可启用完整接待中的可选 inspection；仅导航变体不包含该步骤。

当前共用 Runtime 按结构化执行证据核验，不使用旧接待循环的 `COMPLETED_HAND_STATE_ONLY` 成功终态。模板中的 `vla_result_policy`、`grasp_photo_verification_enabled`、`place_photo_verification_enabled` 不能作为当前共用链路已启用“手状态通过”或“VLM/LLM 图片双判真”的依据；这些旧字段不能绕过第 8.2 节的物体证据要求。

步骤时限及请求构造见 `src/brain/packages/reception.py`。本次更新未改变配置或接口实现。

## 5. 大脑调用的 DREAM 接口

下表列出客户端能力及执行链路使用的接口，不表示当前任务启动时必定依次调用全部端点。

| 类别 | 方法 | 路径 | 用途 |
|---|---|---|---|
| 健康能力 | GET | `/health` | DREAM HTTP 健康查询 |
| 执行门／交接 | GET | `/v1/status` | 导航通路、定位、安全门和活动命令状态 |
| 世界能力 | GET | `/v1/world` | 客户端可获取世界入口，不等于准备节点已实际查询 |
| 关系图能力 | GET | `/total_scene_graph_latest.json` | 客户端默认关系图路径，也可使用世界回包中的 URL |
| 导航 | POST | `/v1/navigation/goals` | 提交每段导航 |
| 轮询 | GET | `/v1/commands/{command_id}` | 获取真实导航终态 |
| 命令停止 | POST | `/v1/navigation/cancel` | 暂停或执行中跳过时，请求结束指定导航命令；取消大脑任务不调用 |

导航成功硬条件：

```text
state == "succeeded"
AND result.success == true
AND result.reached == true
AND result.navigation_stopped == true
```

## 6. 大脑调用的 VLA 接口

下表区分执行时调用与客户端保留能力，不将相机接口存在视为图片判真已接入。

| 类别 | 方法 | 路径 | 用途 |
|---|---|---|---|
| 健康能力 | GET | `/health` | VLA HTTP 健康查询 |
| 控制交接 | GET | `/v1/vla/control/status` | 动作端口、策略和相机总状态 |
| 动作 | POST | `/v1/vla/tasks` | 提交 pick/place |
| 轮询 | GET | `/v1/vla/tasks/{command_id}` | 查询 VLA 终态 |
| 命令停止 | POST | `/v1/vla/tasks/{command_id}/cancel` | 暂停或执行中跳过时，请求结束指定操控命令；取消大脑任务不调用 |
| 相机能力 | GET | `/v1/camera/status` | 客户端保留的相机状态查询，不代表当前接待自动调用 |
| 快照能力 | POST | `/v1/camera/snapshots` | 客户端保留的动作后图片能力；不据此声明当前图片判真已接入 |
| 可选图片 | GET | `/v1/camera/snapshots/{snapshot_id}/rgb` | 下载 JPEG/PNG |

## 7. 固定导航参数

坐标系：`map`；yaw：弧度。

| 阶段 | target_id | route_phase | leg_index | goal_xyt | motion_mode |
|---|---|---|---:|---|---|
| table2 | `table_2` | 空 | 1 | `[0.9903405869861586, 1.3761315438191244, -0.39236607751253016]` | `forward_path` |
| relay2 | `door_1` | `door_approach` | 2 | `[3.733075988421528, 6.215369909530748, 2.718279944258407]` | `forward_path` |
| relay3 | `door_1` | `door_lateral_exit` | 3 | `[4.185939449618811, 7.560143924693016, 2.7689146673931306]` | `lateral_path_aligned` |
| table1 | `table_1` | `table1_approach` | 4 | `[3.0873798986272165, 8.279995338440145, 1.175238157458919]` | `forward_path` |

所有导航均设置：

```json
{
  "frame_id": "map",
  "require_final_orientation": true
}
```

## 8. 当前接待流程与核验

### 8.1 流程与请求身份

完整接待使用 `reception.single_can` v1：

```text
初始化 → 读取现场 → table2 导航 → 可选 inspection
→ 抓取 → 抓取核验 → relay2 导航 → relay3 横移
→ table1 导航 → 放置 → 放置核验与最终交接
```

明确触发词“开始接待（仅导航）”选择 `reception.single_can.nav_only` v1，仅包含两个准备节点和四段导航，不派发抓取、放置、inspection，也不执行导航与操控之间的交接。6/6 只证明该仅导航流程完成。

大脑从 `src/contracts/reception_lan.py` 的固定合同构造四段请求，字段见第 7 节及导航接口说明。模型不重新生成这些坐标、路段或动作模式。每段独立提交、查询和核验，正常流程核验通过才推进；人工跳过另作记录。

所有动作沿用原 `task_id`；新尝试使用新 `command_id`，原请求、回执和失败不覆盖。操控请求包含 `operation`、`object_id`、`target_area` 和引用原导航命令的 `navigation_proof`。完整请求示例见导航、操控接口文档，不使用旧版 `runtime_phase`／`verified_state` 作为当前任务状态字段。

当前 `HttpFacade.get_task_preflight()` 直接返回 `ready=true`、`required=false`、`task_type=runtime`、`blockers=[]`，不实际查询三端。不能把此响应当作世界、定位或操控已检查通过。`INITIALIZING`、`FETCHING_WORLD` 在当前 Runtime 中也是本地进度节点，完成标记不证明已查询世界关系图或完成四段合同比对。

真正派发时，真机端口仍受派发许可约束，导航门读取下游 `navigation_transport_ready`；门不可达或未开时等待人工，继续后重新检查。导航本身的定位批准、规划和执行安全门仍由导航决定。上述预检能力缺口本次只如实记录，未修改代码补齐。

### 8.2 成功证据

大脑先检查命令、任务、目标等身份，以及带时区的完成时间，再由 Verifier 输出 `PASS / FAIL / UNKNOWN`。下游自报成功不直接等于大脑任务成功。

| 动作 | 当前适配器接受的主要结果条件 |
| --- | --- |
| 导航 | `state=succeeded`、`success=true`、`reached=true`、`navigation_stopped=true` |
| 抓取 | `state=succeeded`、`success=true`、`object_grasped=true`、`holding` 等于请求物体；`evidence.object_presence_verified` 不能显式为 false |
| 放置 | `state=succeeded`、`success=true`、`object_grasped=false`、`released=true`、`holding=null`、`object_at_target=true`；`evidence.object_at_target_verified` 不能显式为 false |

抓取和放置还要求 `policy_stopped=true`、`navigation_port_ready=true`，步骤要求物体等级证据。`evidence_level` 或 `evidence_grade` 为 `hand_state_only` 时，适配器不认定物体效果通过；只有手部开合或弱成功回执时保留未知，不能用 LLM 建议补成 PASS，也不能因为未知就重复抓放。

这些条件说明大脑如何解释下游合同，不证明导航／操控已在真机上提供了全部可靠传感器证据。已发布的规则还可能追加核验要求；不能用回包字段齐全代替真实效果验证。

### 8.3 控制交接与人工跳过

普通交接先核对来源命令身份、时间、成功终态、停止及资源释放，再读取导航 `/v1/status` 与操控 `/v1/vla/control/status`。要求两侧无活动命令、操控策略未运行、导航通路已归还。

- 下游不提供 `control_receipt` 时，满足上述条件可按 `transport_ready_without_receipt` 放行。
- 下游提供该字段时，必须通过任务、原命令、目标控制器和时效核验；无效凭证不等于缺省凭证。
- 从来源命令步骤到交接目标之间包含人工跳过时，大脑记录 `manual_skip` 并补齐该次交接，不再重复查询交接。跳过操作自身仍核对停止和资源。
- 导航被人工跳过后，大脑可继续提交操控，`navigation_proof` 仍引用原真实命令。请求中的声明不改写原导航终态；操控仍按自身合同查询并决定接受或拒绝，不承诺必然执行。

通路空闲不是实际控制器接管的物理证明；上述内容是大脑当前放行规则。最终放置还需完成 `safe_idle` 交接核验。

### 8.4 流程终态

全部必要步骤通过后为 `succeeded`；过程中有人工重试不清除失败历史。含人工跳过的流程结束为 `failed` 并设置 `flow_finished=true`，不显示全部成功。取消整轮任务为 `cancelled`，仅表示大脑已废弃编排。

当前不再生成 `COMPLETED_HAND_STATE_ONLY`。原始弱证据仍可保留在尝试回执和 `subtask_list[].evidence_level`，不能转换成物体成功终态。

## 9. 本地状态保存

任务状态由 Runtime 单写，当前账本位于 `data/tasks/`。`KernelStore` 使用目录互斥、版本检查和原子文件替换，保存每一步的尝试、不可变请求、原 `command_id` 及证据。旧接待记录在 `data/retired/reception/` 仅作历史留存，不作为新内核的续跑入口。

动作发送前记录意图；重启或丢回包时优先核查原命令。未知结果不转换为成功，也不靠删除账本、清 holding 或 `force_new_task` 续跑原动作。显式取消任务按第 1.2 节本地废弃整轮，不伪造原命令停止证据。

## 9.1 同一 Runtime 的继续与人工重试

| 项 | 当前行为 |
| --- | --- |
| 入口 | `POST /publish_task` 的 `resume: true`，以及 `/api/task_continue`，均进入大脑的原任务控制通路 |
| 任务身份 | 沿用原 `task_id`，附着账本记录的业务包、执行后端和目标地址；不重新发布一整单 |
| `recovery_required` | 有在途原命令时先查询；已识别的提交拒绝可准备新尝试；未下发且交接失败时重查交接。结果未知时不重发 |
| `paused` / `waiting_human` | 按各自状态继续，重新经过相应条件检查；继续不等于已取得身体动作许可 |
| 新尝试 | 人工继续核对旧尝试停止、资源释放和明确未完成后才创建；自动恢复预算仍为 0，只读重查不增加 `-r{n}` 计数 |
| 取消与新任务 | 显式取消按 `task_only` 结束大脑任务并解除该任务准入占用；新任务仍执行自身前置检查，不能把取消回执当作远端已停 |

当前不承诺下游跨重启一定保留历史命令。继续原任务时，原命令不可查仍保持 `recovery_required`；用户另行取消任务不依赖该查询。控制交接与效果证据见第 8 节，不能套用旧规划中的“取消必须确认下游停止”规则。

## 9.2 提交拒绝与不确定事务

导航提交前先持久化原 `command_id`，不因异常自动换号重发：

- 只有 HTTP 400／403／409／422 且错误结构、身份和错误码符合 `src/contracts/submission.py` 的识别规则，才视为明确提交拒绝，记录 `failure_detail.kind=rejected`、`not_started=true`。忙碌错误还需明确占用者是其他命令。
- 任意 HTTP 400 不能一概视为未执行；HTTP 500、裸 404、同 ID 冲突或不符合拒绝合同的回包，也不能证明动作未执行。适配器保留结果未知。
- 导航 POST 连接断开或超时时，客户端有界查询原命令；查到则继续跟踪，查不到进入恢复处理，不自动重新 POST。
- 轮询阶段连接失败或原命令不可查，同样不能改写为成功，也不能自动换号补发。
- 下游明确失败／取消终态与提交前拒绝分别记录；人工继续的条件见第 9.1 节。下游 `retryable=false` 不应被文案直接解释为“永远不能人工重试”，也不意味着其他恢复条件已经满足。

取消任务是独立的本地控制决定，不用于推断原命令究竟是否执行。

## 10. 当前代码落点

| 位置 | 职责 |
| --- | --- |
| `src/brain/api/app.py` | 大脑 API 路由，提交与控制任务 |
| `src/brain/service.py` | 附着原任务、规划与调度的应用入口 |
| `src/brain/kernel/runtime.py` | 状态迁移、原命令核查、人工重试与跳过、本地任务取消 |
| `src/brain/storage/tasks.py` | 任务和尝试账本 |
| `src/brain/packages/reception.py` | 接待相位与合同 |
| `src/brain/adapters/execution.py` | 下游回执解释、停止与资源证据、交接条件和可选凭证核验 |
| `src/entries/web/`、`src/entries/feishu/`、`src/entries/voice/` | 独立入口，通过大脑接口操作任务 |

## 11. 验收边界

2026-10-09 最后一轮仅导航任务四段成功，含两次人工重试、零跳过；不等于无需干预一次通过，也不代表抓放或完整三端接待通过。最终任务取消改动部署在该轮之后，不把这轮算成新按钮版本的真机复测。

当天大脑侧修复及回归范围见[多端联调记录](多端联调_20261009_大脑与导航流程及任务控制.md)。本次文档核准基于工作区实现，未修改协议路径、导航目标或远端程序，也未新增真机验证。

## 接待协议模拟路由（2026-09-28）

运行环境的接待模块可选 `simulation_backend: reception_protocol`。从正式 `/publish_task` 入口执行「开始接待」，任务状态与账本标记 `execution_backend=reception_protocol`，仍执行 `reception.single_can` v1。独立下游默认延时成功，任务核验不设模拟特例。旧 `/api/reception/run` 是旧会议演示入口，不用它验证本流程。

`handoff_observation` 可包含共用可选合同 `fq/control-receipt/v1` 的核验结果。凭证缺失、有效或无效时的处理统一见本文第 8.3 节。详细启动、端点和真机差异见[模拟真机接口说明](接口说明_模拟真机.md)。

## 语音文本入口（2026-09-29）

`python -m entries.voice` 默认监听 `:8890`，通过当前网络配置解析大脑地址，可由 `MASTER_URL` 显式覆盖。它不采集音频，也不筛选文字。

`POST /publish_task` 要求 JSON 字段 `task` 为非空字符串。缺省时入口生成 `task_id`，并把 `refresh` 设为 true，然后原样转发到大脑 `POST /publish_task`。请求头固定为 `X-FQ-Source: voice`、`X-FQ-Via: voice`。空文本返回 400；大脑不可达返回 503。`GET /health` 只表示入口进程在听，不表示大脑已接受任务。

大脑收到后的分类、预检、控制和执行与网页、飞书提交的同一段文字相同。NX 上的 `reception_voice.py` 仍只向网页 `:8888` 发送固定任务「开始接待」，尚未改为调用本入口。
