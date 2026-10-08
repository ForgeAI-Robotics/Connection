# 接口说明：大脑 Brain

初版日期：2026-08-27  
当前现场口径：2026-09-20  
契约版本：`fq/reception-lan/v1`  
适用范围：单罐接待固定 Pipeline、DREAM/VLA 调用、状态保存、断点继续与可选照片判真。

> 2026-09-24 工程更新：大脑实现位于 `src/brain`，旧执行循环和兼容启动器已退出。网页与飞书各自直连 `:5000`。下文第 8 节保留早期接待报文和流程说明，其中 hand-only 终态不适用于当前 Runtime。当前任务状态与恢复见第 1、9 节；工程与有效约束统一见[重构规划第 16–17 节](规划说明_具身大脑重构.md)。`kernel_enabled=false`，真机动作许可仍关闭。

## 1. 大脑侧职责与 API 归属

Runtime 是任务状态唯一权威；Planner 只给计划，Runner 不改业务目标，Verifier 输出 PASS / FAIL / UNKNOWN。接待、通用任务、观察、整理及演示共用 Runtime；真机端口与仿真端口由运行配置选择。大脑不直接实现 L1/L0，不发 NX 裸 START/STOP。交接能力未确认时停止推进。

| 接口 | 行为 |
| --- | --- |
| `POST /publish_task` | 保留 task、task_id、refresh、resume 与 accepted 等现有字段；一次只接受一个顶层任务，拒绝时不自动重发 |
| `GET /api/task_status` | 读取内核账本，支持 paused、waiting_human、cancelling、recovery_required；不改写旧账本 |
| `GET /api/sops` | 返回已登记业务包的 SOP 标识、版本、范围和明确触发词（`packages`），以及只能由明确触发词选中的 SOP 变体（`variants`，如仅导航接待）；不是候选经验文件 |
| `POST /api/task_pause`、`task_continue`、`task_cancel` | 送到 Runtime 所有者线程；取消受理不等于已经停止 |
| `POST /api/task_skip` | 人工跳过指定待恢复／已暂停未完成步骤；必填 `task_id`、`step_id`，核对停止/资源后前移，保留失败，不伪造前置成功；跨越该跳过的下一次交接由大脑补齐，导航被跳过后照常下发操控（见规划第 21 节） |
| `POST /api/task_preflight` | 只读提示，不等于下游已就绪或已取得真机许可 |
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

建议限于等待、人工协助、取消，以及当前状态允许的查询原命令或继续。LLM 不直接调用控制接口；模型异常或格式非法时保存规则回退建议。建议基于的账本版本变化后 `applicable=false`，迟到结果不能覆盖新状态。

`POST /api/task_continue` 在暂停／待恢复时先查询原 `command_id`。确认停止、资源释放且当前动作明确失败、取消或未开始后，为同一任务的当前环节准备新 attempt／新命令 ID，重新执行原有前置检查，再下发。原动作报告成功时进入效果核验，弱证据不重复抓放。未知或未核清资源仍阻止新派发。请求可携带 `step_id` 和 `expected_command_id`（来自状态接口的 `control_step_id`、`control_command_id`；无命令时传空串），拒绝旧页面／重复请求误重试新的命令。网页和飞书继续入口绑定这些身份。`/publish_task` 的 `resume:true` 使用相同恢复语义。DREAM/VLA 线上请求体和 HTTP 路径未增加字段或接口。

## 2. 局域网拓扑

| 服务 | 地址 | 调用方向 | 用途 |
|---|---|---|---|
| 大脑网页/Master | `http://<BRAIN_IP>:8888` | 浏览器 → 大脑 | 创建和查看任务 |
| 语音文本入口 | `http://<BRAIN_IP>:8890` | 上游识别文本 → 大脑 | 只提交已识别文字 |
| DREAM | `http://192.168.5.18:8001` | 大脑 → DREAM | 世界、状态、导航 |
| VLA | `http://192.168.5.194:8091` | 大脑 → VLA | 抓取、放置、可选相机图片 |
| RealSense | NX `192.168.5.240:5555` 唯一相机服务打开 USB | VLA 子系统内部 | HTTP 桥只读订阅；照片开关开启时生成动作后图片 |

联调采用大脑主动轮询，不要求 DREAM 或 VLA 回调大脑。三台主机需要互相可达，防火墙放行 TCP 8001、VLA 服务端口和大脑网页端口。

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

当前真机配置核心字段：

