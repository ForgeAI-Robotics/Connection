# G1 DREAM、VLA、Agent 统一真机联调设计方案

初版日期：2026-08-27  
当前修订：2026-08-28  
契约版本：`fq/reception-lan/v1`  
范围：单罐可乐接待任务；不自动重试、不执行多罐循环、不修改原VLA训练/动作代码。

## 1. 联调目标

最终只运行一条由Agent统一编排的真实链路：

```text
网页自然语言“开始接待”
→ Master创建task_id并读取DREAM世界/关系图
→ DREAM leg1导航table2
→ DREAM执行table2细检并持有相机
→ DREAM/VLA既有相机Pipeline完成控制权切换
→ Agent显式调用VLA pick
→ Agent轮询VLA真实终态及动作通路归还
→ 可选：左腕图片验证可乐确实位于灵巧手中
→ DREAM leg2导航relay2
→ DREAM leg3横移relay3
→ DREAM leg4导航table1
→ Agent显式调用VLA place
→ VLA进入waiting_operator_approval
→ 现场人员保留原流程，真实按Enter批准放置
→ Agent轮询VLA真实释放终态及动作通路归还
→ 可选：非腕部场景图片验证可乐位于table1
→ Agent写入SUCCEEDED
```

任一阶段失败、取消、超时或状态不确定，后续动作立即停止。

## 2. 三方唯一职责

### Agent/Master

- 唯一任务级编排者；
- 维护整轮唯一`task_id`和每步唯一`command_id`；
- 按固定顺序调用DREAM和VLA；
- 轮询远端真实终态；
- 维护`runtime_phase`和已确认业务状态；
- 进程或网络状态不确定时禁止盲目补发动作。

### DREAM

- 提供世界、关系图、固定审核导航合同和真实导航状态；
- 执行table2、relay2、relay3、table1四段导航；
- 保留9882、定位、Gateway、Token、碰撞和动作安全链；
- 不调用VLA、不发布`/nav_done`自动触发VLA、不自动续航下一段；
- table2细检阶段持有相机；细检完成后通过DREAM/VLA既有相机Pipeline释放并切给VLA；
- Agent不直接启动、停止或切换相机驱动。

### VLA

- 只执行Agent显式提交的pick/place；
- 不自行决定下一段导航；
- 不监听旧`/nav_done`；
- 真实动作完成后停止策略并归还动作通路；
- 不使用mock、进程退出码、单纯闭手或Token播放结束伪造成功。

## 3. 网络地址

| 服务 | 地址 |
|---|---|
| Agent网页 | `http://192.168.0.108:8888` |
| Agent Master | `http://192.168.0.108:5000` |
| Agent DREAM适配器 | `http://192.168.0.108:5006` |
| DREAM HTTP | `http://192.168.0.185:8001` |
| VLA Brain Bridge | `http://192.168.0.194:8091` |
| NX VLA相机流 | `tcp://192.168.0.240:5555` |
| 导航/VLA动作出口 | `192.168.0.194:5556` |

## 4. 四段导航合同

坐标系固定`map`，yaw单位弧度，`require_final_orientation=true`。

| leg | target_id | route_phase | goal_xyt | motion_mode |
|---:|---|---|---|---|
| 1 | `table_2` | `""` | `[0.9948137550501258, 1.402057782965935, -0.3193204258080712]` | `forward_path` |
| 2 | `door_1` | `door_approach` | `[3.733075988421528, 6.215369909530748, 2.718279944258407]` | `forward_path` |
| 3 | `door_1` | `door_lateral_exit` | `[4.185939449618811, 7.560143924693016, 2.7689146673931306]` | `lateral_path_aligned` |
| 4 | `table_1` | `table1_approach` | `[3.0873798986272165, 8.279995338440145, 1.175238157458919]` | `forward_path` |

Agent下发这些指令；DREAM加载审核副本只用于安全校验，不代表DREAM拥有任务编排权。

Agent启动任务时核对关系图中的`contract_version/frame_id/agent_navigation_contract`和
`nodes.door_1.evidence.navigation_contract`。任何字段缺失或坐标、顺序、模式不一致时，
停止在`FETCHING_WORLD`，不发送导航。

导航成功必须同时满足：

```text
state == succeeded
result.success == true
result.reached == true
result.navigation_stopped == true
```

## 5. VLA HTTP合同

正式接口：

```http
GET  /health
GET  /v1/vla/control/status
POST /v1/vla/tasks
GET  /v1/vla/tasks/{command_id}
POST /v1/vla/tasks/{command_id}/cancel
GET  /v1/camera/status
POST /v1/camera/snapshots
GET  /v1/camera/snapshots/{snapshot_id}/rgb
```

pick请求：

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

place请求同结构，`operation=place`、`target_area=table_1`，凭证引用table1导航命令。

VLA在动作开始前必须查询DREAM真实凭证：

