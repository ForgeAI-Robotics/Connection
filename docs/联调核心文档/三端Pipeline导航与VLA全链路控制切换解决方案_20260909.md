# 三端Pipeline：导航与VLA全链路控制切换解决方案

版本：v1.2｜日期：2026-09-09｜状态：待评审、未实施。

本文从已发现的“抓取后5556归还但NX仍保持”问题出发，统一处理导航→抓取→导航多段→放置→安全待机的双向控制切换。按三个责任端交付：**大脑端、VLA端（包含relay、手桥及NX协同修改）、导航端**。NX是实际执行侧，不能因为按三端分工就省略它的改动。

本文独立于Pipeline断点恢复优化设计；不引入完整任务数据库、通用重规划、跨重启业务续跑或自动重抓。以下新增接口、状态和函数均为设计，不代表当前已存在。

**全链路入口：优先阅读第12节。**第1节保留历史故障依据，第3节是pick→导航的详细示例；第4–7节的文件及接口由第12节扩展为双向切换。放置后的任务结束采用SAFE_IDLE，不强制CONTROL_CONFIRMED；示例中的导航接管要求仅适用于下一步确有导航动作时。

## 阅读顺序

- 项目负责人：先读第12节全链路方案，再读第1、2、8节。
- 大脑开发：第4节及第12.6节B07–B10。
- VLA及NX开发：第5节及第12.3–12.6节。
- 导航开发：第6节及第12.1、12.6节。
- 三方对齐接口：第7节示例及第12.5节通用合同。
- 联调验收：第9、10节及第12.8–12.10节，共32项。

## 1. 结论：修复的是控制接管缺口

### 1.1 已经看到的事实

历史任务：`c9b696151e604551af82e55c3c868f56`。

| 环节 | 证据 |
| --- | --- |
| table2 | 导航到达成功；最终位置误差0.082m、朝向残差7.52°，朝向调整达到15秒限时 |
| VLA抓取 | 大脑与VLA均记录抓取成功，采用hand_state_only，照片验证关闭 |
| VLA收尾 | 当次键盘日志末尾发送k；历史hook备份也存在该步骤，运行中的k对应STOP |
| 5556归还 | relay记录重新绑定与navigation_ownership_restored |
| relay2 | 18:36:00通过Gateway并进入转向；18:37:01触发18秒无进展保护，最终朝向误差约102.44° |
| NX | 日志出现STOP→safe hold锁定；此后仍收大量Token，较后才出现显式START解除 |
| 当前代码 | relay归还只恢复转发；导航Token控制器只在启动阶段发START；ROS Arm不解除NX的保持锁 |

**已确认的是协议缺口；它是这轮历史故障的高可信原因。**NX日志缺逐行时间与任务身份，当前源码不完全等于当时二进制，因此尚不宣称唯一根因或已现场复现。

### 1.2 为什么原检查不够

当前流程把以下事实混在一起：

```text
VLA策略已停止 ≠ 5556出口已归还 ≠ NX已采用导航输入 ≠ 导航本段已成功
```

不能仅在VLA结果里写`navigation_port_ready=true`，或只查TCP ESTAB，就开始relay2。也不能在导航侧循环补START：较新的急停/取消可能被错误解除。

### 1.3 本轮完成目标

每次跨控制源切换都确认原发布者停止、手部状态明确、目标输入正确以及NX受控接管；同一导航源的段间转换只核验导航条件与动作模式。任何条件未知时停在交接阶段，并显示具体原因；不重复pick。

保留STOP、安全保持、原定位审批和导航保护。首次修改不增加18秒无进展超时，不改导航目标、模型或转向片段，不重放冷启动PLANNER→POSE/初始姿态序列。

## 2. 三端分工：谁决策、谁发控制、谁确认

| 责任端 | 必须完成的工作 | 不能代替其他端做什么 |
| --- | --- | --- |
| 大脑 | 保存抓取结果，显示交接进度，跟踪同一handoff，取得有效接管凭证后下发relay2 | 不直接发NX裸START，不靠新建“开始接待”修复交接 |
| VLA（含relay、NX） | 停止动作发布者；唯一交接协调；保持手部目标；请求NX受控接管；提供真实状态回执 | 不代替导航批准定位，不以服务活着/端口监听推断接管成功 |
| 导航 | 保持本地运动门关闭并准备中性输入；取得接管凭证后再Arm/Execute；最后一层拦截无凭证动作 | 不在ROS解除急停回调中无条件发送START，不启动自己的审核队列抢占大脑任务 |

**唯一控制接管负责人：VLA端的handoff协调模块，双向负责NAVIGATION↔VLA以及切换到SAFE_IDLE。**建议新增`g1_brain_vla_bridge/handoff_coordinator.py`，由Bridge受管启动；relay只处理本地出口和帧转发，NX负责最终执行校验。其他hook、adapter、大脑和导航均不另起一套“补发START”流程。

这里的“恢复控制”会影响实体机器人。第一版须有明确现场授权与核验结果，不能由LLM决定。来源不明的保持、物理急停、姿态保护不能按普通交接自动解除。

## 3. 跨源切换示例：pick完成后交给导航