```yaml
reception_real:
  enabled: true
  contract_version: "fq/reception-lan/v1"
  dream_base_url: "http://192.168.5.18:8001"
  vla_base_url: "http://192.168.5.194:8091"
  vla_result_policy: "hand_state_only"
  request_timeout_sec: 10
  dream_poll_interval_sec: 0.5
  vla_poll_interval_sec: 1.0
  navigation_timeout_sec: 900
  navigation_transport_timeout_sec: 30
  dream_inspection_enabled: false
  door_lateral_timeout_sec: 300
  vla_timeout_sec: 600
  place_timeout_sec: 1800
  snapshot_timeout_sec: 30
  auto_retry_business_step: false
  camera_owner: "vla"
  grasp_camera_view: "left_wrist"
  place_camera_view: "ego_view"
  grasp_photo_verification_enabled: false
  place_photo_verification_enabled: false
```

环境变量 `RECEPTION_MODE` 非空时优先于 `reception_real.enabled`。公开模板 `config/examples/env.example` 默认使用 `mock`，现场真机环境必须明确设置为 `real` 或移除该覆盖。

当前两个照片判真开关均为 `false`：大脑不把相机状态作为预检硬门，不请求动作后快照，也不调用 VLM/LLM。抓取和放置依据 VLA 真实终态、左手连续帧证据、`holding/released`、`policy_stopped` 和 `navigation_port_ready` 推进；任务状态中的 `evidence_level` 当前为 `vla_only`。打开对应照片开关后，才执行该动作的图片双重判真。

## 5. 大脑调用的 DREAM 接口

| 顺序 | 方法 | 路径 | 用途 |
|---:|---|---|---|
| 1 | GET | `/health` | DREAM HTTP健康 |
| 2 | GET | `/v1/status` | 世界、定位、安全门、活动命令 |
| 3 | GET | `/v1/world` | 获取关系图URL等世界入口 |
| 4 | GET | `/total_scene_graph_latest.json` | 读取 table2、door_1、table1 |
| 导航 | POST | `/v1/navigation/goals` | 提交每段导航 |
| 轮询 | GET | `/v1/commands/{command_id}` | 获取真实导航终态 |
| 可选取消 | POST | `/v1/navigation/cancel` | 人工取消当前导航 |

导航成功硬条件：

```text
state == "succeeded"
AND result.success == true
AND result.reached == true
AND result.navigation_stopped == true
```

## 6. 大脑调用的 VLA 接口

| 顺序 | 方法 | 路径 | 用途 |
|---:|---|---|---|
| 预检 | GET | `/health` | VLA HTTP健康 |
| 预检 | GET | `/v1/vla/control/status` | 动作端口、策略、相机状态 |
| 动作 | POST | `/v1/vla/tasks` | 提交 pick/place |
| 轮询 | GET | `/v1/vla/tasks/{command_id}` | 查询 VLA 终态 |
| 可选取消 | POST | `/v1/vla/tasks/{command_id}/cancel` | 取消 VLA 任务 |
| 可选相机 | GET | `/v1/camera/status` | 对应照片开关开启时确认 VLA 独占相机且可取帧 |
| 可选快照 | POST | `/v1/camera/snapshots` | 对应照片开关开启时请求一张动作后新图片 |
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

## 8. 大脑完整执行顺序

### 8.1 创建任务

```json
{
  "task_id": "reception-20260827-001",
  "state": "RUNNING",
  "runtime_phase": "FETCHING_WORLD",
  "verified_state": "INITIALIZED",
  "holding": null,
  "object_location": "table_2"
}
```

### 8.2 世界预检

依次调用 DREAM `/health`、`/v1/status`、`/v1/world`、关系图。确认：

- `interface_online=true`；
- `world_state_available=true`；
- 没有冲突的 `active_command_id`；
- 关系图中包含 `table_2`、`door_1`、`table_1`。

随后进行发送前合同核对，但不采用或改写DREAM坐标：

- 关系图 `contract_version=fq/reception-lan/v1`；
- 关系图和 `agent_navigation_contract` 均使用 `frame_id=map`；
- `agent_navigation_contract` 中四个 `leg_index` 各出现一次；
- 四段的 target、phase、goal_xyt、motion_mode、最终朝向要求与本地固定合同一致；
- `nodes.door_1.evidence.navigation_contract` 的relay2/relay3与本地一致；
- 任一字段缺失或不一致时停止在 `FETCHING_WORLD`，不发送导航。

`agent_navigation_contract` 的外层对象或数组嵌套形式可以变化；大脑递归查找所有含 `goal_xyt` 的 leg，并以唯一 `leg_index` 识别四段。嵌套形式可变不代表字段可缺省，每个命中的 leg 仍必须通过完整合同核对。