```text
command_id匹配
target_id匹配
state=succeeded
result.success=true
result.reached=true
result.navigation_stopped=true

/v1/status:
active_command_id=""
active_command_state=null
navigation_transport_ready=true
```

## 6. VLA成功硬门

当前首轮使用：

```yaml
vla_result_policy: hand_state_only
```

### pick

```text
返回contract_version/task_id/command_id/operation必须匹配当前事务
state == succeeded
result.object_id == cola_can_1
result.success == true
result.object_grasped == true
result.holding == cola_can_1
result.policy_stopped == true
result.navigation_port_ready == true
result.evidence_level == hand_state_only
result.evidence.hand == left
result.evidence.hand_closed_confirmed == true
result.evidence.close_score >= 0.70
result.evidence.confirmed_frames >= 3
completed_at存在且包含时区
```

### place

```text
返回contract_version/task_id/command_id/operation必须匹配当前事务
state == succeeded
result.object_id == cola_can_1
result.success == true
result.object_grasped == false
result.holding == null
result.released == true
result.object_at_target == null或true
result.policy_stopped == true
result.navigation_port_ready == true
result.evidence_level == hand_state_only
result.evidence.hand == left
result.evidence.hand_open_confirmed == true
result.evidence.open_score <= 0.30
result.evidence.confirmed_frames >= 3
completed_at存在且包含时区
```

hand-only模式明确不证明物体存在或最终落桌。完整链路跑完后写
`COMPLETED_HAND_STATE_ONLY`，不能冒充严格业务成功`SUCCEEDED`。切换到
`strict_object_evidence`或开启放置照片并通过后，才允许写`SUCCEEDED`。

## 7. 动作端口双重确认

VLA终态必须返回：

```text
policy_stopped=true
navigation_port_ready=true
```

Agent提交下一段导航前再次读取DREAM：

```text
navigation_transport_ready=true
```

两侧同时确认才继续。VLA的`navigation_port_ready`必须来自真实hook/5556交接，不能根据
线程退出、子进程结束或无活动任务推断。

## 8. place保留原Enter流程

```text
Agent POST place
→ VLA验证DREAM凭证
→ VLA进入waiting_operator_approval
→ 现场人员检查实体保护、机器人、可乐和路径
→ 在VLA Bridge tmux终端真实按Enter
→ VLA执行原放置动作
→ VLA验证释放、落桌、策略停止和端口归还
→ 返回终态
```

Agent把`waiting_operator_approval`视为运行中，等待上限30分钟。Agent和Bridge均不得模拟
或注入Enter。取消或超时必须在未启动动作时安全结束，或在已启动时先停止策略、归还端口。

## 9. 两个独立照片判真开关

相机所有权顺序固定为：

```text
初始与table2细检：camera_owner=dream
→ DREAM细检终态succeeded
→ DREAM停止自己的相机占用
→ DREAM/VLA既有相机Pipeline完成切换
→ camera_owner=vla
→ Agent才允许POST VLA pick
```

Agent只负责提交DREAM细检、轮询终态并观察`driver_enabled=false/external_owner=vla`；
不通过Agent或hook直接控制相机启停。VLA pick hook也不启动NX相机服务，只消费双方既有
相机Pipeline交接后的图像流。

当前默认：

```yaml
grasp_photo_verification_enabled: false
place_photo_verification_enabled: false
grasp_camera_view: left_wrist
place_camera_view: ego_view
```

### 抓取照片判真

开启后使用左腕`left_wrist`，只判断：

```json
{
  "target_visible": true,
  "target_in_gripper": true,
  "confidence": 0.0,
  "reason": "可乐确实位于左侧灵巧手中"
}
```

### 放置照片判真

开启后使用非腕部场景相机`ego_view`，判断可乐位于table1且不在灵巧手中。

任一开关为false时，Agent不请求该阶段快照、不调用VLM/LLM，并记录`evidence_level=vla_only`
或相应单阶段图片证据等级。开关关闭不降低VLA动作层自身的真实证据硬门。

NX相机服务是唯一USB RealSense硬件所有者；Brain Bridge仅订阅`.240:5555`，不会启动
第二个驱动。

## 10. 幂等、断线和恢复

每个真实POST前，Agent先原子持久化task_id、command_id和完整payload。

```text
相同command_id + 相同payload → 返回原事务，不重复动作
相同command_id + 不同payload → HTTP 409 COMMAND_ID_CONFLICT
已有活动任务 → HTTP 409 VLA_BUSY
```

断线处理：

```text
POST超时、连接断开或HTTP 504
→ 不生成新command_id
→ 查询原command_id
→ 立即、1秒、2秒、5秒有界重查
→ 查到事务则继续轮询
→ 始终无法确认则RECOVERY_REQUIRED
```

明确400/409直接失败；503且明确`action_started=false/action_state_uncertain=false`时按未启动失败；
连续404或无法判断真实动作状态时必须人工恢复。