| 步骤 | 谁做 | 动作 | 通过条件 |
| --- | --- | --- | --- |
| 1 | VLA hook/Bridge | 记录抓取效果，执行原STOP及策略停止，核验所有动作发布者 | 原会话与发布者确实退出；效果证据独立保存 |
| 2 | VLA协调器→导航 | 请求本次交接准备 | 无未核清goal；本地运动门关闭；当前任务/实例匹配 |
| 3 | 导航Token adapter | 清旧动作意图，过渡到已审核中性身体输入；提供prepare凭证 | 中性输入已生成，源代次、序列、有效期明确 |
| 4 | VLA协调器→relay | 按handoff取得5556，隔离旧缓冲，应用已确认手部策略 | 唯一出口；仅允许本轮中性输入 |
| 5 | relay→NX | 在NX仍保持时发送本轮准备输入 | NX确认收到匹配源代次和新鲜序列 |
| 6 | VLA协调器→NX | 提交带身份的RESUME_AFTER_HANDOFF | 本次现场授权有效、保持原因允许、无更高优先级保护 |
| 7 | NX→VLA协调器 | 返回实际接管状态 | 保持解除；本轮输入已采用；模式正确；反馈/身体状态稳定 |
| 8 | VLA Bridge→大脑 | 返回CONTROL_CONFIRMED及接管凭证 | command/task/handoff一致，无未解决收尾问题 |
| 9 | 大脑→导航 | 提交relay2，携带接管凭证 | 导航执行前再次验证凭证、当前位置与原安全门 |
| 10 | 导航/NX | 执行本段并观察实际转向 | 按原导航合同返回终态；不把接管成功当导航成功 |

### 3.1 解决等待依赖，避免再次卡住

- 抓取效果已产生时，先保存`action_effect`和`publisher_stop`；不能等NX接管成功才保存抓取结果。
- 大脑只跟踪，VLA协调器在pick worker内完成接管流程；不要求大脑先收到pick最终成功才能启动协调器。
- 导航的`prepare-handoff`必须允许在NX未接管、运动隔离状态下执行；它只准备中性输入，不能产生导航goal。
- VLA协调器不依赖DREAM旧`transport_status()`的“全部ready”作为准备入口，否则会形成互等。准备接口与运动准入接口分别实现。
- 服务间调用不持有数据库/全局互斥锁；收到响应后用revision复核再提交，避免互相查询死锁。

### 3.2 交接阶段状态

```text
ACTION_EFFECT_RECORDED
→ STOPPING_PUBLISHERS
→ WAITING_OPERATOR_CONFIRMATION（第一版）
→ PREPARING_NAVIGATION_INPUT
→ OUTPUT_PREPARED
→ NX_RESUME_PENDING
→ CONTROL_CONFIRMED

任一阶段状态不明 → HANDOFF_RECOVERY_REQUIRED
收到取消 → CANCELLING → 确认安全收尾/保留隔离
```

上述是交接子状态，不要求重写整个Pipeline状态机。失败时保留原pick command与效果；仅修复原handoff不执行新的pick/place。历史失败任务是否能继续业务步骤属于独立续跑能力，本专项不通过手改步骤索引实现。

## 4. 大脑端改动清单

本地项目根：`C:/Program Files (x86)/brain`。

| 编号 | 文件/位置 | 当前行为 | 本次改动 |
| --- | --- | --- | --- |
| B01 | `master/sop/reception_real.py::_vla_action()` | 主要等待VLA最终成功 | 接收并展示交接子状态；保存已产生的抓取效果；交接失败进入明确待处理，不提示重新抓取 |
| B02 | `master/integrations/vla_client.py::wait_task()`、`require_pick_success()` | 检查success、policy_stopped、navigation_port_ready | 新模式要求身份一致的CONTROL_CONFIRMED；旧navigation_port_ready仅为传输证据；等待函数识别新交接状态并有期限 |
| B03 | `master/sop/reception_real.py::_navigate()` | 等待navigation_transport_ready后提交 | relay2前要求有效接管凭证；把handoff_id、owner_epoch、NX实例与凭证版本放入请求 |
| B04 | `master/integrations/dream_client.py` | 查询传输ready | 增加接管状态/导航准入查询；缺字段或未知返回明确blocker，不降级为端口检查 |
| B05 | `master/sop/reception_store.py` | 保存任务状态与commands | 在现有记录中追加handoff及effect/stop/control分层证据，避免失败覆盖成功效果；不为本修复引入通用SQLite任务框架 |
| B06 | `master/run.py`、`deploy/run.py`、接待页面 | 以大阶段展示进度 | 增加“抓取已完成，等待NX接管”、具体阻塞原因、只读检查入口；交接操作只作用于原handoff |

拟议函数职责：`wait_handoff_status(command_id)`只查；`require_control_confirmed(report)`校验；`repair_handoff(command_id, operation_id, check_id)`只处理原交接。不允许网页把repair映射到publish_task。

大脑不得仅依赖数秒前检查结果：导航端仍须执行最后校验。网络查询失败不产生新导航或新抓取命令；已下发的relay2仍按原ID跟踪，禁止因接管查询异常直接重投。

**大脑交付结果：**用户能知道停在“NX交接”而非“抓取失败”；接管未确认时relay2调用次数必须为0。

## 5. VLA端改动清单（含relay、手桥与NX）

VLA项目根：`/home/lgj_4090/文档/sonic_wbc`。NX相关改动由VLA负责协同控制器维护人完成，不让大脑远程直接操纵控制器。

### 5.1 抓取/放置收尾

| 编号 | 文件/函数 | 本次改动 |
| --- | --- | --- |
| V01 | `g1_brain_vla_bridge/pick_action_hook.py` | 保留受控STOP语义；开手/闭手等待可取消；每次按键前核对取消；效果、停止、交接分别报告 |
| V02 | `hand_state_hook_common.py::HandStateMonitor.wait()`、`terminate_group()` | 等待传入取消事件；停止函数核对进程组/会话身份并等待实际退出；不能固定返回policy_stopped=true |
| V03 | `bridge_service.py::CommandBackend.execute()` | 持续消费stdout/stderr，限缓存；取消/超时也保留最后效果和收尾报告 |
| V04 | `bridge_service.py::_worker()`、`_persist()`、`control_status()` | 保存handoff与unresolved；失败后active清空不等于可用；增加action_effect/publisher_stop/control_status独立字段 |
| V05 | `place_action_hook.py`、`navigation_place_coke_adapter.py` | 归还责任收敛到唯一协调器；不再由adapter正常归还、外层异常再按task_stopped重复归还 |
| V06 | `bridge_service.py::validate_result()` | 将open_score缺值判断与0.0分开；不以hand_state_only证明物体实际存在 |