DREAM HTTP 在线与动作准入是两层状态：

- `interface_online=false` 才表示 HTTP 接口未就绪；
- `localization_approved`、`motion_ready`、`gateway_ready` 或 `token_ready` 为 false 表示当前动作门未满足，不等于 HTTP 服务故障；
- 空闲待命时仅 `GATEWAY_NOT_READY`、`TOKEN_NOT_READY` 可作为受控的未 Arm 状态；定位、传输所有权、活动命令及其他 blocker 仍按失败关闭处理；
- 大脑只等待或拒绝本次动作，不通过重启服务、补发命令来绕过安全门。

再调用 VLA `/health` 和 `/v1/vla/control/status`。确认：

- `service_ready=true`；
- `policy_running=false`；
- `navigation_port_ready=true`。

只有抓取或放置照片判真开关启用时，预检才额外调用 `/v1/camera/status`，并要求 `camera.owner=vla`、`camera.ready=true`。

### 8.3 table2导航

```json
{
  "contract_version": "fq/reception-lan/v1",
  "command_id": "nav-table2-001",
  "task_id": "reception-20260827-001",
  "target_id": "table_2",
  "route_phase": "",
  "leg_index": 1,
  "frame_id": "map",
  "goal_xyt": [0.9903405869861586, 1.3761315438191244, -0.39236607751253016],
  "motion_mode": "forward_path",
  "require_final_orientation": true
}
```

POST 后轮询 `GET /v1/commands/nav-table2-001`。真实成功后把 `verified_state` 更新为 `AT_TABLE2`。

### 8.4 VLA抓取

```json
{
  "contract_version": "fq/reception-lan/v1",
  "command_id": "vla-pick-001",
  "task_id": "reception-20260827-001",
  "operation": "pick",
  "object_id": "cola_can_1",
  "target_area": "table_2",
  "navigation_proof": {
    "dream_command_id": "nav-table2-001",
    "target_id": "table_2",
    "state": "succeeded"
  }
}
```

轮询 `GET /v1/vla/tasks/vla-pick-001`。所有策略都必须满足：

```text
state=succeeded
result.success=true
result.object_grasped=true
result.holding=cola_can_1
result.policy_stopped=true
result.navigation_port_ready=true
completed_at存在
```

当前 `hand_state_only` 还必须满足：

```text
result.evidence_level=hand_state_only
result.evidence.hand=left
result.evidence.hand_closed_confirmed=true
result.evidence.confirmed_frames>=3
```

照片判真关闭时，直接更新为：

```json
{
  "runtime_phase": "READY_FOR_RELAY2",
  "verified_state": "GRASP_CONFIRMED_BY_VLA",
  "holding": "cola_can_1",
  "object_location": "in_gripper",
  "evidence_level": "vla_only"
}
```

### 8.5 抓取图片和判真（可选）

仅 `grasp_photo_verification_enabled=true` 时执行本节。进入图片判真前暂不把抓取写成视觉确认成功。

请求图片：

```json
POST /v1/camera/snapshots
{
  "contract_version": "fq/reception-lan/v1",
  "task_id": "reception-20260827-001",
  "source_command_id": "vla-pick-001",
  "purpose": "verify_pick",
  "captured_after": "<VLA completed_at>"
}
```

校验响应中的 `captured_at > completed_at`，再下载 `rgb_url`。

VLM固定输出：

```json
{
  "target_visible": true,
  "target_in_gripper": true,
  "confidence": 0.92,
  "reason": "目标确实位于灵巧手中"
}
```

LLM 输入 VLA终态、图片元数据、VLM结果和固定规则，输出：

```json
{
  "verified": true,
  "operation": "pick",
  "object_id": "cola_can_1",
  "reason": "VLA状态与动作后图像一致"
}
```

只有所有硬条件和 `LLM verified=true` 同时成立，才原子更新：

```json
{
  "runtime_phase": "READY_FOR_RELAY2",
  "verified_state": "GRASP_VERIFIED",
  "holding": "cola_can_1"
}
```

### 8.6 relay2、relay3、table1

三段分别提交、分别轮询，不得合并。每段只有 DREAM 真实成功后才提交下一段。

每次POST前，大脑重新轮询DREAM `/v1/status`，只有
`navigation_transport_ready=true` 才允许发送本段请求；等待超过30秒则停止，
不得在VLA仍占用动作通路时发送导航。

relay2 payload：

