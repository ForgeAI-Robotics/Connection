# VLA侧：单链路联调实现与接口说明

日期：2026-08-27  
契约版本：`fq/reception-lan/v1`  
联调范围：接收大脑显式 pick/place 任务，订阅唯一RealSense相机服务，返回真实终态并提供动作后图片。

## 1. VLA侧职责

VLA侧负责：

- 在局域网提供 HTTP 服务；
- 只在收到大脑明确任务后执行抓取或放置；
- 使用 `task_id` 和 `command_id` 建立幂等事务；
- 执行前确认导航已停止、动作端口可安全切换；
- 抓取后判断是否真正抓住目标，而不是只判断动作程序结束；
- 放置后返回夹爪已释放、当前 `holding=null`；
- 无论成功、失败或取消，都停止策略并恢复导航动作通路；
- 只读订阅NX上唯一打开RealSense的相机服务，不重复打开USB设备；
- VLA终态成功后，按大脑请求提供一张动作完成后的新图片。

VLA侧不负责：

- 不决定何时去 table2、relay2、relay3 或 table1；
- 不因 `/nav_done`、ROS锁存消息或位置变化自动启动；
- 不编排下一段导航；
- 不直接修改大脑任务成功状态；
- 不把“夹爪闭合”直接等同于抓取成功。

## 2. 局域网与配置

建议配置：

```yaml
server:
  host: "0.0.0.0"
  port: <VLA_PORT>
  contract_version: "fq/reception-lan/v1"

dream:
  base_url: "http://192.168.0.185:8001"
  request_timeout_sec: 5

camera:
  owner: "vla_camera_subsystem"
  source: "tcp://192.168.0.240:5555"
  view: "left_wrist"
  keep_streaming_after_policy: true
  snapshot_dir: "runtime/snapshots"

task:
  poll_status_retention_sec: 3600
  allow_parallel_tasks: false
```

VLA服务监听局域网地址而非仅 `127.0.0.1`。防火墙放行 `<VLA_PORT>`。VLA主机必须可以访问 DREAM 8001，以便只读验证导航凭证。

## 3. 统一字段和HTTP规范

### 3.1 标识和幂等

- `task_id`：整条接待任务ID；
- `command_id`：本次 VLA 动作唯一ID；
- 同一 `command_id`、相同请求体重复 POST：返回已有事务，不重复动作；
- 同一 `command_id`、不同请求体：HTTP 409，错误码 `COMMAND_ID_CONFLICT`；
- 同一时间只允许一个活动 VLA 任务；第二个任务返回 HTTP 409 `VLA_BUSY`。

### 3.2 时间

- 所有时间使用带时区 ISO 8601；
- `completed_at` 是策略停止、动作终态确定的时间；
- 相机快照必须返回 `captured_at`；
- 对带 `captured_after` 的请求，只能返回严格晚于该时间的新帧。

### 3.3 错误格式

```json
{
  "success": false,
  "error": {
    "code": "VLA_BUSY",
    "message": "已有VLA任务正在执行",
    "retryable": false,
    "details": {
      "active_command_id": "vla-pick-001"
    }
  }
}
```

