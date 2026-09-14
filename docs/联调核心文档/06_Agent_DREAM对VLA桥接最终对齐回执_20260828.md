# Agent / DREAM 对 VLA 桥接最终对齐回执

日期：2026-08-28  
协议版本：`fq/reception-lan/v1`  
用途：供VLA组按本版完成桥接配置、真实hook和首次三方真机联调准备。

## 1. 当前总体结论

Agent与DREAM导航链已经完成真实联调：自然语言任务经过Master LLM拆解、Slaver工具调用、DREAM异步导航和真实终态轮询，机器人已两次到达table2。

当前仅缺VLA侧正式HTTP服务、真实pick/place hook和动作端口交接证据。完整Agent+DREAM+VLA链路尚未宣称跑通。

## 2. 已拍板的接口地址

| 服务 | 地址 |
|---|---|
| Agent网页 | `http://192.168.0.108:8888` |
| Agent Master | `http://192.168.0.108:5000` |
| Agent DREAM适配器 | `http://192.168.0.108:5006` |
| DREAM HTTP | `http://192.168.0.185:8001` |
| VLA HTTP正式地址 | `http://192.168.0.194:8091` |
| NX VLA相机流 | `tcp://192.168.0.240:5555` |
| 导航/VLA动作出口 | `192.168.0.194:5556` |

VLA正式端口确定为`8091`。Agent配置已经写入该地址，但真机接待总开关仍保持关闭，直到双方共同确认VLA已就绪。

## 3. 最终固定Pipeline

```text
网页自然语言“开始接待”
→ Master创建一个task_id
→ 读取并核对DREAM世界/关系图合同
→ DREAM leg1导航table2并返回真实成功终态
→ Agent显式POST VLA pick
→ Agent轮询VLA pick终态
→ 确认VLA停止策略并归还导航通路
→ DREAM leg2导航relay2
→ DREAM leg3横移relay3
→ DREAM leg4导航table1
→ Agent显式POST VLA place
→ VLA进入waiting_operator_approval
→ 现场人员按原流程Enter批准
→ VLA执行放置、停止策略并归还导航通路
→ Agent轮询VLA place终态
→ Agent写入SUCCEEDED
```

任一动作失败、取消、超时或状态不确定，后续步骤立即停止。

## 4. 首轮关闭照片判真

首轮三方动作联调确定使用：

```yaml
photo_verification_enabled: false
```

因此Agent首轮不会调用：

```http
GET  /v1/camera/status
POST /v1/camera/snapshots
GET  /v1/camera/snapshots/{snapshot_id}/rgb
```

也不会调用VLM或LLM做动作后图片判断。任务状态会明确记录：

```json
{
  "photo_verification_enabled": false,
  "evidence_level": "vla_only"
}
```

这只是暂时关闭Agent的额外图片复核，不允许VLA使用mock或动作进程退出码伪造业务成功。

## 5. Agent提交VLA任务

### 5.1 pick请求

```json
{
  "contract_version": "fq/reception-lan/v1",
  "command_id": "vla-pick-<task-suffix>",
  "task_id": "reception-<id>",
  "operation": "pick",
  "object_id": "cola_can_1",
  "target_area": "table_2",
  "navigation_proof": {
    "dream_command_id": "nav-table2-<task-suffix>",
    "target_id": "table_2",
    "state": "succeeded"
  }
}
```

### 5.2 place请求

```json
{
  "contract_version": "fq/reception-lan/v1",
  "command_id": "vla-place-<task-suffix>",
  "task_id": "reception-<id>",
  "operation": "place",
  "object_id": "cola_can_1",
  "target_area": "table_1",
  "navigation_proof": {
    "dream_command_id": "nav-table1-<task-suffix>",
    "target_id": "table_1",
    "state": "succeeded"
  }
}
```

Agent在POST前会原子保存task_id、command_id和完整请求体。相同command_id不得重复执行动作。

## 6. 首轮VLA成功硬门

### 6.1 pick必须同时满足

```text
VLA state == succeeded
result.success == true
result.object_grasped == true
result.holding == cola_can_1
result.policy_stopped == true
result.navigation_port_ready == true
completed_at存在且包含时区
```

Agent通过后才写：

```json
{
  "verified_state": "GRASP_CONFIRMED_BY_VLA",
  "holding": "cola_can_1",
  "object_location": "in_gripper",
  "evidence_level": "vla_only"
}
```

### 6.2 place必须同时满足

```text
VLA state == succeeded
result.success == true
result.object_grasped == false
result.holding == null
result.released == true
result.object_at_target == true
result.policy_stopped == true
result.navigation_port_ready == true
completed_at存在且包含时区
```

因为首轮照片判真关闭，`object_at_target=true`是必需字段。不能只根据张手或holding=null判定放置完成。

Agent通过后才写：

```json
{
  "state": "SUCCEEDED",
  "verified_state": "PLACE_CONFIRMED_BY_VLA",
  "holding": null,
  "object_location": "table_1",
  "evidence_level": "vla_only"
}
```

## 7. place保留现场Enter批准

双方确认不由HTTP桥模拟或注入Enter。VLA状态机应表现为：

```text
accepted
→ verifying_navigation
→ acquiring_action_port
→ waiting_operator_approval
→ 现场人员检查并按Enter
→ running
→ evaluating_action_result
→ stopping_policy
→ restoring_navigation
→ succeeded / failed / cancelled
```

