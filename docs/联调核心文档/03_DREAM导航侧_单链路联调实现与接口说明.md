# DREAM导航侧：单链路联调实现与接口说明

日期：2026-08-27  
契约版本：`fq/reception-lan/v1`，底层导航接口基线 `dream/g1-agent-*/v1`  
联调范围：向大脑提供世界和四段安全导航；不自动触发 VLA，不占用本轮 RealSense。

## 1. DREAM侧职责

DREAM侧负责：

- 在局域网 `192.168.0.185:8001` 提供 HTTP 服务；
- 提供健康、总状态、世界入口和关系图；
- 接收大脑显式提交的 table2、relay2、relay3、table1 导航命令；
- 保留 9882人工审核、定位、规划、Arm、Gateway、Token和碰撞安全链；
- HTTP 202后按 `command_id` 保存事务并提供状态查询；
- 返回真实 `succeeded/failed/cancelled` 终态；
- 对非法门段顺序、错误坐标系和错误动作模式拒绝执行；
- 保留命令结果，供大脑和 VLA 使用同一 `command_id` 查询导航凭证。

DREAM侧不负责：

- 不决定何时抓取或放置；
- 不调用 VLA；
- 不在到达 table2 后发布会自动启动 VLA 的 `/nav_done`；
- 不执行固定队列自动续航；
- 不允许 Agent绕过9882、Gateway、Token和碰撞门；
- 本轮不直接打开 RealSense，RealSense 由 VLA 唯一打开。

## 2. 局域网和服务配置

```yaml
server:
  host: "0.0.0.0"
  port: 8001
  contract_version: "fq/reception-lan/v1"

agent_mode:
  enabled: true
  auto_queue_enabled: false
  publish_nav_done_for_vla: false
  camera_driver_enabled: false
  retain_command_result_sec: 3600
```

防火墙放行 TCP 8001。大脑和 VLA 主机都需要能访问 8001：

- 大脑读世界、提交导航、轮询终态；
- VLA只读查询上一段导航凭证，不提交导航。

## 3. 统一字段和HTTP规范

### 3.1 标识和幂等

- `task_id`：整条接待任务唯一；
- `command_id`：每段导航唯一；
- 同一 `command_id`、相同请求体重复 POST：返回原事务，不重复导航；
- 同一 `command_id`、不同请求体：HTTP 409 `COMMAND_ID_CONFLICT`；
- 同一时间只允许一个活动导航命令；冲突请求返回 HTTP 409 `NAVIGATION_BUSY`。

### 3.2 时间

- 所有 JSON 时间为 ISO 8601 带时区格式；
- 每个命令至少保存 `accepted_at`、`started_at`、`completed_at`；
- `completed_at` 只有终态时才有值。

### 3.3 HTTP语义

- POST返回 HTTP 202 只表示 DREAM 已接收事务；
- 大脑必须轮询命令接口；
- 只有 `state=succeeded && result.success=true` 表示真实到达；
- HTTP断线、等待审核或 `motion_ready=false` 都不能被当成成功。

### 3.4 统一错误格式

```json
{
  "success": false,
  "error": {
    "code": "NAVIGATION_BUSY",
    "message": "已有导航命令正在执行",
    "retryable": false,
    "details": {
      "active_command_id": "nav-table2-001"
    }
  }
}
```

## 4. 必须实现的接口

| 方法 | 路径 | 调用方 | 用途 |
|---|---|---|---|
| GET | `/health` | 大脑 | HTTP健康 |
| GET | `/v1/status` | 大脑 | 世界、安全和活动命令状态 |
| GET | `/v1/world` | 大脑 | 世界资源入口 |
| GET | `/total_scene_graph_latest.json` | 大脑 | 关系图 |
| GET | `/map.yaml` | 大脑可选 | 地图元数据 |
| GET | `/map.pgm` | 大脑可选 | 占据图 |
| POST | `/v1/navigation/goals` | 大脑 | 提交导航 |
| GET | `/v1/commands/{command_id}` | 大脑、VLA只读 | 查询导航事务 |
| POST | `/v1/navigation/cancel` | 大脑 | 取消当前导航 |

本轮不要求大脑或VLA调用 DREAM `/v1/camera/*` 和 `/v1/inspection`。

## 5. 健康、状态和世界接口

### 5.1 `GET /health`

HTTP 200：

```json
{
  "contract_version": "fq/reception-lan/v1",
  "service": "dream-agent-http",
  "status": "ok",
  "interface_online": true,
  "time": "2026-08-27T14:20:00.000+08:00"
}
```

HTTP健康不表示当前允许运动。

### 5.2 `GET /v1/status`

HTTP 200示例：

```json
{
  "contract_version": "fq/reception-lan/v1",
  "interface_online": true,
  "world_state_available": true,
  "motion_ready": false,
  "motion_blockers": ["WAITING_OPERATOR_APPROVAL"],
  "localization_initialized": true,
  "localization_approved": false,
  "gateway_ready": false,
  "token_ready": false,
  "active_command_id": "nav-table2-001",
  "active_command_state": "waiting_operator_approval",
  "agent_mode": {
    "enabled": true,
    "auto_queue_enabled": false,
    "publish_nav_done_for_vla": false
  },
  "camera": {
    "driver_enabled": false,
    "external_owner": "vla"
  }
}
```