当前hand_state_only读取的是5556目标关节，不是灵巧手实测。第一版交接确认使用新鲜物体观测或经身份核验的现场持有确认，记录来源和时间；禁止将旧holding缓存自动当成现场事实。

### 5.2 新增唯一交接协调器

建议模块：`g1_brain_vla_bridge/handoff_coordinator.py`。复用Bridge的稳定runtime，原子保存事务；单写者、revision及实例锁必须落实。只保存本专项的handoff，不扩展为全任务调度器。

| 函数职责（拟议） | 输入/行为 |
| --- | --- |
| `begin_handoff()` | 绑定原task/command、VLA执行会话、hand_policy，先持久化后开始操作 |
| `prepare_navigation_input()` | 向DREAM申请中性输入准备凭证，不调用导航Execute |
| `prepare_output()` | 通过版本化内部请求要求relay绑定与清缓冲，确认唯一所有者 |
| `request_nx_resume()` | 持久化operation及不可变payload，提交本次受控接管 |
| `poll_nx_confirmation()` | 同operation查询，验证实际采用与保持状态 |
| `repair_original_handoff()` | 从现存阶段核查/继续同一事务；不重跑pick、不覆盖效果 |
| `cancel_handoff()` | 作废本轮放行权限，阻止新goal；按NX状态确认保护与隔离，不自动张手 |

桥接服务重启后先核查未解决事务，不能重新启动原动作worker。协调器崩溃也不能让relay默认恢复任意上游转发。

### 5.3 relay与手桥

文件：`g1_navigation_vla_bridge/sequential_navigation_action_relay.py`及`ros_handoff_acquire.py`、`ros_handoff_signal.py`。

- Bool改为带handoff/operation/instance/epoch的版本化请求与应答，重复同请求返回原结果。
- 分离`OUTPUT_PREPARED`与`CONTROL_CONFIRMED`；第一次收到上游帧不再宣告运动接管成功。
- 取得出口后清空旧队列，在准备阶段只允许本轮中性身体输入；不能以“cmd_vel=0”替代实际Token核验。
- relay只转发协调器已授权的NX控制消息，不自己定时补START；导航原启动/维护消息必须与本次受管交接模式隔离。
- 运行帧携带源代次与序列，NX按实际帧核验；控制消息中的epoch不能代替数据帧身份。
- `hand_policy`显式为持有保持/释放后保持/待人工核定。失败或取消不能统一设carry_hold_closed=true。
- 当前手桥独立订阅5556；版本变更必须同步解析。NX身体保护不保证手部不会动作，准备流必须保留已核定手目标。
- 持久化owner、hand_policy和未解决事务；重启从RECONCILING开始，不默认navigation/carry=false。

### 5.4 NX必要改动

已核查主文件：`/home/dev/sonic/gear_sonic_deploy/src/g1/g1_deploy_onnx_ref/src/g1_deploy_onnx_ref.cpp`；命令解析：同工程`include/input_interface/zmq_manager.hpp`。输出编码位置沿当前publish调用确认。

| 编号 | 改动 | 必须满足 |
| --- | --- | --- |
| N01 | 暴露safe_hold、hold_reason、hold_revision、controller_instance_id、controller_mode | 5557有反馈不能替代这些字段；未知不放行 |
| N02 | 提供`RESUME_AFTER_HANDOFF`受控命令及可查询结果 | 检查本次权限、保持原因、实例、源代次、中性输入凭证后再解除 |
| N03 | 原子处理STOP/RESUME竞争 | 新STOP提高hold_revision，旧恢复凭证失效；全局急停保持独立优先权 |
| N04 | 分离已接收与已采用序列 | 接管回执在控制路径真正采用本轮输入后成立；不是网络接收成功即成立 |
| N05 | 保持中立姿态与兼容模式 | 不重启SONIC、不重放PLANNER→POSE启动，不重新初始化姿态；采用控制器审核的过渡 |
| N06 | 隔离旧裸START | 受管模式迟到legacy START不能绕过新权限；保留启动功能时只允许独立维护模式 |

既有STOP保护应保留。安全保持解除后，输入新鲜度、反馈、姿态等原保护继续有效。准备时NX仍保持；只有受控操作接受后才能采用外部输入。若模式不匹配，返回待现场处理，不在这里自动切换。

**VLA交付结果：**“原动作停止、出口归还、NX接管”分别有证据；操作失败时抓取效果不丢，原handoff可查询，不能靠重启或裸START清问题。

## 6. 导航端改动清单

DREAM根：`/home/fq/BJHYZJ_FQ/DREAM`。

Token控制器真实位置：`/home/fq/BJHYZJ_FQ/navigation_handoff_packages/G1建图导航交接包_20260813/runtime/gear_sonic/scripts/sonic_token_navigation_controller.py`。修改此实际部署映射，不能只改VLA侧同名副本。

| 编号 | 文件/位置 | 本次改动 |
| --- | --- | --- |
| D01 | `tools/g1_agent_navigation_service.py` | 新增交接准备查询/操作；保存准备凭证与本地运动隔离；relay2提交要求有效接管证明 |
| D02 | `tools/g1_hybrid_localization_web_view.py::arm_navigation()/execute_shadow_goal()` | 操作绑定command及handoff代次；实际发goal前复核NX状态、定位和保护，防止检查后状态改变 |
| D03 | Token控制器主循环 | 提供可核验的中性输入阶段；清旧intent/primitive缓存；生成源代次/序列；记录实际intent和选中片段 |
| D04 | Token控制器ROS急停解除逻辑 | 仅解除导航本地运动门；不当作NX接管完成，不在回调里盲发START |
| D05 | 状态输出 | 分别报告transport_ready、nx_control_confirmed、can_dispatch_goal与blockers，禁止用单一ready混合 |
| D06 | `cancel_navigation()`及9882取消路径 | CANCELLING期间不释放活动占用；针对原命令的取消与新任务互斥，全局急停不受旧ID限制 |

