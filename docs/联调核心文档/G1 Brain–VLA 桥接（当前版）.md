# G1 Brain–VLA 桥接（当前版）

日期：2026-09-17  
契约版本：`fq/reception-lan/v1`  
现场路径：`<VLA_PROJECT_ROOT>`  
适用范围：DREAM / Agent 与 G1 真机 VLA 之间的独立 HTTP 边界。

本文是当前现场执行口径。它不修改 SONIC、GR00T N1.7 或导航源码；真机动作只能经过本目录内经审核的 hook 执行。8 月 VLA 联调文档保留当时契约与决策，不代表当前启动方式、地址或证据等级。

## 1. 当前正式组合

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

抓取不再调用旧的 `g1_groot_n17_inference/run_real_inference_stack.sh`，而是调用 `run_phase_aware_action_stack.sh`。该脚本只启动 policy server、Phase-Aware RTC client 和 keyboard publisher，复用现有 SONIC、NX 相机和 LinkerHand，不会启动第二个 LowCmd 控制器。

## 2. 当前真机能力与边界

- `config.json` 已启用真实 pick/place hook，不是空后端。
- pick 使用 GR00T N1.7 Phase-Aware RTC；place 复用经审核的 `g1_navigation_vla_bridge/run_navigation_place_coke.sh`。
- 默认证据等级为 `hand_state_only`：可证明左手连续稳定闭合/张开，不能证明可乐罐确实在手中，也不能证明已放在目标桌面。
- place 仍要求现场操作者在 bridge tmux 中按 Enter；这是业务动作确认，不是底层安全门控确认。
- 自动安全门控在 SONIC 底层独立运行。低电、温度预警触发时，不依赖 Brain HTTP 服务或 VLA hook 才能执行原生 vendor 接管与 dunxia。

## 3. 安全门控集成

统一配置位于 `../sonic_safety_gate/config.env`：

```bash
SONIC_SAFETY_GATE_MODE=automatic  # 本机默认
# SONIC_SAFETY_GATE_MODE=off      # 未移植 vendor 运行时的 G1
```

### 3.1 `automatic` 模式

Bridge 启动前必须确认：

- SONIC 是物理 `rt/lowcmd` 所有者；
- `--persistent-controller` 已启用；
- Automatic safety gate 已启用；
- supervisor 在线且为 `WAITING`；
- SONIC state `5557`、NX camera `5555` 及 VLA LinkerHand 均在线。

HTTP 状态中的 `startup_safety_attested=true` 只代表启动时通过。它不伪装成持续 `sonic_healthy=true`；每个 pick/place hook 在取得 `5556` 之前会重新执行完整在线检查。

### 3.2 `off` 模式

`off` 模式只在运行中 SONIC 同样显示为 `legacy/off` 时接受，不会把未移植的安全功能报成已启用。它仍要求 persistent SONIC、物理 `rt/lowcmd`、相机、状态和 LinkerHand 在线。

## 4. 启动顺序

前提：SONIC 已用项目原有命令启动，并已经过 `k` / `i` 进入稳定站立；NX 相机和 VLA LinkerHand 已运行。

先做静态检查（不发送动作）：

```bash
cd <VLA_PROJECT_ROOT>
./run_agent_vla_runtime.sh check
```

再做实时依赖检查（不发送动作）：

```bash
./run_phase_aware_action_stack.sh live-dependencies
```

正式启动 Brain HTTP + 受管导航 relay：

```bash
BRAIN_VLA_REAL_ACTION_ACK=PHYSICAL_ESTOP_READY \
  ./run_agent_vla_runtime.sh start
```

查看状态：

```bash
./run_agent_vla_runtime.sh status
curl --noproxy '*' -s http://127.0.0.1:8091/v1/vla/control/status | python3 -m json.tool
```

停止：

```bash
./run_agent_vla_runtime.sh stop
```

停止命令只停 Brain HTTP 和受管 relay，不停 SONIC、相机或灵巧手。若任务正在执行，它会拒绝停止。

## 5. 5556 所有权与 pick 时序

1. 常态下只有 sequential navigation relay 监听工作站 `5556`。
2. DREAM 命令必须证明导航成功、已停止且 transport ready。
3. pick hook 重新验证 SONIC / 安全监督器 / 相机 / LinkerHand。
4. relay 通过 ROS ACK 交出 `5556`；仅看到端口空闲不算合法交接。
5. `run_phase_aware_action_stack.sh` 启动上层 RTC，不启动第二个 SONIC。
6. hook 发送 prompt、`k`、`i`、`p`，并等待左手闭合证据。
7. 策略停止、`5556` 确认释放后，relay 才能通过 ROS ACK 恢复导航。

任意一步不可证明时失败关闭，不并行启动第二个 `5556` publisher。

## 6. HTTP 接口与 DREAM 校验

### 6.1 接口

- `GET /health`
- `GET /v1/vla/control/status`
- `POST /v1/vla/tasks`
- `GET /v1/vla/tasks/{command_id}`
- `POST /v1/vla/tasks/{command_id}/cancel`
- `GET /v1/camera/status`
- `POST /v1/camera/snapshots`
- `GET /v1/camera/snapshots/{snapshot_id}/rgb`

默认监听 `0.0.0.0:8091`。DREAM 地址来自统一网络配置，当前为 `http://192.168.5.18:8001`，不再使用旧的 `192.168.0.185`。

### 6.2 动作前只读校验

动作前会读取：

- `GET {DREAM_BASE_URL}/v1/commands/{dream_command_id}`
- `GET {DREAM_BASE_URL}/v1/status`

命令必须匹配 `command_id` / `target_id` / `state=succeeded` 与 `result.success` / `reached` / `navigation_stopped=true`；总状态必须为：

- `active_command_id == ""`
- `active_command_state == null`
- `navigation_transport_ready == true`

字段缺失或无法证明时不启动动作。

## 7. 相机、证据和 place 批准

Bridge 不直接打开 USB RealSense，只订阅 NX 唯一相机服务。pick 对应 `left_wrist`，place 对应 `ego_view`。当前两个照片判真开关都为 `false`，因此图像不参与成功判定。

place 在 DREAM 校验后进入 `waiting_operator_approval`。执行：

```bash
./run_brain_vla_bridge.sh attach
```

检查现场后在该 tmux 终端按 Enter。等待超时或取消时不启动 place。

## 8. 无动作安全自检

```bash
cd <VLA_WORKSPACE>
./sonic_safety_gate/manage.sh closed-loop-shadow-test
```

该测试只注入隔离 SOC 序列，要求 supervisor ACK 匹配且物理 writer GUID 不变，不启动 `wbControl`、不发 FSM、不写 LowCmd。

## 9. 取消与安全结束

如果 hook 被取消，必须先停策略、确认 `5556` 释放并恢复导航 relay，才返回 `policy_stopped=true` 和 `navigation_port_ready=true`。如果不能证明这两项，HTTP 层不会报告任务安全结束。

## 10. 验证

```bash
cd <VLA_WORKSPACE>
bash -n g1_brain_vla_bridge/*.sh
g1_groot_n17_inference/.venv/bin/python -m py_compile g1_brain_vla_bridge/*.py
g1_groot_n17_inference/.venv/bin/python -m unittest discover \
  -s g1_brain_vla_bridge/tests -v
```

`./run_agent_vla_runtime.sh check` 是静态检查，不启动机器人动作。
