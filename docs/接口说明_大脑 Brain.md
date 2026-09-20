# 接口说明：大脑 Brain

初版日期：2026-08-27  
当前现场口径：2026-09-20  
契约版本：`fq/reception-lan/v1`  
适用范围：单罐接待固定 Pipeline、DREAM/VLA 调用、状态保存、断点继续与可选照片判真。

> 本文是大脑端对外调用和编排行为的唯一维护文档。当前配置为真机接待启用、`hand_state_only`、抓取/放置照片判真关闭。文中的 VLM/LLM 图片判真章节只在对应开关启用时适用。

## 1. 大脑侧职责

大脑侧 Master 是唯一任务编排者，负责：

- 接收网页“开始接待”；
- 创建并维护一个 `task_id`；
- 读取 DREAM 世界、关系图和状态；
- 按顺序提交四段导航并轮询真实终态；
- 显式提交 VLA 抓取、放置任务并轮询真实终态；
- 当前按 `hand_state_only` 证据推进；照片开关启用时才从 VLA 获取动作后图片并执行 VLM/LLM 判真；
- 根据当前证据策略满足对应硬条件后，更新业务状态并进入下一步；
- 任一步失败立即停止，不调用后续动作；
- 真机接待失败后，网页「断点继续」可从 `failed_phase` 接着发导航/VLA（见第 9.1 节）。

大脑侧不负责：

- 不连接 SONIC 5556；
- 不发布 ROS goal、cmd_vel 或 Token；
- 不模拟 DREAM 9882 Approve；
- 不直接打开 RealSense；
- 不允许 LLM 改变固定 Pipeline 顺序。

顶层任务结束后，Master 用统一 episode 账本做事后复盘（DeepSeek，失败降模板）。这是读账本写候选经验，不是规划器，不改本轮动作顺序。默认只写入 `master/memory/reflections/`；mock 接待仍可 `write_sop`。飞书任务卡展示「复盘」。当前用法见 [项目 README](../README.md)。

## 2. 局域网拓扑

| 服务 | 地址 | 调用方向 | 用途 |
|---|---|---|---|
| 大脑网页/Master | `http://<BRAIN_IP>:8888` | 浏览器 → 大脑 | 创建和查看任务 |
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

## 9. 本地状态保存，不使用数据库

```text
master/sop/runtime/reception/
  current_task.json
  events.jsonl
  verification.jsonl
  images/pick_after.jpg
  images/place_after.jpg
```

- 内存对象用于网页实时显示；
- `current_task.json` 使用临时文件写入后原子替换；
- 调用 DREAM/VLA 前先保存对应 `command_id`；
- `events.jsonl` 记录请求、轮询终态和状态切换；
- `verification.jsonl` 记录图片元数据、VLM结果和LLM结果；
- Master 重启发现非终态任务时标记 `RECOVERY_REQUIRED`，本轮不自动补发动作；
- 操作员修好现场后，可用「断点继续」人工续跑（见下节），不要用「开始接待」+ `force_new_task` 覆盖，除非确认要整单重开。

## 9.1 真机接待断点继续（联调 MVP，已落地）

范围只覆盖真机「开始接待」单链路，不改导航/VLA 下游协议，也不做通用 LLM 任务续跑。

| 项 | 行为 |
|---|---|
| 网页 | `:8888`「断点继续」按钮，确认后 `POST /publish_task` 带 `resume: true`、`task: "开始接待"` |
| Master | 跳过新任务空手预检；读 `current_task.json` 的 `failed_phase`，**沿用同一 `task_id`** |
| 编排 | `reception_real.run(..., resume=True)` 从失败相位起继续发 DREAM/VLA；已完成步骤不重发 |
| 持物 | 续跑预检允许 VLA `holding`（例如抓取后停在 relay2 时的 `cola_can_1`） |
| 命令 ID | 重试步使用 `…-rN` 后缀，避免与上次失败指令撞车 |
| 状态 | `GET /api/task_status` 在存在 `failed_phase` 且未在跑时可 `can_resume: true` |
| 放弃本轮 | 仍用确认后的 `force_new_task` 整单重开（会清空 holding/commands） |

已知限制：DREAM/VLA 进程重启导致历史成功记录丢失时，续跑可能仍失败，只能 `force_new_task` 或从更早步重试。完整跨重启恢复见 [规划说明：中断恢复与断点续跑](规划说明_中断恢复与断点续跑.md)，该文其余接口仍属设计稿。

## 9.2 DREAM HTTP 不确定事务处理

导航 POST 发送前先持久化原 `command_id`。异常处理遵循：

- 收到明确 HTTP 错误响应时，该响应不是“投递结果未知”；按契约错误停止当前 Pipeline，不更换 ID 重发；
- 连接断开、超时等连接级异常可能发生在 DREAM 已接收命令之后，只能有界查询原 `command_id`；
- 查到原命令后继续跟踪原事务，不创建新命令；
- 原 ID 连续 404、连接失败或有界查询后仍无法确认时，进入 `RECOVERY_REQUIRED`；
- 轮询阶段连续无法确认原命令同样进入 `RECOVERY_REQUIRED`，禁止通过新 ID 补发动作。

这套规则只避免重复执行，不把未知事务改写为成功或失败。

## 10. 建议代码落点

```text
master/agents/agent.py                 # “开始接待”入口；resume 续跑；顶层任务收尾触发复盘
master/run.py                          # publish_task 支持 resume，续跑时跳过新任务预检
deploy/templates/index.html            # 「断点继续」按钮
master/sop/reception_skill.py          # mock/real；透传 resume
master/sop/reception_real.py           # 固定单链路；resume 从 failed_phase 切入
master/sop/episode.py                  # 仿真/真机共用的复盘账本
master/sop/reflect.py                  # 事后复盘（默认只落 candidates，不改 SOP）
master/integrations/dream_client.py    # DREAM HTTP客户端
master/integrations/vla_client.py      # VLA任务与相机HTTP客户端
master/integrations/reception_verify.py# VLM + LLM判真
master/sop/runtime/reception/          # JSON/JSONL/图片证据
```

真机入口逻辑：

```python
if reception_mode == "mock":
    run_reception_mock(...)
else:
    run_reception_real(..., resume=resume)
```

`run_reception_real()` 位于现有 Master 进程内，不新增独立服务。

## 11. 大脑侧完成判据

- 可以配置并访问 DREAM、VLA 两个局域网基址；
- 严格按固定 Pipeline 提交动作；
- 每个外部动作均保存并轮询 `command_id`；
- VLA 失败时不取图、不调用后续导航；
- `hand_state_only` 下抓取/放置必须满足左手状态、连续帧、策略停止和通路归还；
- 对应照片开关开启时，每个动作只取一张动作后新图，且 VLM/LLM 判真前不写入视觉成功；
- 照片开关关闭时终态为 `COMPLETED_HAND_STATE_ONLY`，不得对外表述为视觉验收成功；
- 真机接待失败后可用「断点继续」从 `failed_phase` 接着发，且不重做已成功的抓取。