```json
{
  "command_id": "nav-relay2-001",
  "task_id": "reception-20260827-001",
  "target_id": "door_1",
  "route_phase": "door_approach",
  "leg_index": 2,
  "frame_id": "map",
  "goal_xyt": [3.733075988421528, 6.215369909530748, 2.718279944258407],
  "motion_mode": "forward_path",
  "require_final_orientation": true
}
```

relay3 payload：

```json
{
  "command_id": "nav-relay3-001",
  "task_id": "reception-20260827-001",
  "target_id": "door_1",
  "route_phase": "door_lateral_exit",
  "leg_index": 3,
  "frame_id": "map",
  "goal_xyt": [4.185939449618811, 7.560143924693016, 2.7689146673931306],
  "motion_mode": "lateral_path_aligned",
  "require_final_orientation": true
}
```

table1 payload：

```json
{
  "command_id": "nav-table1-001",
  "task_id": "reception-20260827-001",
  "target_id": "table_1",
  "route_phase": "table1_approach",
  "leg_index": 4,
  "frame_id": "map",
  "goal_xyt": [3.0873798986272165, 8.279995338440145, 1.175238157458919],
  "motion_mode": "forward_path",
  "require_final_orientation": true
}
```

table1成功后更新 `verified_state=AT_TABLE1`，但任务仍是 `RUNNING`。

### 8.7 VLA放置

```json
{
  "contract_version": "fq/reception-lan/v1",
  "command_id": "vla-place-001",
  "task_id": "reception-20260827-001",
  "operation": "place",
  "object_id": "cola_can_1",
  "target_area": "table_1",
  "navigation_proof": {
    "dream_command_id": "nav-table1-001",
    "target_id": "table_1",
    "state": "succeeded"
  }
}
```

轮询成功后，所有策略都必须满足：

```text
state=succeeded
result.success=true
result.object_grasped=false
result.released=true
result.holding=null
result.policy_stopped=true
result.navigation_port_ready=true
completed_at存在
```

当前 `hand_state_only` 还必须满足：

```text
result.object_at_target 为 null 或 true
result.evidence_level=hand_state_only
result.evidence.hand=left
result.evidence.hand_open_confirmed=true
result.evidence.confirmed_frames>=3
```

照片判真关闭时，放置成功后使用 `verified_state=PLACE_CONFIRMED_BY_VLA`、`holding=null`。

### 8.8 放置图片和最终状态（可选）

仅 `place_photo_verification_enabled=true` 时调用 `/v1/camera/snapshots`，`purpose=verify_place`、`source_command_id=vla-place-001`，并要求新图晚于放置 `completed_at`。

VLM固定输出：

```json
{
  "target_visible": true,
  "target_on_table1": true,
  "target_in_gripper": false,
  "confidence": 0.91,
  "reason": "目标位于table1表面且不在夹爪中"
}
```

LLM固定输出：

```json
{
  "verified": true,
  "operation": "place",
  "object_id": "cola_can_1",
  "target_id": "table_1",
  "reason": "VLA释放状态与动作后图像一致"
}
```

全部通过后一次性更新：

```json
{
  "state": "SUCCEEDED",
  "runtime_phase": "SUCCEEDED",
  "verified_state": "PLACE_VERIFIED",
  "holding": null,
  "object_location": "table_1"
}
```

当前两个照片开关均关闭时，最终任务状态为：

```json
{
  "state": "COMPLETED_HAND_STATE_ONLY",
  "runtime_phase": "COMPLETED_HAND_STATE_ONLY",
  "verified_state": "PLACE_CONFIRMED_BY_VLA",
  "holding": null,
  "object_location": "table_1",
  "evidence_level": "vla_only"
}
```

该终态只证明当前手状态证据和接口终态满足，不证明物体已被视觉确认放到桌面。

## 9. 本地状态保存

任务状态由 Runtime 单写，当前账本位于 `data/tasks/`。`KernelStore` 使用目录互斥、版本检查和原子文件替换，保存每一步的尝试、不可变请求、原 `command_id` 及证据。旧接待记录在 `data/retired/reception/` 仅作历史留存，不作为新内核的续跑入口。

动作发送前记录意图；重启或丢回包时优先核查原命令。未知结果不转换为成功，也不靠删除账本、清 holding 或 `force_new_task` 放行新动作。

## 9.1 同一 Runtime 的继续与人工重试