准备接口应保留有效定位批准，不直接调用会清除批准和中断记录的普通`lock_navigation()`来代替准备。若现场条件本身失效，仍按既有规则撤销批准；准备不自动再批准。

`/recover-current-leg`属于导航自己的审核队列，不能作为大脑交接修复入口；受管大脑模式下两套编排互斥。

本专项无需导航实现完整跨重启历史任务续跑；导航重启后所有prepare凭证作废并阻挡旧接管，核查后重新准备。命令持久恢复可按独立优化计划推进，不应作为本次问题修复的全部前置工作。

**导航交付结果：**即使传输ready，NX仍保持时也不能下发relay2；接管成功后保持原导航规划及运动保护。

## 7. 三端先冻结的接口合同（pick出站示例；通用入口见12.5）

本节接口是建议名称，未部署；现有端口沿用，大脑不新增直接访问NX的控制入口。

### 7.1 VLA对外

| 接口（拟新增） | 功能 |
| --- | --- |
| `GET /v1/vla/tasks/{command_id}/handoff` | 返回分层证据、交接阶段、blocker与版本；只读 |
| `POST /v1/vla/tasks/{command_id}/handoff/check` | 只读核查并保存短期检查凭证，不发控制 |
| `POST /v1/vla/tasks/{command_id}/handoff/repair` | 在现场授权和版本检查后，修复原交接；不执行pick/place |

正常pick worker可以调用相同内部协调器，无需通过HTTP调用自己。repair请求包含`operation_id`、`expected_revision`、`check_id`；先返回原幂等结果，再判断版本。相同operation不同请求拒绝。

### 7.2 导航对外交接准备

| 接口（拟新增） | 功能 |
| --- | --- |
| `POST /v1/navigation/handoff/prepare` | 持久接受本轮中性输入准备，保持运动门关闭 |
| `GET /v1/navigation/handoff/{handoff_id}` | 查询准备状态、实例、输入源、期限和当前blocker |
| `POST /v1/navigation/handoff/{handoff_id}/cancel` | 作废本轮准备/放行权限；不解除保护 |

请求携带task、handoff、operation及expected navigation instance。不得用不带身份的全局“设ready”接口。

### 7.3 接管结果示例

```json
{
  "contract_version": "fq/navigation-handoff/v1",
  "task_id": "task-example",
  "source_command_id": "vla-pick-example",
  "handoff_id": "handoff-example",
  "revision": 9,
  "state": "CONTROL_CONFIRMED",
  "action_effect": {"holding": "cola_can_1", "evidence_source": "operator_confirmed"},
  "publisher_stop": {"confirmed": true, "session_id": "vla-session-example"},
  "output": {"bound": true, "relay_instance_id": "relay-example", "owner_epoch": 13},
  "nx": {
    "controller_instance_id": "nx-example",
    "safe_hold_latched": false,
    "hold_revision": 8,
    "mode": "STREAMED_MOTION",
    "active_source_epoch": 13,
    "last_received_sequence": 901,
    "last_applied_sequence": 900,
    "last_resume_operation_id": "resume-example",
    "feedback_fresh": true
  },
  "navigation_prepare_id": "prepare-example",
  "blockers": []
}
```

示例不是可直接使用的成功证明。真实凭证须绑定证据引用、确认者身份与时间、导航实例、地图/定位上下文和有效期；这些由权威服务产生并在下游核查，不相信客户端填true。NX输入年龄用接收端单调时钟，跨机墙钟仅用于审计。

### 7.4 幂等、保护与兼容规则

1. 操作先保存不可变请求再外发；回复丢失查询原operation，不换ID重发。
2. NX重启改变controller_instance_id，relay/导航重启改变各自实例；旧准备、旧回执不得放行。
3. 新STOP/取消使旧准备和恢复凭证失效。实际解除时原子比较hold_revision；初步检查不能代替执行端最后校验。
4. 空间上允许一个发布者；时间上只接受当前代次输入。frame epoch/sequence必须进入NX实际接收校验，不能只写在控制请求中。
5. 新字段缺失返回UNKNOWN；三方能力未匹配时关闭受控自动接管，只提供诊断与现场维护。
6. v1 Bool、旧START及新协议不得同时驱动接管状态。部署切换在无活动动作、现场确认的维护窗口进行。
7. 保持原因无法证明属于本次可恢复交接时，不自动解除；需要独立现场核查与权限校验。

## 8. 实施顺序：先看得见，再闭环

| 阶段 | 改动包 | 交付条件 |
| --- | --- | --- |
| A：冻结基线与合同 | 记录三端/NX实际加载版本、消息兼容方式、中性输入合同、确认权限 | 三方认可第2、3、7节；不猜测部署路径与时间阈值 |
| B：只读状态 | N01、各端分层状态与身份日志 | 界面能区分“收流但NX保持”，无新动作 |
| C：修收尾和接管 | V01–V06、协调器、relay、N02–N06、D01/D03 | 假服务故障测试通过；原pick效果保留；旧STOP/START不会错放行 |
| D：接导航准入和大脑 | B01–B06、D02/D04–D06 | CONTROL_CONFIRMED之前relay2提交次数0；之后仍过原安全门 |
| E：现场分段验证 | 中性接管→短段转向→抓取后relay2 | 同一handoff可关联所有证据；实际角速度与目标一致；无重抓 |
| F：整轮验证 | table2→pick→relay2→relay3→table1→place | 成功与取消/失败路径均符合合同 |

