# 接口说明：操控 VLA

初版日期：2026-08-27  
远端现场记录基线：2026-09-20；大脑调用规则核对：2026-10-09
契约版本：`fq/reception-lan/v1`  
适用范围：接收大脑显式 pick/place 任务，返回真实终态；照片判真启用时订阅唯一 RealSense 相机服务并提供动作后图片。

> 本文维护操控 VLA 对外合同。大脑调用和核验以新增的 2026-10-09 核准说明及大脑接口文档为准；下方原现场记录保留当时的远端能力，后续报文示例包含完整目标合同。本次不把历史远端能力重新认定为当前已验收能力。

## 大脑调用与核验规则（2026-10-09）

本节已按当前大脑代码核准；后面的远端实现记录属于原标注日期的现场基线，本次没有重新验收 VLA 服务、策略、相机或全部状态。地址按当前网络与生效配置解析，旧网段示例不作为当前连接地址。

| 场景 | 当前大脑行为 |
| --- | --- |
| 提交与查询 | 显式提交 pick/place，以 `task_id`、`command_id` 关联原事务，轮询终态；仅导航变体不下发操控 |
| 下游自报成功 | 还需核验物体效果、停止、资源归还、身份和时间；`hand_state_only` 不足以通过物体效果核验 |
| 暂停／执行中跳过 | 有当前操控命令时调用 `/v1/vla/tasks/{command_id}/cancel` 请求停止，再查询停止与资源；不是简单暂停策略时钟 |
| 人工继续 | 原动作明确未完成且已停、资源已释放等条件满足时，准备新的当前步骤尝试；原动作自报成功但证据弱时只核验，不盲目重复抓放 |
| 取消大脑任务 | 大脑本地结束编排和等待，不调用操控取消接口，也不宣称策略停止或 5556 已释放 |
| 导航被人工跳过 | 大脑仍可提交后续操控，并引用原真实导航命令；不把该命令改为成功。VLA 按自身前置检查决定接受或拒绝，不能保证执行 |

大脑当前效果条件：抓取要求 `success=true`、`object_grasped=true`、`holding` 等于目标物体，物体存在证据不能显式为 false；放置要求 `success=true`、`object_grasped=false`、`released=true`、`holding=null`、`object_at_target=true`，目标到位证据不能显式为 false。两者均要求成功终态、有效身份和完成时间、`policy_stopped=true`、`navigation_port_ready=true`，并且证据等级不能是 `hand_state_only`。适配器接受这些字段不等于独立传感器验证了物理效果。

普通交接无 `control_receipt` 时可依据原命令成功且已停、两侧无活动命令和通路就绪放行；返回凭证时仍核验其有效性。人工跳过跨越的交接例外、最终 `safe_idle` 以及任务状态字段统一见[大脑接口](接口说明_大脑%20Brain.md)。这些大脑规则不改变 VLA 本身的前置安全检查。

## 当前现场实现口径（2026-09-17）

### 正式链路

现场路径：`<VLA_BRIDGE_ROOT>`（由部署环境配置）

```text
DREAM / Agent (192.168.5.18:8001)
        ↓ HTTP 8091
g1_brain_vla_bridge
        ↓ 导航 relay 交出 5556
Phase-Aware Safe RTC (GR00T-N1.7-3B, dual camera, 30→50 Hz)
        ↓ pose / TCP 5556
已经运行的 SONIC + LinkerHand
        ↓
G1
```

桥接层不修改 SONIC、GR00T N1.7 或导航源码；真机动作只能经过已审核的 hook。抓取使用 `run_phase_aware_action_stack.sh`，该脚本只启动 policy server、Phase-Aware RTC client 和 keyboard publisher，复用已有 SONIC、NX 相机和 LinkerHand，不启动第二个 LowCmd 控制器。放置复用 `g1_navigation_vla_bridge/run_navigation_place_coke.sh`。

### 当前能力与证据边界