Agent把`waiting_operator_approval`视为运行中状态，不判失败，也不发送后续命令。place等待上限暂定30分钟。

如现场拒绝或取消，VLA必须停止动作发布、归还通路并返回明确终态。

## 8. 动作端口采用双重确认

VLA终态必须提供：

```text
result.policy_stopped == true
result.navigation_port_ready == true
```

Agent在提交下一段DREAM导航前还会读取：

```http
GET http://192.168.0.185:8001/v1/status
```

并要求：

```text
navigation_transport_ready == true
```

只有VLA和DREAM两侧同时确认，Agent才继续导航。

VLA的`navigation_port_ready`必须由真实hook/端口交接逻辑确认，不能根据线程结束、子进程退出或当前没有活动任务推断。

## 9. DREAM真实权威字段

VLA回执中的以下路径只是候选，与DREAM实际响应不一致：

```text
navigation_active
navigation.state
active_command.state
```

真实DREAM命令成功响应已经在真机联调中确认，权威字段为：

```text
GET /v1/commands/{dream_command_id}

state == succeeded
target_id == navigation_proof.target_id
result.success == true
result.reached == true
result.navigation_stopped == true
```

实际成功样例核心字段：

```json
{
  "contract_version": "fq/reception-lan/v1",
  "kind": "navigation",
  "command_id": "fq-nav-15830078fd714a6d",
  "task_id": "nl-nav-table2-20260827-155148",
  "target_id": "table_2",
  "route_phase": "",
  "leg_index": 1,
  "state": "succeeded",
  "result": {
    "success": true,
    "reached": true,
    "navigation_stopped": true,
    "navigation_task_state": "reached"
  }
}
```

DREAM空闲状态的权威根级字段为：

```json
{
  "active_command_id": "",
  "active_command_state": null,
  "navigation_transport_ready": true
}
```

VLA验证导航凭证时应同时查询命令和总状态。若字段缺失或不一致，返回`NAVIGATION_STATUS_UNVERIFIABLE`，不得启动动作。

## 10. 相机决策（后续启用）

本轮固定使用左臂抓取。照片判真重新开启时使用：

```text
图像键：left_wrist
图像来源：NX唯一VLA相机服务 192.168.0.240:5555
HTTP桥：只读订阅，不直接打开USB RealSense
```

抓取图片只需判断：

```json
{
  "target_visible": true,
  "target_in_gripper": true,
  "confidence": 0.0,
  "reason": "可乐确实位于左侧灵巧手中"
}
```

VLA动作层仍可使用闭合、负载、抬升和视觉跟随作为自身成功证据。

## 11. 幂等、错误和恢复要求

VLA端已确认：

- 相同command_id和相同payload返回原事务；
- 相同command_id和不同payload返回409 `COMMAND_ID_CONFLICT`；
- 第二个活动任务返回409 `VLA_BUSY`；
- 重启发现非终态任务进入`RECOVERY_REQUIRED`。

Agent侧处理原则：

| 情况 | Agent行为 |
|---|---|
| HTTP 409 | 停止Pipeline，不换command_id重发 |
| HTTP 503且明确未启动动作 | 标记失败，等待VLA就绪后由现场重新开始 |
| POST断线或HTTP 504，是否已启动不确定 | 查询原command_id，不创建新ID |
| 连续404或始终无法确认 | 进入`RECOVERY_REQUIRED`，人工处理 |
| failed/cancelled | 停止后续导航 |

请VLA确保错误响应中的`error.code/retryable/details`能够区分“明确未启动”和“动作状态不确定”。

## 12. 首次联合验证顺序

### A. 零动作接口

```text
GET /health
GET /v1/vla/control/status
重复command_id幂等检查
DREAM凭证解析检查
```

### B. table2 + pick

```text
自然语言任务
→ DREAM真实导航table2
→ Agent POST pick
→ VLA真实pick终态
→ 端口双重确认
→ 暂停，不进入relay2
```

该阶段照片判真关闭。

### C. 单独place

```text
提交place
→ waiting_operator_approval
→ 现场按Enter
→ 放置终态
→ 端口双重确认
```

### D. 完整单罐链路

仅在B、C均有真实证据后运行：

```text
table2 → pick → relay2 → relay3 → table1 → place → SUCCEEDED
```

## 13. VLA组上线前确认清单

- [ ] `192.168.0.194:8091`开始监听且Agent主机可访问；
- [ ] `GET /health`返回协议版本；
- [ ] `/v1/vla/control/status.service_ready=true`；
- [ ] pick真实hook已经配置；
- [ ] pick返回第6.1节全部字段；
- [ ] place进入`waiting_operator_approval`并保留人工Enter；
- [ ] place返回第6.2节全部字段；
- [ ] DREAM解析改用第9节实际字段；
- [ ] `.194:5556`真实交接结果能生成`navigation_port_ready`；
- [ ] 失败和取消也能停止策略并归还动作通路；
- [ ] 不监听`/nav_done`自动开始；
- [ ] 首轮不要求启动快照接口和照片判真；
- [ ] 后续照片启用时使用`left_wrist`。

VLA完成以上配置后，请先通知Agent侧。双方现场共同确认后，Agent再把：

```yaml
reception_real.enabled: true
```

并重启Master开始三方真机联调。在此之前总开关继续保持`false`。