中性输入的具体Token、过渡时长、反馈新鲜阈值和稳定窗口，由控制器/导航负责人根据现有安全基线与采样确定，先填入合同再实现。不能为了给出“完整配置”随意发明物理参数。

不采用临时“去掉k”“循环START”“加长导航超时”作为上线修复。第一批可以独立发布只读诊断，但不能因此宣称接管已解决。

## 9. 抓取出站基础失败处置及验收

### 9.1 必须有的失败出口

| 状态 | 处理 |
| --- | --- |
| VLA发布者未停止 | 保持隔离，不绑定第二个动作源 |
| 抓取有效但交接失败 | 保存抓取效果，只修复原handoff |
| 无中性输入/手部证据矛盾 | 不恢复NX、不发goal；展示具体blocker |
| NX接管响应丢失 | 查原operation；DREAM门保持关闭，不重复START |
| NX已接管但上层崩溃 | 保持中性输入与禁止新goal，重启核查原事务；不默认ready |
| 新STOP/保护触发 | 作废旧恢复权限，保持安全收尾；不能重试旧解除 |
| 修复后仍无进展 | 原因分支转向Token/定位/姿态，受控停止并留证；不重复pick |

超时只结束本轮等待并转待处理，不自动释放未知资源。网络、停止、准备输入、NX确认各有预算；预算必须覆盖实际收尾测量值，不能用固定sleep代替确认。

### 9.2 专项测试清单

| 编号 | 场景 | 通过标准 |
| --- | --- | --- |
| H01 | STOP后5556恢复但NX仍保持 | 大脑与DREAM均拒绝relay2 |
| H02 | 正常受控接管 | NX确认实际采用后才下发；pick只执行一次 |
| H03 | 5557持续反馈但safe_hold=true | 不误报可运动 |
| H04 | RESUME回执丢失/重复请求 | 查询同operation，不重复改变控制状态 |
| H05 | 新STOP与旧RESUME竞争 | 新保护优先，旧请求拒绝 |
| H06 | NX/relay/导航任一重启 | 旧实例凭证失效，不自动运动 |
| H07 | 队列残留转向/行走帧 | 不进入中性准备，不在解除瞬间被采用 |
| H08 | hook退出但tmux发布者残留 | 停止未确认，不能接管 |
| H09 | 持有状态未知、放置后收尾失败 | 不自动握拳/松手，不重做动作 |
| H10 | NX确认后大脑/协调器退出 | 原记录可查询，无新goal，无重抓 |
| H11 | legacy START迟到 | 不能绕过当前hold revision和权限 |
| H12 | 独立手桥同步接管 | 准备流不意外改变手部目标 |
| H13 | 接管必要字段缺失或过期 | UNKNOWN/阻挡，不降级为端口检查 |
| H14 | 接管正确但实际不转向 | 有cmd_vel、primitive、Token与实际角速度证据；原保护有效 |
| H15 | table2朝向调整超时 | 独立呈现位置完成、朝向完成/降级原因 |
| H16 | 模式不一致/初始化风险 | 不重放冷启动脚本；模式不符返回待核查 |

先以假NX、假发布者、进程与消息故障注入验证。现场测试需经过正常现场授权，保持既有物理保护，不随意断电或在危险动作中注入故障。只改文档不代表以上测试已完成。

### 9.3 上线与回退

先关闭新动作准入、核清旧发布者/NX状态并备份配置，再同步升级协议相关端。成功后按阶段放开；旧模式与新模式显式互斥。未解决handoff不能直接切回Bool/裸START路径。回退到诊断或维护状态，不关闭STOP保护，也不回滚掉已发生的动作事实。

## 10. 日志补充与剩余问题

每次接管至少记录：UTC时间、单调时钟/实例、task/command/handoff/operation、hold revision、owner epoch、接收/采用序列、输入模式、ROS intent/primitive、实测角速度与反馈年龄。关键迁移持久化，高频指标采样，避免逐帧刷屏造成日志阻塞。

| 剩余项 | 如何处理 |
| --- | --- |
| table2朝向15秒限时后成功 | 作为独立到达合同项复核，先明确降级语义；不能直接归因relay2停滞 |
| 转向Token错误/定位数值异常 | 仍未排除，接管修复后的单段验证必须采集实际输出与反馈 |
| NX Token stale与大延迟指标 | 无时间对应不能作为本轮根因；Streaming data mean delay不是网络RTT |
| hand_state_only | 指令证据与物体证据分开，不能证明实际持有 |
| 历史日志时间关联不足 | 保留“高可信历史原因”表述，不能宣布唯一根因已复现 |

本次证据位于项目外 `C:/Users/Administrator/Documents/brain源码核查_20260909/incident_20260831/`；入口为`排查结论_20260909.txt`与`navigation/ros_timeline.txt`。本机源码快照只用于审计，不作为覆盖远端部署的来源。

## 11. 抓取出站修复阶段的交付检查

- 大脑：接管未确认时不发relay2，交接失败不重抓，界面能够说明阻塞原因。
- VLA：唯一协调器、完整发布者收尾、手部策略、NX受控接管与可核验回执。
- 导航：可查询的中性准备、真实NX准入检查、原安全门与实际转向日志。
- 联合：正常与失败场景通过H01–H16；在同一handoff下关联“STOP→归还→NX接管→relay2→实际运动”。

**验收结果应证明控制接管正确，而不仅是某一轮偶然走通。**

## 12. 全Pipeline统一切换方案

本节将前面的pick出站方案推广到所有切换点，同时限定哪些位置不应重新发START。以下是目标合同；并非所有后续切换已经发生相同故障。历史日志已证明的问题与设计防护需要分开表述。

### 12.1 全流程与七个控制边界