等待人工审核期间 `motion_ready=false` 是正常运行状态，不应返回HTTP服务错误。

VLA占有动作通路时，建议：

```json
{
  "motion_ready": false,
  "motion_blockers": ["VLA_ACTION_PORT_OWNED"]
}
```

### 5.3 `GET /v1/world`

```json
{
  "contract_version": "fq/reception-lan/v1",
  "frame_id": "map",
  "map_yaml_url": "/map.yaml",
  "map_image_url": "/map.pgm",
  "relation_graph_url": "/total_scene_graph_latest.json",
  "door_object_id": "door_1",
  "updated_at": "2026-08-27T14:19:55.000+08:00"
}
```

### 5.4 `GET /total_scene_graph_latest.json`

关系图至少能让大脑确认以下对象存在：

```json
{
  "frame_id": "map",
  "objects": [
    {"id": "table_2", "type": "table"},
    {"id": "door_1", "type": "door"},
    {"id": "table_1", "type": "table"}
  ],
  "updated_at": "2026-08-27T14:19:55.000+08:00"
}
```

对象ID必须与导航请求中的 `target_id` 完全一致。

## 6. 本轮固定导航合同

坐标系固定 `map`，位置单位米，yaw单位弧度。

| 顺序 | 阶段 | target_id | route_phase | leg_index | goal_xyt | motion_mode |
|---:|---|---|---|---:|---|---|
| 1 | table2 | `table_2` | 空 | 1 | `[0.9948137550501258, 1.402057782965935, -0.3193204258080712]` | `forward_path` |
| 2 | relay2 | `door_1` | `door_approach` | 2 | `[3.733075988421528, 6.215369909530748, 2.718279944258407]` | `forward_path` |
| 3 | relay3 | `door_1` | `door_lateral_exit` | 3 | `[4.185939449618811, 7.560143924693016, 2.7689146673931306]` | `lateral_path_aligned` |
| 4 | table1 | `table_1` | `table1_approach` | 4 | `[3.0873798986272165, 8.279995338440145, 1.175238157458919]` | `forward_path` |

门段合同：

```text
relay2 → relay3
path_heading_deg = 71.3886338891
body_yaw_deg = 158.6471242735
sideways_alignment_error_deg = 2.7415096157
motion_mode = lateral_path_aligned
```

DREAM、本地审核配置和现场运行配置必须统一成上述新 relay2/relay3，不能继续使用旧坐标。

顺序硬约束：

```text
table2 succeeded
→ VLA抓取和大脑判真（由大脑控制）
→ relay2 succeeded
→ relay3 succeeded
→ table1 succeeded
```

DREAM只需要对自身可见的导航顺序执行安全校验；不能因为 table2 succeeded 自动提交 relay2或触发VLA。

## 7. 提交导航接口

### 7.1 `POST /v1/navigation/goals`

通用请求Schema：

```json
{
  "contract_version": "fq/reception-lan/v1",
  "command_id": "nav-table2-001",
  "task_id": "reception-20260827-001",
  "target_id": "table_2",
  "route_phase": "",
  "leg_index": 1,
  "frame_id": "map",
  "goal_xyt": [0.9948137550501258, 1.402057782965935, -0.3193204258080712],
  "motion_mode": "forward_path",
  "require_final_orientation": true
}
```

字段约束：

| 字段 | 必填 | 规则 |
|---|---|---|
| `contract_version` | 是 | `fq/reception-lan/v1` |
| `command_id` | 是 | 每段唯一 |
| `task_id` | 是 | 四段相同 |
| `target_id` | 是 | `table_2`、`door_1`、`table_1` |
| `route_phase` | 是 | 门段必须准确；table2可为空 |
| `leg_index` | 是 | 1、2、3、4 |
| `frame_id` | 是 | 仅 `map` |
| `goal_xyt` | 是 | 三个有限数，必须匹配本轮合同 |
| `motion_mode` | 是 | `forward_path`或门段限定的`lateral_path_aligned` |
| `require_final_orientation` | 是 | 本轮固定true |

接收成功返回 HTTP 202：

```json
{
  "contract_version": "fq/reception-lan/v1",
  "accepted": true,
  "task_id": "reception-20260827-001",
  "command_id": "nav-table2-001",
  "target_id": "table_2",
  "state": "accepted",
  "status_url": "/v1/commands/nav-table2-001",
  "accepted_at": "2026-08-27T14:22:00.000+08:00"
}
```

### 7.2 接收时校验

DREAM必须校验：

1. 请求Schema和契约版本；
2. `frame_id=map`；
3. goal_xyt均为有限数；
4. target、route_phase、leg_index、坐标和motion_mode组合合法；
5. relay3只能在同一task_id的relay2成功后接收；
6. table1只能在同一task_id的relay3成功后接收；
7. 当前没有其他活动导航；
8. VLA没有占用动作通路；
9. Agent模式没有启用固定自动队列。