## 4. 必须实现的接口

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/health` | HTTP服务健康 |
| GET | `/v1/vla/control/status` | 策略、动作端口、相机总状态 |
| POST | `/v1/vla/tasks` | 提交 pick/place 异步任务 |
| GET | `/v1/vla/tasks/{command_id}` | 查询任务状态和终态 |
| POST | `/v1/vla/tasks/{command_id}/cancel` | 取消任务并安全释放资源 |
| GET | `/v1/camera/status` | RealSense状态 |
| POST | `/v1/camera/snapshots` | 动作完成后创建一张新快照 |
| GET | `/v1/camera/snapshots/{snapshot_id}/rgb` | 下载RGB图片 |

## 5. 健康和控制状态

### 5.1 `GET /health`

HTTP 200：

```json
{
  "contract_version": "fq/reception-lan/v1",
  "service": "vla-task-service",
  "status": "ok",
  "time": "2026-08-27T14:20:00.000+08:00"
}
```

这里只证明 HTTP 服务在线，不等于机械臂、相机和动作端口都可用。

### 5.2 `GET /v1/vla/control/status`

HTTP 200：

```json
{
  "contract_version": "fq/reception-lan/v1",
  "service_ready": true,
  "active_command_id": null,
  "policy_running": false,
  "action_port": {
    "owner": "navigation",
    "navigation_port_ready": true,
    "last_changed_at": "2026-08-27T14:19:58.000+08:00"
  },
  "camera": {
    "owner": "vla",
    "ready": true,
    "streaming": true,
    "latest_frame_id": "frame-10234",
    "latest_frame_at": "2026-08-27T14:19:59.900+08:00"
  },
  "robot": {
    "sonic_healthy": true,
    "gripper_ready": true
  }
}
```

`navigation_port_ready=true` 的含义是：VLA策略已停止，VLA不再向动作入口发送控制，导航侧可以重新使用动作通路。

## 6. 提交VLA任务

### 6.1 `POST /v1/vla/tasks`

抓取请求：

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

放置请求：

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

字段约束：

| 字段 | 必填 | 规则 |
|---|---|---|
| `contract_version` | 是 | 固定 `fq/reception-lan/v1` |
| `command_id` | 是 | 全局唯一、非空字符串 |
| `task_id` | 是 | 与导航任务相同 |
| `operation` | 是 | 仅 `pick` 或 `place` |
| `object_id` | 是 | 本轮固定 `cola_can_1` |
| `target_area` | 是 | pick=`table_2`，place=`table_1` |
| `navigation_proof` | 是 | 上一段 DREAM 成功凭证 |

接收成功返回 HTTP 202：

```json
{
  "contract_version": "fq/reception-lan/v1",
  "accepted": true,
  "task_id": "reception-20260827-001",
  "command_id": "vla-pick-001",
  "state": "accepted",
  "status_url": "/v1/vla/tasks/vla-pick-001",
  "accepted_at": "2026-08-27T14:30:00.000+08:00"
}
```

### 6.2 接收前检查

VLA服务在开始策略前必须完成：

1. 请求字段和契约版本合法；
2. 当前没有其他 VLA 任务；
3. 使用 `navigation_proof.dream_command_id` 调用：

   ```http
   GET http://192.168.0.185:8001/v1/commands/{dream_command_id}
   GET http://192.168.0.185:8001/v1/status
   ```

4. DREAM返回 `state=succeeded && result.success=true`，且 `target_id` 与请求一致；
5. DREAM `/v1/status` 显示没有正在运动的导航命令；
6. 动作端口可以从 navigation 安全切给 VLA；
7. RealSense在线，夹爪和SONIC健康；
8. pick前 `holding=null`；place前 `holding=cola_can_1`。

任一检查失败，任务进入 `failed`，不得启动策略。

## 7. VLA任务状态机

```text
accepted
→ verifying_navigation
→ waiting_navigation_release
→ acquiring_action_port
→ running
→ evaluating_action_result
→ stopping_policy
→ restoring_navigation
→ succeeded / failed / cancelled
```

所有状态通过 `GET /v1/vla/tasks/{command_id}` 返回。

## 8. 查询VLA任务

### 8.1 运行中响应

```json
{
  "contract_version": "fq/reception-lan/v1",
  "task_id": "reception-20260827-001",
  "command_id": "vla-pick-001",
  "operation": "pick",
  "state": "running",
  "accepted_at": "2026-08-27T14:30:00.000+08:00",
  "started_at": "2026-08-27T14:30:02.000+08:00",
  "completed_at": null,
  "result": null,
  "progress": {
    "phase": "lifting",
    "message": "目标抬升确认中"
  }
}
```

### 8.2 抓取成功终态

```json
{
  "contract_version": "fq/reception-lan/v1",
  "task_id": "reception-20260827-001",
  "command_id": "vla-pick-001",
  "operation": "pick",
  "state": "succeeded",
  "accepted_at": "2026-08-27T14:30:00.000+08:00",
  "started_at": "2026-08-27T14:30:02.000+08:00",
  "completed_at": "2026-08-27T14:30:18.500+08:00",
  "result": {
    "success": true,
    "object_id": "cola_can_1",
    "object_grasped": true,
    "holding": "cola_can_1",
    "released": false,
    "policy_stopped": true,
    "navigation_port_ready": true,
    "confidence": 0.93,
    "evidence": {
      "gripper_width": 0.034,
      "load_detected": true,
      "lift_test_passed": true,
      "wrist_track_passed": true
    },
    "message": "抓取完成，策略已停止，导航通路已恢复"
  }
}
```

抓取不得只根据动作脚本结束返回成功。建议至少组合：夹爪开度/负载、抬升测试、腕部视觉跟随。没有足够证据时返回 `failed`，不要返回伪成功。

### 8.3 放置成功终态

```json
{
  "contract_version": "fq/reception-lan/v1",
  "task_id": "reception-20260827-001",
  "command_id": "vla-place-001",
  "operation": "place",
  "state": "succeeded",
  "accepted_at": "2026-08-27T14:36:00.000+08:00",
  "started_at": "2026-08-27T14:36:02.000+08:00",
  "completed_at": "2026-08-27T14:36:15.200+08:00",
  "result": {
    "success": true,
    "object_id": "cola_can_1",
    "object_grasped": false,
    "holding": null,
    "released": true,
    "object_at_target": true,
    "policy_stopped": true,
    "navigation_port_ready": true,
    "confidence": 0.90,
    "message": "放置完成，夹爪已释放，导航通路已恢复"
  }
}
```

`object_at_target` 是 VLA自身判断，最终是否完成仍由大脑使用动作后图片判真。

### 8.4 失败终态

```json
{
  "contract_version": "fq/reception-lan/v1",
  "task_id": "reception-20260827-001",
  "command_id": "vla-pick-001",
  "operation": "pick",
  "state": "failed",
  "completed_at": "2026-08-27T14:30:18.500+08:00",
  "result": {
    "success": false,
    "object_id": "cola_can_1",
    "object_grasped": false,
    "holding": null,
    "released": false,
    "policy_stopped": true,
    "navigation_port_ready": true,
    "confidence": 0.95,
    "message": "夹爪完全闭合且未检测到负载，判定空抓"
  },
  "error": {
    "code": "EMPTY_GRASP",
    "message": "未抓住目标物体",
    "retryable": false,
    "details": {}
  }
}
```

失败时也必须先停止策略并恢复导航通路，再返回最终 `failed`。如果通路恢复失败，返回 `navigation_port_ready=false`，并使用 `ACTION_PORT_RESTORE_FAILED`。

## 9. 取消接口

### `POST /v1/vla/tasks/{command_id}/cancel`

请求：

```json
{
  "task_id": "reception-20260827-001",
  "reason": "operator_cancelled"
}
```

返回 HTTP 202，VLA进入 `stopping_policy → restoring_navigation → cancelled`。大脑继续轮询直到 `cancelled`，且应看到 `policy_stopped=true`。

## 10. RealSense与动作后图片

### 10.1 相机所有权

- 本轮只允许NX上的VLA相机服务直接打开RealSense；
- Brain–VLA HTTP桥只读订阅`.240:5555`，不直接打开USB；
- DREAM不启动第二个 RealSense 驱动；
- 大脑只通过 HTTP 获取图片，不直接打开设备；
- VLA策略停止后，相机流可以继续运行，以便大脑请求动作后新帧。

### 10.2 `GET /v1/camera/status`

```json
{
  "contract_version": "fq/reception-lan/v1",
  "owner": "vla",
  "ready": true,
  "streaming": true,
  "latest_frame_id": "frame-10500",
  "latest_frame_at": "2026-08-27T14:30:19.000+08:00",
  "width": 1280,
  "height": 720,
  "format": "rgb8"
}
```

### 10.3 `POST /v1/camera/snapshots`

请求：

```json
{
  "contract_version": "fq/reception-lan/v1",
  "task_id": "reception-20260827-001",
  "source_command_id": "vla-pick-001",
  "purpose": "verify_pick",
  "captured_after": "2026-08-27T14:30:18.500+08:00"
}
```

约束：

- `source_command_id` 必须存在且已经终态；
- `purpose` 仅 `verify_pick` 或 `verify_place`；
- VLA等待并截取第一张严格晚于 `captured_after` 的完整新帧；
- 不得返回缓存的动作前旧图。

成功返回 HTTP 201：

```json
{
  "contract_version": "fq/reception-lan/v1",
  "snapshot_id": "snap-vla-pick-001-001",
  "task_id": "reception-20260827-001",
  "source_command_id": "vla-pick-001",
  "purpose": "verify_pick",
  "frame_id": "frame-10501",
  "captured_at": "2026-08-27T14:30:19.050+08:00",
  "content_type": "image/jpeg",
  "width": 1280,
  "height": 720,
  "sha256": "<64位十六进制摘要>",
  "rgb_url": "/v1/camera/snapshots/snap-vla-pick-001-001/rgb"
}
```

### 10.4 `GET /v1/camera/snapshots/{snapshot_id}/rgb`

HTTP 200 返回 JPEG 或 PNG 字节，并设置：

```text
Content-Type: image/jpeg
X-Snapshot-Id: snap-vla-pick-001-001
X-Captured-At: 2026-08-27T14:30:19.050+08:00
X-Content-SHA256: <摘要>
```

图片必须保存到本地至少一小时，保证大脑轮询和下载期间 URL 有效。

## 11. 推荐错误码

| 错误码 | 含义 |
|---|---|
| `INVALID_REQUEST` | 字段缺失或格式错误 |
| `UNSUPPORTED_CONTRACT_VERSION` | 契约版本不支持 |
| `COMMAND_ID_CONFLICT` | 同ID不同请求体 |
| `VLA_BUSY` | 已有活动任务 |
| `NAVIGATION_PROOF_INVALID` | DREAM凭证不成功或目标不匹配 |
| `NAVIGATION_STILL_ACTIVE` | 导航仍在运动 |
| `ACTION_PORT_ACQUIRE_FAILED` | 无法取得动作端口 |
| `ACTION_PORT_RESTORE_FAILED` | 无法归还导航通路 |
| `CAMERA_NOT_READY` | RealSense不可用 |
| `GRIPPER_NOT_READY` | 夹爪不可用 |
| `EMPTY_GRASP` | 空抓 |
| `OBJECT_MISMATCH` | 抓到的不是请求目标 |
| `PLACE_FAILED` | 放置动作失败 |
| `SNAPSHOT_TOO_OLD` | 无法取得晚于指定时间的新帧 |
| `TASK_NOT_FOUND` | command_id不存在 |

## 12. VLA侧完成判据

- 大脑可通过局域网访问全部必需接口；
- 同一 command_id 重复提交不会重复动作；
- `/nav_done` 不会自动触发任何动作；
- pick返回真实 `object_grasped/holding`；
- place返回真实 `released/holding=null`；
- 每个终态都有 `completed_at`；
- 成功、失败、取消都能停止策略并给出 `navigation_port_ready`；
- VLA相机子系统独占RealSense，HTTP桥能按`captured_after`返回腕部相机新图片；
- VLA不自行启动下一段导航。