| 项 | 当前行为 |
| --- | --- |
| 入口 | `POST /publish_task` 的 `resume: true`，以及 `/api/task_continue`，均进入大脑的原任务控制通路 |
| 任务身份 | 沿用原 `task_id`，附着账本记录的业务包、执行后端和目标地址；不重新发布一整单 |
| `recovery_required` | 先查询原 `command_id`；结果未核清时不生成新物理命令 |
| `paused` / `waiting_human` | 按各自状态继续，重新经过相应条件检查；继续不等于已取得身体动作许可 |
| 新尝试 | 人工继续核对旧尝试停止、资源释放和明确未完成后才创建；自动恢复预算仍为 0，只读重查不增加 `-r{n}` 计数 |
| 取消与新任务 | 取消受理和停止完成分别记录；`force_new_task` 不绕过未知命令或资源隔离 |

当前不承诺下游跨重启一定保留历史命令。若原命令不可查，保持 `recovery_required`，不以强制新建或从更早步骤重做来替代核查。控制交接、效果证据、取消确认及未完成范围统一见[重构规划第 17 节](规划说明_具身大脑重构.md#17-统一规划口径控制交接与断点恢复2026-09-24)。

## 9.2 DREAM HTTP 不确定事务处理

导航 POST 发送前先持久化原 `command_id`。异常处理遵循：

- 收到明确 HTTP 错误响应时，该响应不是“投递结果未知”；按契约错误停止当前 Pipeline，不更换 ID 重发；
- 连接断开、超时等连接级异常可能发生在 DREAM 已接收命令之后，只能有界查询原 `command_id`；
- 查到原命令后继续跟踪原事务，不创建新命令；
- 原 ID 连续 404、连接失败或有界查询后仍无法确认时，进入 `RECOVERY_REQUIRED`；
- 轮询阶段连续无法确认原命令同样进入 `RECOVERY_REQUIRED`，禁止通过新 ID 补发动作。

这套规则只避免重复执行，不把未知事务改写为成功或失败。

## 10. 当前代码落点

| 位置 | 职责 |
| --- | --- |
| `src/brain/api/app.py` | 大脑 API 路由，提交与控制任务 |
| `src/brain/service.py` | 附着原任务、规划与调度的应用入口 |
| `src/brain/kernel/runtime.py` | 状态迁移、原命令核查与恢复、取消收尾 |
| `src/brain/storage/tasks.py` | 任务和尝试账本 |
| `src/brain/packages/reception.py` | 接待相位与合同 |
| `src/brain/adapters/execution.py` | 真机 / 仿真执行适配，真实交接缺失时明确不可用 |
| `src/entries/web/`、`src/entries/feishu/`、`src/entries/voice/` | 独立入口，通过大脑接口操作任务 |

## 11. 验收边界

软件回放及工程部署记录见[重构规划第 16 节](规划说明_具身大脑重构.md#16-最终目录替换2026-09-24)。当前状态、三态核验、取消确认和原命令恢复必须符合该规划第 6、9、17 节；手部开合证据不等于物体实际到位。DREAM/VLA 报文仍为 `fq/reception-lan/v1`，真机派发仍需单独完成第 9.7 节的放行核查。


## 接待协议模拟路由（2026-09-28）

运行环境的接待模块可选 `simulation_backend: reception_protocol`。从正式 `/publish_task` 入口执行「开始接待」，任务状态与账本标记 `execution_backend=reception_protocol`，仍执行 `reception.single_can` v1。独立下游默认延时成功，任务核验不设模拟特例。旧 `/api/reception/run` 是旧会议演示入口，不用它验证本流程。

`handoff_observation` 现可包含共用可选合同 `fq/control-receipt/v1` 的核验结果。2026-10-08 起缺少该凭证时，以通路空闲作为确认（`transport_ready_without_receipt`），见[规划第 21 节](规划说明_具身大脑重构.md#21-真机联调放行规则2026-10-08)。详细启动、端点和真机差异见[接待协议模拟使用说明](使用说明_接待协议模拟.md)。

## 语音文本入口（2026-09-29）

`python -m entries.voice` 默认监听 `:8890`，用 `MASTER_URL`（默认 `http://127.0.0.1:5000`）连接大脑。它不采集音频，也不筛选文字。

`POST /publish_task` 要求 JSON 字段 `task` 为非空字符串。缺省时入口生成 `task_id`，并把 `refresh` 设为 true，然后原样转发到大脑 `POST /publish_task`。请求头固定为 `X-FQ-Source: voice`、`X-FQ-Via: voice`。空文本返回 400；大脑不可达返回 503。`GET /health` 只表示入口进程在听，不表示大脑已接受任务。

大脑收到后的分类、预检、控制和执行与网页、飞书提交的同一段文字相同。NX 上的 `reception_voice.py` 仍只向网页 `:8888` 发送固定任务「开始接待」，尚未改为调用本入口。