```text
初始安全待机
  T0 取得导航执行权限
导航到table2 → 确认到位与停止
  T1 NAVIGATION → VLA_PICK
抓取 → 效果记录 → 策略停止 → 抓取验证/现场确认
  T2 VLA_PICK → NAVIGATION
导航到relay2 → 本段停止
  T3 NAVIGATION → NAVIGATION：前进模式切换为横移模式
横移到relay3 → 本段停止
  T4 NAVIGATION → NAVIGATION：横移模式切回前进模式
导航到table1 → 确认到位与停止 → 放置前现场批准
  T5 NAVIGATION → VLA_PLACE
放置 → 释放效果记录 → 收尾 → 放置验证
  T6 VLA_PLACE → SAFE_IDLE
任务结束：稳定待机、资源与物体事实明确
```

若流程以后增加放置后的导航动作，T6改为`VLA_PLACE→NAVIGATION`，采用与T2相同的接管机制，但手部策略为空手/已释放策略，不能沿用闭手搬运。不能仅因为协议字段叫navigation_port_ready就默认任务结束后还应恢复运动。

| 边界 | 源→目标 | 手部事实/策略 | 关键要求 | 是否重新取得NX执行权限 |
| --- | --- | --- | --- | --- |
| T0 | SAFE_IDLE→NAVIGATION | 当前空手或任务批准的状态 | 新任务准入、现场启动授权、正确模式/输入、最新NX状态 | 按实际保持状态受控取得；不能盲目初始化 |
| T1 | NAVIGATION→VLA_PICK | 空手；抓取允许后才改变目标 | table2当前到位、原导航停止、相机就绪、VLA准备输入已核验 | 是，目标源为pick；旧导航禁止再发动作 |
| T2 | VLA_PICK→NAVIGATION | 已确认持有；保持目标 | pick效果、发布者停止、导航中性输入、NX接管确认 | 是；这是历史故障重点 |
| T3 | NAVIGATION→NAVIGATION | 保持持有 | relay2凭证、横移起始位置/朝向、原段停止、无旧指令残留 | 正常不切源、不重发START；NX状态仍要核验 |
| T4 | NAVIGATION→NAVIGATION | 保持持有 | relay3凭证、已过门、下一段前进模式；清横移残留 | 同T3 |
| T5 | NAVIGATION→VLA_PLACE | 持有；放置开始前保持 | table1当前到位、导航停止、放置前条件与现场批准 | 是，目标源为place；不能只按TCP可绑定开始 |
| T6 | VLA_PLACE→SAFE_IDLE | 已释放/实际状态核定 | 发布者停止、身体安全待机、手目标明确、无待解决控制事务 | 不为结束任务解除保持；必要受控过渡仅用于达到待机状态 |

`forward_path`和`lateral_path_aligned`是导航运动模式；`PLANNER/STREAMED_MOTION`是NX输入模式。两者不得混用，T3/T4不能通过NX冷启动模式切换实现。

### 12.2 统一模型：源、执行模式、业务状态各自记录

每个handoff在创建时就保存：

- task_id、handoff_id、operation_id、source_command_id、destination_command_id（SAFE_IDLE时可为空）；
- source_owner、target_owner：NAVIGATION / VLA_PICK / VLA_PLACE / SAFE_IDLE；
- expected_source_epoch、target_epoch、relay_instance、NX instance、hold_revision；
- effect_evidence_ref、hand_policy_revision、navigation_proof_ref、operator_approval_ref；
- target_mode、prepared_input_id、prepare_expiry、transition_profile、schema版本与请求摘要。

source_owner不是由监听端口猜测，而来自受管事务及执行器实际状态。target_owner=SAFE_IDLE表示目标控制状态为安全待机，不要求把NX LowCmd控制进程停掉。另设transport_owner表示谁监听5556，不能与实际控制源混成同一字段。

接管凭证按目标command与源代次绑定。T2的凭证可以建立本次导航控制会话，但不是永久授权：T3/T4仍检查当前epoch、NX实例、无新保持事件和本段前置条件；一旦T5切给place，旧导航会话凭证全部失效。

### 12.3 导航交给VLA：T1与T5的具体实现

这两个方向不能只把T2的字符串改名，必须拆开目前“一启动hook就可能发控制”的行为。

1. 大脑先持久创建下一条pick/place command，并绑定刚完成的导航凭证；接收命令不等于授权开始策略。
2. VLA验证同task、正确目标、地图/定位上下文与当前位姿。历史成功命令不是当前位置证明；到位的朝向降级必须按动作允许条件处理。
3. 导航建立转出屏障：原goal核清、撤销其继续发运动的资格，维持身体中性输入；T1/T5完成后禁止迟到旧goal/Arm/Execute生效。
4. VLA先做模型/资产/相机准备，保持`PREPARING`。准备期间不得绑定第二个活动发布者或发送会启动NX的k/i/pose_mode。
5. 完成现场批准。尤其place原来的Enter批准应保留并绑定本次command、场景证据与版本。等待批准尽量放在导航稳定待机或已确认的隔离待机阶段，不能长时间留下无人维护的空端口。
6. 协调器关闭导航转发、确认释放，再允许目标VLA受管发布者绑定。此时只能发布已核验的初始中性/保持输入，策略执行门仍关闭。
7. NX确认目标源epoch、准备输入和允许模式；协调器执行一次受控接管。NX回执证明已采用后，签发绑定destination_command_id的一次性动作启动许可。
8. hook/adapter消费许可后才运行抓取策略或放置序列；同一许可重复请求不能再启动一遍。

需要将现有hook拆成“准备资源→等待接管许可→执行业务动作→报告效果→停止/收尾”阶段。pick的k/i与place的`publisher.pose_mode()`属于会改变执行状态的操作，不能在受管模式下继续绕过协调器自行发送。若原推理栈离不开这些操作，需要增加受管接口把模式变化纳入同一事务，先验证再开放；不能只删除操作导致初始化语义改变。