## 11. 本地持久化，不使用数据库

```text
master/sop/runtime/reception/
  current_task.json
  events.jsonl
  verification.jsonl
  images/
```

状态快照使用临时文件、flush/fsync和原子替换。Master重启发现非终态任务时进入
`RECOVERY_REQUIRED`，不自动续跑或补发真实动作。

## 12. 不修改原VLA代码的hook实现

- 不修改GR00T模型、训练代码、SONIC、LinkerHand或现有动作脚本；
- 不修改或操作`g1_navigation_vla_bridge`；
- 只允许在`g1_brain_vla_bridge`内新增可审计hook包装器；
- hook可以调用既有动作入口，但必须取得真实动作结果和5556归还证据；
- 既有动作若不输出足够证据，包装器必须失败关闭，不能补写成功字段；
- 原place中的Enter必须继续由现场人员真实执行。

Brain Bridge内已经新增：

```text
hand_state_hook_common.py
pick_action_hook.py
place_action_hook.py
```

pick hook包装原GR00T RTC栈：启动原PolicyServer/VLA client、通过原5580键盘通道启动策略、
监听5556中的`left_hand_joints`、要求先稳定OPEN后稳定CLOSED、暂停并停止原动作发布、确认
5556释放后输出JSON。

place hook调用原`run_place_coke_replay_test.sh start`，保留原脚本`/dev/tty` Enter，要求
先稳定CLOSED后稳定OPEN、等待原脚本完成、确认5556释放后输出JSON。

两个hook都要求：

```text
BRAIN_VLA_REAL_ACTION_ACK=PHYSICAL_ESTOP_READY
```

并支持`--check`只读检查。当前正式配置仍为：

```json
"real_action_enabled": false
```

所以hook代码已挂接，但不会因HTTP请求启动真实动作。现场Go/No-Go后才将该值改为true并
使用上述环境确认重新启动Brain Bridge。

## 13. 当前实现状态（2026-08-28）

### 已完成

- 自然语言→Master拆解→Slaver→DREAM真实导航table2已两次成功；
- Agent DREAM/VLA HTTP客户端、状态机、持久化和严格恢复已实现；
- Agent VLA终态身份及动作结果一致性检查已实现；
- VLA Brain Bridge地址`.194:8091`已启动并可从Agent主机访问；
- Brain Bridge使用DREAM真实权威字段；
- Brain Bridge保留place Enter；
- Brain Bridge支持抓取/放置两个独立照片开关和`left_wrist/ego_view`；
- Agent本地相关回归10项通过；
- VLA主机Brain Bridge原生测试22项通过；
- bridge-local pick/place hand-only hook已实现并通过`--check`；
- SSH密钥别名`vla194`已配置，保活已启用。

### 尚未完成

- DREAM `.185:8001`当前需要重新启动后才能继续三方联调；
- hand-only pick hook尚未进行吊保条件下真实动作验证；
- hand-only place hook尚未进行原Enter、张手和回stand真机验证；
- 5556真实取得、停止和归还逻辑已实现但尚未进行现场验证；
- 完整Agent+DREAM+VLA真机链路尚未执行。

当前必须保持：

```yaml
reception_real.enabled: false
VLA backend.real_action_enabled: false
```

## 14. 联调顺序

### A. 零动作HTTP

```text
DREAM启动8001
→ GET VLA /health
→ GET VLA /v1/vla/control/status
→ 验证DREAM凭证解析、409、503、404和幂等
```

### B. table2 + pick

```text
自然语言任务
→ DREAM真实导航table2
→ Agent POST pick
→ VLA真实执行并返回第6节pick硬门
→ 双重确认动作通路
→ 停止，不进入relay2
```

### C. 单独place

```text
Agent POST place
→ waiting_operator_approval
→ 现场真实按Enter
→ VLA返回第6节place硬门
→ 双重确认动作通路
```

### D. 完整单罐链路

仅在B、C都有可复查真机证据后，将总开关改为true并重启Master，完整运行第1节Pipeline。

## 15. 开启完整真机前的Go/No-Go

- [ ] DREAM 8001在线且关系图合同一致；
- [ ] VLA 8091 `service_ready=true`；
- [ ] pick hook返回全部真实成功字段；
- [ ] place hook保留Enter并返回全部真实成功字段；
- [ ] 失败/取消也能停止策略并归还5556；
- [ ] Agent与VLA相同command_id幂等测试通过；
- [ ] 现场9882、实体急停、扶持和观察人员就位；
- [ ] 首轮不启用自动业务重试或多罐循环。
- [ ] 现场确认后设置`BRAIN_VLA_REAL_ACTION_ACK=PHYSICAL_ESTOP_READY`；
- [ ] 分段测试通过前不同时开启Agent与VLA两个真实总开关；

任一项未通过，只执行对应分段测试，不宣称完整Pipeline已接通。