安全门未就绪不一定拒绝HTTP请求；可以接收事务后进入 `waiting_operator_approval` 或 `waiting_safety_ready`。

## 8. 导航状态查询

### 8.1 `GET /v1/commands/{command_id}`

状态枚举：

```text
accepted
→ waiting_operator_approval
→ planning
→ arming
→ waiting_safety_ready
→ navigating
→ succeeded / failed / cancelled
```

运行中示例：

```json
{
  "contract_version": "fq/reception-lan/v1",
  "task_id": "reception-20260827-001",
  "command_id": "nav-table2-001",
  "target_id": "table_2",
  "route_phase": "",
  "leg_index": 1,
  "state": "navigating",
  "accepted_at": "2026-08-27T14:22:00.000+08:00",
  "started_at": "2026-08-27T14:23:05.000+08:00",
  "completed_at": null,
  "result": null,
  "progress": {
    "message": "导航执行中"
  }
}
```

成功终态示例：

```json
{
  "contract_version": "fq/reception-lan/v1",
  "task_id": "reception-20260827-001",
  "command_id": "nav-table2-001",
  "target_id": "table_2",
  "route_phase": "",
  "leg_index": 1,
  "state": "succeeded",
  "accepted_at": "2026-08-27T14:22:00.000+08:00",
  "started_at": "2026-08-27T14:23:05.000+08:00",
  "completed_at": "2026-08-27T14:24:10.000+08:00",
  "result": {
    "success": true,
    "reached": true,
    "final_xyt": [0.995, 1.401, -0.320],
    "navigation_stopped": true,
    "message": "目标已到达"
  }
}
```

失败终态示例：

```json
{
  "contract_version": "fq/reception-lan/v1",
  "task_id": "reception-20260827-001",
  "command_id": "nav-table2-001",
  "target_id": "table_2",
  "state": "failed",
  "completed_at": "2026-08-27T14:24:10.000+08:00",
  "result": {
    "success": false,
    "reached": false,
    "navigation_stopped": true,
    "message": "路径执行失败"
  },
  "error": {
    "code": "NAVIGATION_EXECUTION_FAILED",
    "message": "未到达目标",
    "retryable": false,
    "details": {}
  }
}
```

命令终态至少保留一小时，保证大脑和VLA可以查询同一导航凭证。

## 9. 取消接口

### `POST /v1/navigation/cancel`

请求：

```json
{
  "task_id": "reception-20260827-001",
  "command_id": "nav-table2-001",
  "reason": "operator_cancelled"
}
```

返回 HTTP 202。大脑继续轮询原命令，直到 `cancelled`。

取消后 DREAM 可以撤销本轮 Arm 并要求现场重新审核定位。DREAM需要在状态中明确返回 `localization_approved=false` 和对应 `motion_blockers`，不得自动重新批准。

## 10. 推荐错误码

| 错误码 | 含义 |
|---|---|
| `INVALID_REQUEST` | 请求字段错误 |
| `UNSUPPORTED_CONTRACT_VERSION` | 契约版本不支持 |
| `COMMAND_ID_CONFLICT` | 同ID不同请求体 |
| `NAVIGATION_BUSY` | 已有活动导航 |
| `INVALID_FRAME_ID` | 非map坐标系 |
| `INVALID_GOAL_XYT` | 坐标非法或不匹配审核合同 |
| `INVALID_ROUTE_PHASE` | 路由阶段错误 |
| `INVALID_LEG_ORDER` | 门段/导航顺序错误 |
| `INVALID_MOTION_MODE` | 动作模式错误 |
| `VLA_ACTION_PORT_OWNED` | VLA仍占用动作通路 |
| `WAITING_OPERATOR_APPROVAL` | 等待9882人工审核，通常作为状态而非失败 |
| `SAFETY_NOT_READY` | Gateway/Token/定位等未就绪 |
| `NAVIGATION_EXECUTION_FAILED` | 导航执行失败 |
| `COMMAND_NOT_FOUND` | command_id不存在 |

## 11. 与VLA动作端口对齐

本轮动作顺序必须互斥：

```text
DREAM导航停止并返回succeeded
→ VLA验证导航凭证
→ VLA取得动作端口并执行
→ VLA停止策略
→ VLA返回navigation_port_ready=true
→ 大脑完成图片判真
→ 大脑提交下一段DREAM导航
```

DREAM收到导航请求时，如果 VLA 仍占用动作通路，必须拒绝或保持安全等待，不能同时向动作入口发送控制。

## 12. DREAM侧完成判据

- 大脑、VLA可通过局域网访问8001只读接口；
- 大脑可以提交并轮询四段导航；
- 四段使用统一task_id和不同command_id；
- relay2/relay3采用本文新坐标；
- relay2未成功时不执行relay3，relay3未成功时不执行table1；
- HTTP 202不被当作到达；
- 终态包含真实 `result.success`、时间和导航停止状态；
- 到达table2不会自动触发VLA；
- 固定自动队列已关闭；
- 本轮DREAM不打开RealSense；
- VLA查询原导航command_id时能得到相同终态。