只允许预先核验的目标输入/过渡曲线，不自动把抓取或放置的第一帧当安全中性帧。身体初始姿态不合适时返回PRECONDITION_FAILED，现场处理，不能强行混合Token通过检查。

### 12.4 VLA转出与任务结束：T2、T6分别处理

**T2抓取转导航：**采用第3节详细流程；抓取效果与持有核验通过，才配置搬运手部策略。抓取照片验证若开启，处在稳定待机阶段完成，不因图像分析延迟维持运动；验证前后不把缓存holding清成空手或确定物体存在。获取新图也不触发新的动作。

**T6放置后结束：**报告“释放效果”“策略/发布者停止”“安全待机建立”三项独立状态。正常最终放置的成功合同改为：

```text
release_effect按本任务证据策略通过
+ 原策略与动作发布者停止
+ NX安全待机状态明确、反馈有效
+ 手部目标与已释放事实一致
+ 未解决交接/控制事务为零
```

此时`safe_hold_latched=true`可以是预期状态，不强求false，也不强求外部导航输入已采用。transport可以由relay监听，但必须阻挡运动帧；不能以任务完成为由关闭本体稳定控制。保留手目标所需的发布者属于明确的待机资源，不等同于未停止的业务策略；应单独标注owner与职责。

NX停止保持与独立手桥要一起核查：身体进入待机不自动意味着手应该张开；只有实际放置效果允许时才使用已释放策略。若物体是否释放未知，进入待处理，不为“清空holding”而发张手。

若SAFE_IDLE所需身体稳定状态不能直接由当前保持达到，允许单独经过现场授权的安全过渡事务；它不是恢复导航，仍不允许goal或重放place。

“放置后已释放但收尾失败”只修复收尾；“动作未发生且确认未开始”可结束本次任务。两者不能统一重试放置。`require_place_success()`按after_action_target选择`SAFE_IDLE_CONFIRMED`或`CONTROL_CONFIRMED`，并保留任务证据等级，不把手目标张开称为物体放到桌面。

### 12.5 通用接口与状态机

对外统一新增（拟议，仍由VLA Bridge承载控制交接）：

| 接口 | 行为 |
| --- | --- |
| `POST /v1/control/handoffs` | 接受source→target交接，绑定命令、策略、批准与版本；先持久化，不代表已启动动作 |
| `GET /v1/control/handoffs/{handoff_id}` | 查询阶段、源/目标、NX状态、证据和blocker |
| `POST /v1/control/handoffs/{handoff_id}/check` | 只核查，不改变控制目标 |
| `POST /v1/control/handoffs/{handoff_id}/continue` | 经批准继续原交接阶段，不重新执行源/目标业务动作 |
| `POST /v1/control/handoffs/{handoff_id}/cancel` | 停止新动作准入并进入收尾；不是自动反向交接 |

第7.1节task下的接口如保留，只作绑定command后的别名，必须调用同一个实现和操作记录；禁止新增第二个协调器。第7.2节导航准备只适用于目标为导航；目标为VLA时使用VLA内部prepared executor合同。大脑与正常worker二者只能由一个启动交接，另一方查询既有handoff；以task+源command+目标command+方向建立唯一约束。

统一阶段：

```text
REQUESTED
→ CHECKING_SOURCE
→ WAITING_APPROVAL / PREPARING_TARGET
→ QUIESCING_SOURCE
→ OUTPUT_SWITCHING
→ TARGET_INPUT_PREPARED
→ TARGET_CONTROL_PENDING
→ CONTROL_CONFIRMED（目标将执行动作）
或 SAFE_IDLE_CONFIRMED（目标为任务结束待机）

任意不确定 → HANDOFF_RECOVERY_REQUIRED
取消 → CANCELLING → SAFE_IDLE_CONFIRMED或保持隔离
```

目标发布者的准备可以先完成无副作用的模型加载，实际绑定必须在源释放之后。安全待机若不需要外部输入切换，可以跳过不适用阶段，但要显式记录skip理由；不能为了走完状态机而发START。

T3/T4不新建跨源handoff，创建本段导航command并记录当前控制会话凭证。目标输入确认、启动许可、恢复操作的幂等分别实现：重复查询/继续交接不能再次消费已用的业务启动许可。

### 12.6 三端新增改动项

本表追加到B/V/N/D各6项；明确哪些旧函数不能继续沿用原有语义。

| 编号 | 文件/模块 | 全链路新增改动 |
| --- | --- | --- |
| B07 | `reception_real.py::run()` | 在T0/T1/T2/T5/T6显式调用/跟踪对应交接；T3/T4走同源导航条件；禁止所有阶段套用pick出站检查 |
| B08 | `vla_client.py::require_pick_success()/require_place_success()` | 分离效果与交接目标，place最后一步接受SAFE_IDLE_CONFIRMED；不强制所有成功均navigation_port_ready=true |
| B09 | `reception_real.py::_verify()`及holding更新 | 保留历史效果与当前事实，验证期间不先写反向holding；证据冲突阻挡动作，不改变手目标 |
| B10 | 前后端状态映射 | 展示当前源/目标、等待批准、准备、接管、待机；成功结束与“随时可发新动作”分开 |
| V07 | `handoff_coordinator.py` | 从单向改双向与SAFE_IDLE目标；绑定一次性目标动作许可；同一交接只有一个调用者取得执行权 |
| V08 | pick/place hook与推理启动适配 | 支持准备/等待许可/执行分阶段；收归k/i/pose_mode权限；模型加载不擅自操作NX |
| V09 | `bridge_service.py::_worker()`/OperatorApproval | 现场批准绑定command、handoff、事实版本；长等待放在已确认稳定状态；取消不自动启动反方向 |
| V10 | place adapter/手桥/relay | 终态待机与归导航分开；保持手策略与身体策略独立，消除重复RELEASE及task_stopped通用握拳 |
| N07 | NX受控接管协议 | target_owner及target_mode明确，按源/目标核验；允许SAFE_IDLE是合法终点，不为所有交接发START |
| N08 | NX反馈与许可 | 反馈当前控制源及保持原因；较新STOP作废未消费许可；目标动作权限与中性输入接管分开 |
| D07 | 导航Agent/9882 | 增加导航转出屏障，T1/T5完成后旧goal/Arm不得生效；同源段间维持控制epoch |
| D08 | Token控制器/动作模式发布 | relay2→relay3→table1按command原子切motion_mode与intent，清旧横移/转向目标；不切NX PLANNER模式 |
| D09 | 导航成功证据 | table2/table1提供当前位置/朝向/时间与定位上下文；段间成功不免除下一段实时条件检查 |