- `config.json` 已启用真实 pick/place hook，不是空后端；
- 默认证据等级为 `hand_state_only`：只能证明左手连续稳定闭合或张开，不能证明可乐罐确实在手中或已放到目标桌面；
- pick/place 的照片判真开关当前均为 `false`，图像不参与成功判定；
- pick 使用 `left_wrist`，place 使用 `ego_view`；桥接层只读订阅 NX 唯一相机服务，不直接打开 USB RealSense；
- place 在 DREAM 校验后进入 `waiting_operator_approval`，仍需现场操作者在 bridge tmux 中按 Enter；
- 自动安全门控在 SONIC 底层独立运行，不依赖 Brain HTTP 或 VLA hook 才能执行原生 vendor 接管与 `dunxia`。

因此，后文成功终态中的 `object_grasped`、`object_at_target`、负载、抬升和视觉证据是完整接口目标，不代表当前 `hand_state_only` 现场已经具备这些强证据。

### 安全门控

Bridge 只有在 SONIC、状态接口、相机、LinkerHand、LowCmd 所有权和安全模式均通过核验后才能报告可执行。`startup_safety_attested=true` 只表示启动时通过，不等同于持续 `sonic_healthy=true`；每个 pick/place hook 在取得 `5556` 前必须重新执行在线检查。

