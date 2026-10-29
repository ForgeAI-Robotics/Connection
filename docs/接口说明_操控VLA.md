# 接口说明：操控 VLA

> **2026-10-28 当前源码：** source_gate 模式新增 VLA direct-v1 接入，见下节；原 unified 模式与带日期的 relay／acquire 记录保留。三个接待按钮分别绑定真实／模拟执行端，不静默回退。大脑源码适配、隔离验证及大脑／网页重载完成，已有真实操控失败回执；后续 VLA／O6 修订按下文注明来源，完整动作验收尚未完成。

## direct-v1：直连操控与接收源选择

本地交付 `/home/fq/workspace/sonic_wbc/g1_brain_vla_bridge/大脑调用接口.md` 与 contracts/direct_vla_v1.json 的修订为 20261028-direct-v1，业务版本 fq/reception-lan/v1。大脑选择 VLA 来源并确认实际生效后，直接 POST `/v1/vla/tasks`；不调用 NAV/VLA prepare、网关 permits、native/O6 操作，不检查模型、相机、身体模式和双手就绪。具体执行条件由 VLA 内部检查并通过原任务回报。大脑规划目录表示已接入的动作种类，不代表远端已经在线或允许真机动作。

请求只发送 contract_version、task_id、command_id、operation、object_id、target_area；operation 支持 open_fridge／grasp_cola。原 Runtime 请求和导航业务关联仍保留在本地账本，不携带 control、旧授权会话、worksite_evidence 或 navigation_proof。连续操控保持 vla 选通；切源只在下一条真实命令派发时进行，不因 finish 或查询自动执行下一步。网关字段及原请求查询规则见[接收源开关](接口说明_机器人统一网关.md#sonic-接收源开关source_gate)。

VLA 查询、pause、finish、cancel 继续使用原 `/v1/vla/tasks/{command_id}` 路径族。任务调用使用 body_control.vla_token_env（空值沿用 token_env）；finish 使用原请求级 verifier Bearer，不写入任务账本。进入 running／waiting_result_confirmation 后沿原网页流程暂停、提交真实效果证据，再由人工“继续”推进。开门要求门打开且稳定、双手离门；抓取要求物体在手且 clear_of_fridge=true，不用手指闭合或程序退出冒充效果。大脑不发送 SONIC START／STOP 或 Planner 恢复命令；本轮不改 VLA 收尾策略，STOP 与实际姿态影响留待执行端对齐和真机验证。

真实回执必须属于原 task_id／command_id／operation／object_id／target_area，并声明 execution_mode=direct。按 VLA 返回的 policy_stopped、publisher_stopped、body_stopped、hands_stopped、hold_verified 解释当前命令收尾，缺失、null、false 或相互矛盾不补成成功；不再要求 navigation_port_ready／authorization_released。准备前明确 action_started=false 且发布未生效的失败，只清除该命令执行槽，原 body_stopped=null 保留，不宣称身体停稳。明确 REAL_ACTIONS_DISABLED 且 action_started/action_state_uncertain 均为 false 的 503 是未受理拒绝；普通 503／断线／超时仍为未知，先查原命令。

持物运送仍由原业务顺序约束。每条真实运送导航前读取 VLA 当前 holding_evidence，确认有效、物体及原抓取任务／命令身份匹配；不把历史抓取成功当作当前持物，不签发或续期旧 O6 授权证明。双手具体反馈、保持判断及证据失效由 VLA 负责。导航模拟模式只模拟路线，不据此生成真实到位证据；现场作业确认仍由按钮显式采集。

整轮取消仍立即结束大脑编排，沿原控制账本与工作器向实际执行端请求 cancel，分别保存 NAV、VLA 停止和网关全关结果；VLA 取消后先有界查询原命令，再处理全关，不重复取消 POST。旧取消未确认不阻断新任务；旧收尾不能在新任务开始后再发停止或全关。真实 VLA 与 O6 的启动／部署由操控侧执行，不因大脑加载适配器自动触发。验证见[大脑直连接入](多端联调_操控与抓取.md#brain-vla-direct-integration)。

2026-10-29 操控侧本地对接说明新增修订：撤销 O6 的 0.5 秒输入断流／5 秒首帧期限，静默时等待并保留目标，显式取消、实际反馈失效和 CAN 故障仍停止更新。该说明声明已同步 NX 并核对运行文件哈希；这是操控侧交付记录，本次文档整理未独立复核远端进程，也没有真实动作复验。不能只重启 HTTP 就假定其它机器／旧 O6 进程已更新。原 10-28 零首帧失败仍保留在[操控排障](多端联调_操控与抓取.md#direct-v1-进入-running-后-o6-零有效帧退出)。

初版日期：2026-08-27  
远端现场记录基线：2026-09-20；大脑调用规则核对：2026-10-09
契约版本：`fq/reception-lan/v1`  
适用范围：大脑到 VLA 的任务、原命令查询与核验。2026-10-22 起支持新 native `open_fridge/grasp_cola`；旧 `pick/place` 合同保留给原后端。

> 当前 source_gate 以文首 direct-v1 和大脑接口为准；“新动作接入”保留业务效果及旧后端范围；2026-10-09 及更早章节描述旧抓放链路。源码接入、运行实例更新和真机验收分别确认。

## 大脑统一授权对接（2026-10-23，接口离线串联通过）

> 历史 relay 接线，以下至“新动作接入”保留当时行为；当前 direct-v1 不执行这些交接。

本轮共同需求统一见[relay 控制权对接说明](多端联调_大脑任务与恢复.md#relay-history)，请与导航侧审阅同一份正文。此前详细控制协议草案已撤回；本轮优先复用现有业务接口、acquire／release／查询及可靠性逻辑。

两端映射反馈已汇入[最小对接表](多端联调_大脑任务与恢复.md#relay-history-收敛后的最小对接表)：保留客户端交接路径形式并更新大脑权威修订，采用操控侧建议的 stop 与 relay 管理入口；不新增整套 participant API。接口代码已实现，现场运行入口尚未切换。

操控侧负责改接大脑授权、拆开本次退出与导航恢复，以及原 relay 的受控切换与状态查询。连续开门／抓取的起始检查也需去掉“导航转发已经恢复”的依赖，保留真实到位和作业条件核验。第一版沿用每任务独立 worker 生命周期；身体和双手的必要退出／保持检查仍由执行侧落实。具体最小入口和字段由操控侧对照代码提出，与大脑确认后在此维护。

大脑与本地操控／relay 的实际代码隔离对接结果见[第一阶段记录](多端联调_大脑任务与恢复.md#brain-vla-peer-offline)：原连续 VLA 状态与 relay 新鲜流序列化问题已修复并完成 34 项复验。后续 NAV 查询已使用独立 NAV_CONTROL_TOKEN／私有凭据文件；最新三端实际 HTTP 完整 Runtime 串联通过，见[三端复验记录](多端联调_大脑任务与恢复.md#brain-three-peer-offline)。真实输出、模式和身体／双手保持仍待完成，真实执行继续阻断。

下文的 NAV 租约、restoring_navigation 和导航就绪终态条件描述迁移前实现，不表示新方案仍要求这些条件。现有真实执行 readiness 阻断不能因授权方迁移而解除。

## 新动作接入（2026-10-22）

Connection 已接入 `GET /v1/vla/capabilities`、原提交／查询／取消路径，以及原命令的 `/pause`、`/finish`。本次按同级 `sonic_wbc/g1_brain_vla_bridge` 的代码与《大脑调用接口》《导航交接接口》核对。新能力用于 `body_real` 通用身体任务，以及用户明确指定的真机接待分支；真机“开始接待”先导航、开门、原地抓可乐并核验，再沿原中转路线运送到 1 号桌；暂不关门或放置，具体配置见[大脑接待流程](接口说明_大脑%20Brain.md#81-流程与请求身份)。

| 能力 | 接入规则 |
| --- | --- |
| `open_fridge` | 登记作业路线与冰箱 ID 后可规划；完成要求门打开且稳定、双手脱离接触区 |
| `grasp_cola` | 本任务同一目标的开门步骤必须核验 PASS；抓取要求目标在手中且已离开冰箱接触区 |
| `close_fridge`、旧 `pick/place` | 当前 native 配置不支持；整份计划在首个导航派发前检查，不以改名替代 |
| 原后端 | 能力接口明确 404 时保留旧抓放兼容；网络错误、非法能力回包不当作旧后端 |

`configured=true` 只表示执行器已配置。2026-10-22 的旧接线中，动作仍检查 `real_action_enabled`、`navigation_handoff_ready`、`recovery_required=false` 和 `policy_state_uncertain=false`；VLA 自身继续核查导航原命令、硬件与租约。新任务中的导航也检查 VLA 已停、无活动命令、导航通路就绪及无恢复要求，避免失败／取消后直接导航。仅导航接待变体不依赖 VLA 能力发现；真机许可关闭保持原门控行为。

当前 source_gate 不采用上一段的 NAV lease、旧恢复／端口门禁或远端 readiness 前置。大脑使用已支持动作目录编排，VLA 内部检查具体执行条件；direct-v1 请求与收尾见文首。

### 作业点与规划配置

真机“开始接待”的配置在所选 Skill 的 `real_workflow`，直接引用其 `points.table2`；用户已确认这就是冰箱前真机站位，名字与 ID 的沿用不代表另一个物理点。本分支两动作共享原导航证明，中间不导航，坐标只维护在 Skill 一处。

下面的 `reception_real.native_vla` 是通用身体任务的独立动作目录，不是本次接待的必填项；公开模板默认 `points: {}`、`operations: {}`。未登记的通用动作仍拒绝。地址由现有网络／环境配置解析。

- `points.<点键>`：包含 `target_id`、`route_phase`、正整数 `leg_index`、三个有限数的 `goal_xyt`、`motion_mode`（`forward_path` 或 `lateral_path_aligned`）；发出 NAV 请求时固定 `frame_id=map`、要求最终朝向。
- `operations.open_fridge` / `operations.grasp_cola`：包含 `object_id`、按顺序引用点键的非空 `route`、正数 `timeout_sec`。目标与物体 ID 使用 1–128 位 ASCII 机器标识，首字符为字母或数字，其余允许 `_.:-`。
- 通用动作目录中的每个动作执行自己的登记导航路线；抓取前重新取得该路线的真实 NAV 命令证明。两动作路线终点的 `target_id` 必须相同，各自起始位姿仍须适合对应示教。此配置不替 NAV 登记点位。
- 能力、配置和编译步骤绑定进任务快照，恢复旧任务不重读新配置。人工跳过开门不能生成开门 PASS，也不能解锁后续抓取。

登记后的动作绑定形式为 `open_fridge: {object_id: fridge_1, route: [fridge_open_start], timeout_sec: 600}`、`grasp_cola: {object_id: cola_can_1, route: [fridge_grasp_start], timeout_sec: 600}`；这些仅示意 ID 与字段，两个起点的导航参数须现场填写。普通“取冰箱里的可乐”可规划必要的开门＋抓取；当前不支持混入额外运送／放置要求，纯导航继续沿原登记路线规划。

### 暂停、效果确认与大脑继续

1. 大脑保留原 `task_id/command_id` 提交动作，等待 `running` 或 `waiting_result_confirmation` 后在 Runtime 进入 `waiting_human`，状态带 `vla_confirmation`；此时动作可能仍运行。
2. 现场判断适当时机后，通过大脑 `POST /api/task_vla/pause` 请求策略暂停，body 为 `{"task_id":"原任务ID","command_id":"原命令ID"}`。网页提供“暂停策略以核验”。请求受理不等于已经暂停。
3. `POST /api/task_vla/query` 使用相同身份，只查询并记账。等到 `waiting_result_confirmation` 和 `verification_ready_at` 后，由真实现场人员或视觉核验模块采集证据。网页查询按钮调用此接口；普通任务状态轮询只读取账本。
4. 授权核验方调用 `POST /api/task_vla/finish`，Header 为 `Authorization: Bearer <VLA verifier credential>`，body 为原身份加 `evidence`。凭据仅用于当前请求，不存配置、账本或普通任务日志；网页转发保留该 Header。也可由 VLA 原有 `confirm_effect.py` 直接确认，再让大脑查询原命令。
5. 查询观察 VLA 经 `stopping_policy`、`restoring_navigation` 到终态。查询或 finish 回包不会自动推进下一步；点击已有“继续”，Runtime 再查原命令、核验效果与停止／归还证据后推进。终态未到时“继续”拒绝重发。

效果证据字段：

| 字段 | 要求 |
| --- | --- |
| `source` | `operator_observation` 或 `visual_verifier` |
| `reference` | 可追溯观察记录／图像记录 ID，不能编造 |
| `observed_at` | 带时区，处于 `verification_ready_at` 之后至当前时间之间；终态证据时间不得晚于 `completed_at` |
| 开门 | `door_open=true`、`door_stable_open=true`、`hands_clear_of_door=true` |
| 抓可乐 | `object_grasped=true`、`holding` 等于请求物体 ID、`clear_of_fridge=true` |

终态还必须满足原任务／命令／operation／目标／物体身份、完成时间、`success=true`；旧后端另要求 `policy_stopped=true`、`navigation_port_ready=true`，direct-v1 则使用文首分别列明的策略／发布／身体／双手收尾字段；`effect_verified_by/evidence_level` 必须与原证据来源一致，`autonomous_effect_verification=false`。当前没有自动开门／抓稳的视觉判定接线，Planner 不能代写确认。

新 `/pause` 只暂停策略推进，不是姿态冻结或急停。原大脑“暂停任务”仍走取消下游命令并核查停止；source_gate 下“取消任务”先结束大脑编排，再异步请求 VLA 取消并记录全关，回执不代表身体已停。重复暂停／finish、丢失回包或重启后，已记录的核验意图只查询原命令，不自动重发 POST；明确的参数／权限拒绝允许修正后再次人工提交。不同证据不能覆盖已记录的确认。

以下为 2026-10-22 旧交接限制，不适用于 direct-v1：`recovery_required` 或未知策略状态保持阻断。VLA `/v1/vla/operator-state` 留给现场授权维护，本次不接入自动恢复按钮，不修改原失败账本。NAV 的 `fq/vla-handoff/v1` 租约交接由 VLA 调用，Connection 不接管共享 relay；NAV 未实现时不能执行新动作。当前在线部署与真机范围见[新接口验证](多端联调_操控与抓取.md#新-vla-接口离线接入验证)。

## 大脑调用与核验规则（2026-10-09）

本节保留旧 pick/place 后端的大脑规则；新 native 动作以文首接入说明为准。后面的远端实现记录属于原标注日期的现场基线，不代表当前 VLA 服务、策略、相机或全部状态已重新验收。地址按当前网络与生效配置解析，旧网段示例不作为当前连接地址。

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

## 新办公室 GR00T 仿真实验（2026-10-10）

独立实验已通过标准大脑调用真实 GR00T 推理和仿真物理执行，实际抓取失败，大脑保留失败并停止后续步骤。
这份集成使用文件触发推理，本轮临时 pick 任务适配负责原导航交接检查、提交／查询和物体效果回执，
不另建接待调度，不代表主环境生产 VLA HTTP 服务已部署。所检查集成的 place 执行入口尚未核实，
实验适配明确返回 501，未模拟放置成功。接线、证据及限制见
[本轮联调](多端联调_操控与抓取.md#e-n1-s9)。

随后用户授权的调用演示在另一个独立副本中增加了 place 的失败专用回执：保留原请求身份，
核对通路空闲后记录 `UNSUPPORTED_PLACE_EXECUTOR`、`started_at=null`，没有执行放置动作。
大脑通过现有跳过接口走到流程末尾，仍为 `failed`、`flow_finished=true`，不降低抓放核验标准。
这不改变生产能力或原 pick-only 实验的 501 行为，详见
[完整调用演示](多端联调_操控与抓取.md#e-n1-s18)。

## 当前现场实现口径（2026-09-17）

> 历史 pick/place／共享 5556 实现。以下至“操控侧检查与独立启停”保留原记录中的“当前”口径，不描述 2026-10-28 之后的 direct-v1。

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

核对日期：2026-10-29。当前面板只调用官方 `run_agent_vla_runtime.sh start → status` 或 `stop`，依赖检查和受管 O6 准备归启动器；不再额外调用旧 phase-aware 检查或 relay。下面保留带日期的安全门／旧 place 操作供历史排障，不能当作新开门、抓取的必经步骤。

三端冷启动与收工顺序见[控制面板接口说明](接口说明_控制面板.md#三端启动与任务发布)。以下 SSH 别名应事先配置；`<VLA_BRIDGE_ROOT>` 为桥接目录，`<VLA_PROJECT_ROOT>` 为其所属项目目录。面板使用当前网络的操控角色地址，桥接目录可用 `VLA_REMOTE_DIR` 覆盖。

当前独立 NAV 入口启动配套 NAV／SONIC；VLA 相机需另行启动，再启动 VLA HTTP 与其受管 O6。相机使用面板独立卡片，命令与停止范围见[相机入口](接口说明_控制面板.md#vla-三路相机卡片2026-10-29)。不能沿用旧 DREAM 三端一键脚本替代当前流程。

### 操控侧前提

- 机器人有可靠支撑，急停由现场人员掌握；
- SONIC 已进入稳定站立，是物理 `rt/lowcmd` 所有者并启用 persistent controller；
- NX 相机 `:5555`、SONIC state `:5557` 和 LinkerHand 在线；
- 核对当前任务／输出占用与依赖，不因历史失败或未核清旧收尾永久拒绝新任务；
- VLA 使用自己的发布连接，SONIC 按来源选通；不再要求取得旧 relay 共享发布端口。

以下配置为旧部署记录，当前执行条件以 VLA 启动器与实际回执为准；不得为接入新模式照抄关闭保护。历史安全门控位于 `<VLA_PROJECT_ROOT>/sonic_safety_gate/config.env`。2026-09-20 现场记录中的默认值：

```bash
SONIC_SAFETY_GATE_MODE=automatic
```

`automatic` 要求安全门启用、supervisor 在线且为 `WAITING`。`off` 只允许用于尚未移植 vendor 安全运行时的设备，并要求运行中 SONIC 同样显示为 `legacy/off`。

### 静态与实时依赖检查

```bash
ssh gpu4090
cd <VLA_BRIDGE_ROOT>

./run_agent_vla_runtime.sh check
./run_native_action_stack.sh live-dependencies
```

检查不提交业务任务。具体检查项由当前 VLA 版本实现，检查结果不代替动作效果；面板正式 start 将检查与 O6 准备交给启动器，不要求大脑逐项重复。

### 独立启动与状态

启动 VLA HTTP `:8091`，由官方启动器完成检查和受管 O6 准备；不启动导航 relay：

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
- 当前来源通过独立网关查询；任务派发前由大脑选通 VLA，不要求 relay 存在；
- 相机、SONIC 和灵巧手满足动作前置。

`startup_safety_attested=true` 若存在，只证明对应启动检查；每次业务任务由 VLA 内部重新核对实际执行条件，不依靠历史绿灯。

### 现场批准操作

以下仅为旧 place 的终端批准；当前开门／抓可乐按上文原命令 pause／finish 和真实效果证据处理。

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

当前 direct-v1 的 stop 只停止 VLA HTTP；O6 反馈与保持服务继续运行，不停止 SONIC、相机，也不代表动作停止。任务执行中启动器可拒绝停服务，应按原任务 cancel／查询处理，不以杀进程伪造收尾。

检查失败时先查看原始 blocker：HTTP 在线而 `service_ready=false` 不代表可执行；仅 `:5556` 空闲也不代表交接完成。独立停止被拒绝时保留原 `command_id`，核查操控端任务与控制权，按操控命令取消接口处理；不能用大脑“取消任务”冒充远端已停止。

## 1. VLA侧职责

> **以下第 1—12 节统一保留旧 pick/place 与共享端口合同，属于历史／协议模拟参考。** 其中 NAV 回查、`navigation_proof`、恢复导航端口、旧取消语义不是 direct-v1 要求。当前真实开门／抓取只依文首 direct-v1 与“大脑调用接口”对接，不把旧字段混入新请求。

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


## 大脑本地操控模拟（2026-10-14）

“导航真机＋操控模拟”联调模式仅由大脑本地模拟 pick/place 的提交、查询与终态，
不连接本操控服务，也不启动策略或占用动作端口。请求结构保持一致，结果明确标为模拟，
模式选择、恢复与真实性边界统一见[大脑接待流程](接口说明_大脑%20Brain.md#81-流程与请求身份)。