T0若需要冷启动属于现场启动流程，不能由handoff服务偷偷启动新SONIC进程。已有控制器兼容模式不能确认时阻挡任务，不“尝试启动看看”。

### 12.7 相机、审批、事实与导航凭证

- 当前navigation-only分支跳过DREAM细检，相机由既有VLA/NX服务提供；动作控制权切换不隐式重启或抢占相机。
- 如果启用DREAM细检，使用独立camera_owner/camera_epoch与就绪合同；动作源交接和相机交接都完成后才运行依赖图像的策略。相机出错优先补观测，不重复pick/place。
- 地图、定位实例、目标位姿或物体事实变化，使相关action permit与approval失效。重新观测和批准不能重写旧导航成功记录。
- 现场批准必须先通过身份校验，不信任请求体姓名；T5批准只允许本次place，不允许由旧Enter或旧网页请求放行新任务。
- 如果中途重新定位导致旧凭证不能使用，应重新核验当前到位条件并形成独立的当前条件证明；不能伪造导航成功或自动从table2重跑整条流程。
- 下一轮任务从T0重新准入；前一轮SAFE_IDLE不代表holding一定空，也不代表原导航到位凭证仍适用。

### 12.8 全链路故障处理矩阵

| 发生位置 | 已确认事实 | 处置 |
| --- | --- | --- |
| T1目标VLA尚未开始 | 导航已停，策略准备失败 | 保持安全待机；报告未开始，不擅自导航离开或发pick |
| T1/T5目标已绑定但NX未确认 | 无动作许可 | 只允许准备输入；核查原事务，不启动业务动作 |
| T2抓取有效但无法接管 | 物体效果保留 | 继续持有策略，修原交接，不重抓 |
| T3/T4横移/前进切换失败 | 源仍导航 | 停止本段、保持物体；核查原导航命令，不重启VLA |
| T5批准过期或table1位姿改变 | 放置未被允许 | 保持持有与稳定身体，重新核查，不自动place |
| place已释放但T6失败 | 释放效果保留 | 只修复身体/手部安全收尾，不再放一次、不默认闭手 |
| 任一边界新STOP/取消 | 新保持优先 | 旧permit/RESUME失效；不自动发反向START |
| 任一进程重启 | 原身份/效果需查 | 保留未解决状态，禁止从默认owner恢复动作 |
| 流程结束后新任务 | 前任务已安全收尾 | 重新检查当前holding、地图与控制源，不复用旧行动许可 |

业务动作成功与控制资源收尾不一致时，界面必须同时显示两者；不能用一个FAILED遮盖已释放/已抓取事实。

### 12.9 全链路新增验收（追加H17–H32）

| 编号 | 场景 | 必须成立 |
| --- | --- | --- |
| H17 | table2成功转pick | 导航旧命令冻结，NX确认目标VLA后才运行策略 |
| H18 | pick模型加载失败/超时 | 未获得业务许可时不得发k/i启动；保留稳定待机 |
| H19 | T1交接响应丢失 | 原handoff可查询，目标pick最多启动一次 |
| H20 | relay2转relay3 | 同导航源、正确横移模式，不新增NX START |
| H21 | relay3转table1 | 清旧横移intent、切前进模式，未过门不执行 |
| H22 | table1成功转place | 当前到位与本次批准有效，目标接管后才place |
| H23 | place等待批准时取消 | 不执行放置，不自动张手；稳定收尾或隔离 |
| H24 | 旧Enter/旧批准迟到 | 不放行新command或事实已变化的旧command |
| H25 | place结束转SAFE_IDLE | 不为结束任务强制START；保持身体稳定与正确手策略 |
| H26 | place成功但归还失败 | 释放事实保留，重试收尾不重试place |
| H27 | 放置后另有导航步骤 | 使用空手策略接管，不沿用carry_closed |
| H28 | NX模式与motion_mode混淆 | T3/T4不触发PLANNER/STREAMED_MOTION初始化 |
| H29 | T1/T5过程中旧导航发令 | 旧epoch/permit无效，不能与VLA并行 |
| H30 | 相机细检启用/关闭两种分支 | 单一相机所有者；动作交接不误重启相机 |
| H31 | 大脑和worker同时请求handoff | 一个事务、一个目标动作启动许可 |
| H32 | 完整Pipeline及下一轮启动 | 逐个边界有身份和反馈；最终待机；下一轮重新准入 |

保留H01–H16，专项验收总计32项。先分别验证T1/T2/T5/T6，再验证T3/T4模式衔接，最后验证完整任务；不要只测pick→relay2后宣称所有状态切换已解决。

### 12.10 本版整体完成标准

三端应交付一套统一的双向交接合同、一个协调器、分层状态与32项测试；VLA与导航不再各自发互不关联的START/STOP来猜测控制权。明确区分“导航段完成”“动作效果完成”“跨源接管完成”和“安全待机完成”。历史故障修复与后续切换预防共用该机制；通用断点续跑仍独立维护。