安全门控配置、启动、状态、停止、自检和人工操作见本文[操控侧检查与独立启停](#操控侧检查与独立启停)。

### 5556 所有权与动作时序

1. 常态下只有 sequential navigation relay 监听工作站 `5556`；
2. DREAM 命令必须证明导航成功、已停止且 `navigation_transport_ready=true`；
3. hook 重新验证 SONIC、安全监督器、相机和 LinkerHand；
4. relay 通过 ROS ACK 交出 `5556`，仅看到端口空闲不算合法交接；
5. Phase-Aware RTC 启动上层策略，不启动第二个 SONIC；
6. pick hook 发送 prompt、`k`、`i`、`p` 并等待左手闭合证据；
7. 策略停止且确认 `5556` 释放后，relay 才能通过 ROS ACK 恢复导航。

任一步无法证明时失败关闭，不并行启动第二个 `5556` publisher。取消也必须先停止策略、确认 `5556` 释放并恢复 relay，才能返回 `policy_stopped=true` 和 `navigation_port_ready=true`。

### DREAM 动作前只读校验

动作前读取：

```http
GET {DREAM_BASE_URL}/v1/commands/{dream_command_id}
GET {DREAM_BASE_URL}/v1/status
```

命令必须匹配 `command_id`、`target_id`、`state=succeeded`、`result.success`、`reached` 和 `navigation_stopped=true`；总状态必须满足：

- `active_command_id == ""`
- `active_command_state == null`
- `navigation_transport_ready == true`

字段缺失或无法证明时不得启动动作。

以下字段名不是当前 DREAM 权威成功凭证，不能用来替代上述查询结果：

- `navigation_active`
- `navigation.state`
- `active_command.state`
- 仅凭 5556 端口空闲或 TCP 已连接

如果命令、目标、终态或总状态无法从权威接口证明，返回 `NAVIGATION_STATUS_UNVERIFIABLE`，不得启动 pick/place。网络查询失败也不能降级为“默认导航已停止”。

### place 人工批准

place 在 DREAM 凭证校验后进入 `waiting_operator_approval`。只有现场操作者确认后才能开始动作；等待超时或操控命令被取消时不得启动 place。进入终端和执行批准的步骤见本文[现场批准操作](#现场批准操作)。

## 操控侧检查与独立启停

整理日期：2026-10-09。面板调用的 `check → live-dependencies → start → status` 和独立 `stop` 已对照本仓库 `src/ops/vla_remote.py`；安全门配置、远端状态字段、人工批准及自检行为沿用 2026-09-20 现场记录，本次未重新验证远端部署。远端版本变动时需在此更新，不能把这些记录视为本次真机验收。

三端冷启动与收工顺序见[控制面板接口说明](接口说明_控制面板.md#三端启动与任务发布)。以下 SSH 别名应事先配置；`<VLA_BRIDGE_ROOT>` 为桥接目录，`<VLA_PROJECT_ROOT>` 为其所属项目目录。面板使用当前网络的操控角色地址，桥接目录可用 `VLA_REMOTE_DIR` 覆盖。

正常冷机流程由 DREAM 一键脚本依次启动 NX SONIC、相机、灵巧手、VLA HTTP/relay 和导航工作流。本节只用于单独核查或重启操控侧，不能代替冷机一键流程。

### 操控侧前提

- 机器人有可靠支撑，急停由现场人员掌握；
- SONIC 已进入稳定站立，是物理 `rt/lowcmd` 所有者并启用 persistent controller；
- NX 相机 `:5555`、SONIC state `:5557` 和 LinkerHand 在线；
- 当前没有正在执行或状态未核清的 VLA 任务；
- 只允许一个合法发布者占用工作站 `:5556`。

安全门控位于 `<VLA_PROJECT_ROOT>/sonic_safety_gate/config.env`。2026-09-20 现场记录中的默认值：

```bash
SONIC_SAFETY_GATE_MODE=automatic
```

`automatic` 要求安全门启用、supervisor 在线且为 `WAITING`。`off` 只允许用于尚未移植 vendor 安全运行时的设备，并要求运行中 SONIC 同样显示为 `legacy/off`。

### 静态与实时依赖检查

```bash
ssh gpu4090
cd <VLA_BRIDGE_ROOT>

./run_agent_vla_runtime.sh check
./run_phase_aware_action_stack.sh live-dependencies
```

两项检查均不执行 pick/place。实时检查必须确认 SONIC、状态、相机、LinkerHand、LowCmd 所有权和安全模式一致，并且没有第二个不受管的 `:5556` publisher。

### 独立启动与状态

只重启 VLA HTTP `:8091` 和受管导航 relay `:5556`：

```bash
cd <VLA_BRIDGE_ROOT>
BRAIN_VLA_REAL_ACTION_ACK=PHYSICAL_ESTOP_READY \
  ./run_agent_vla_runtime.sh start

./run_agent_vla_runtime.sh status
curl --noproxy '*' -s \
  http://127.0.0.1:8091/v1/vla/control/status \
  | python3 -m json.tool
```

`BRAIN_VLA_REAL_ACTION_ACK` 只表示现场已完成人工安全确认，不能替代支撑、急停和在线依赖检查。独立启动不会启动第二个 SONIC，也不会直接执行动作。

状态至少核对：

- `service_ready=true`；
- `policy_running=false`；
- `active_command_id` 为空；
- 导航 relay 是常态所有者；
- 相机、SONIC 和灵巧手满足动作前置。

`startup_safety_attested=true` 只证明启动时检查通过；每个 pick/place hook 在取得 `:5556` 前仍会重新检查实时依赖。

### 现场批准操作

place 通过 DREAM 凭证校验后进入 `waiting_operator_approval`：

```bash
cd <VLA_BRIDGE_ROOT>
./run_brain_vla_bridge.sh attach
```

现场确认目标、机器人状态和急停后，在该终端按 Enter。操控命令等待超时或被操控端取消时不得启动 place；大脑、面板和脚本都不能模拟该批准。大脑网页的“取消任务”只结束本地编排，不调用操控取消接口，不能据此认定远端批准等待已经结束。

### 无动作安全自检与实现测试

安全门闭环 shadow 测试：

```bash
cd <VLA_PROJECT_ROOT>
./sonic_safety_gate/manage.sh closed-loop-shadow-test
```

该测试只注入隔离 SOC 序列，不启动 `wbControl`、不发 FSM、不写 LowCmd。

脚本语法、Python 编译和单元测试：

```bash
cd <VLA_PROJECT_ROOT>
bash -n g1_brain_vla_bridge/*.sh
g1_groot_n17_inference/.venv/bin/python \
  -m py_compile g1_brain_vla_bridge/*.py
g1_groot_n17_inference/.venv/bin/python \
  -m unittest discover -s g1_brain_vla_bridge/tests -v
```

测试通过不等于真机动作已经验收。

### 独立停止

```bash
cd <VLA_BRIDGE_ROOT>
./run_agent_vla_runtime.sh stop
```

该命令只停止 Brain HTTP 和受管 relay，不停止 SONIC、相机或灵巧手；任务执行中会拒绝停止。如果策略或 `:5556` 所有权未核清，先处理原任务，不能强杀进程伪造安全收尾。

检查失败时先查看原始 blocker：HTTP 在线而 `service_ready=false` 不代表可执行；仅 `:5556` 空闲也不代表交接完成。独立停止被拒绝时保留原 `command_id`，核查操控端任务与控制权，按操控命令取消接口处理；不能用大脑“取消任务”冒充远端已停止。

## 1. VLA侧职责

VLA侧负责：

- 在局域网提供 HTTP 服务；
- 只在收到大脑明确任务后执行抓取或放置；
- 使用 `task_id` 和 `command_id` 建立幂等事务；
- 执行前确认导航已停止、动作端口可安全切换；
- 抓取后判断是否真正抓住目标，而不是只判断动作程序结束；
- 放置后返回夹爪已释放、当前 `holding=null`；
- 无论成功、失败或取消，都停止策略并恢复导航动作通路；
- 照片判真启用时，只读订阅 NX 上唯一打开 RealSense 的相机服务，不重复打开 USB 设备；
- 照片判真启用且 VLA 终态成功后，按大脑请求提供一张动作完成后的新图片。

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
  port: 8091
  contract_version: "fq/reception-lan/v1"

dream:
  base_url: "http://192.168.5.18:8001"
  request_timeout_sec: 5

camera:
  owner: "vla_camera_subsystem"
  source: "tcp://192.168.5.240:5555"
  view: "left_wrist"
  keep_streaming_after_policy: true
  snapshot_dir: "runtime/snapshots"

task:
  poll_status_retention_sec: 3600
  allow_parallel_tasks: false
```

VLA 服务默认监听 `0.0.0.0:8091`。防火墙放行 TCP 8091。VLA 主机必须可以访问 DREAM 8001，以便只读验证导航凭证。旧地址 `192.168.0.185` 已不再使用。

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
   GET http://192.168.5.18:8001/v1/commands/{dream_command_id}
   GET http://192.168.5.18:8001/v1/status
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

`object_at_target` 是 VLA 自身判断。严格物体证据策略要求它为 `true`；历史 `hand_state_only` 回包可能为 `null`，但当前共用大脑不生成 `COMPLETED_HAND_STATE_ONLY`，也不把这种弱证据记为放置通过。旧照片开关不能作为当前图片判真链路已接入的依据；当前大脑核验规则见本文开头及大脑接口第 8.2 节。

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

## 9. 操控命令取消接口

### `POST /v1/vla/tasks/{command_id}/cancel`

请求：

```json
{
  "task_id": "reception-20260827-001",
  "reason": "operator_cancelled"
}
```

合同约定返回 HTTP 202，VLA进入 `stopping_policy → restoring_navigation → cancelled`。当前大脑在暂停／执行中跳过时调用该接口，并查询原命令的停止与资源回执；需要 `policy_stopped=true` 和 `navigation_port_ready=true`，不能仅凭受理或终态名称放行。原命令已成功时保留其真实终态，不为暂停伪造取消。

网页“取消任务”及大脑 `/api/task_cancel` 不调用本接口。这里取消的是一条操控命令，不是整个大脑流程。

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
| `NAVIGATION_STATUS_UNVERIFIABLE` | 无法从 DREAM 权威接口证明导航终态和总状态 |
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


## 协议模拟与控制器凭证扩展（2026-09-28）

本项目新增两个独立的本机协议模拟进程，用正式单罐请求与查询接口验证共用大脑流程。默认成功是模拟场景结果，不是现场动作或传感器验收；弱证据不会被大脑提升为物体成功。使用方法与差异见[模拟真机接口说明](接口说明_模拟真机.md)。

本轮另增加可选状态字段 `control_receipt`，版本 `fq/control-receipt/v1`。VLA 在 `/v1/vla/control/status` 中返回 `to_vla/manipulation` 或 `safe_idle/safe_idle`。字段绑定 `task_id`、`source_command_id`、`kind`、`controller`、`active_command_id=null`、`confirmed=true` 和带时区的 `observed_at`。具体时效与来源要求见上述说明。

这是新增目标契约，尚未声称真机实现。2026-10-08 起大脑把它当作可选：服务无此字段时，在原命令已停止、两侧无活动命令且通路就绪的条件下放行，记为 `transport_ready_without_receipt`；返回了该字段的仍须核验通过。这只是联调放行规则，通路空闲仍不等于目标控制器已经接管，真机适配仍应从实际控制器生成该凭证。大脑具体判断统一见[大脑接口第 8.3 节](接口说明_大脑%20Brain.md#83-控制交接与人工跳过)。
