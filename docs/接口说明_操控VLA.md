# 接口说明：操控 VLA

天机操控的独立原子接口、旧完成事件读取与完整交接统一见[天机操控](接口说明_天机操控.md)。该后端复用 VLA HTTP 客户端及 Runner／Verifier，不改变本页 G1 六字段和专用动作目录，未接入 SOP；天机真实控制器及投放仍待其负责人接通和验收。

跨端日志专项见大脑维护的[实施约定](多端联调_大脑日志与跨端诊断.md#alignment-table)与[VLA 交付映射](多端联调_大脑日志与跨端诊断.md#vla-delivery)。2026-11-02 VLA2 报告日志增强及 97 项定向回归通过；原有 API 已加载日志提交 1911f8f，后续启停由用户手动操作。应用边界见[主文档](多端联调_大脑日志与跨端诊断.md#deployment-20261102)，不改变 direct-v1 请求、效果确认或停止核验规则。

> **2026-10-28 当前源码：** source_gate 模式新增 VLA direct-v1 接入，见下节；原 unified 模式与带日期的 relay／acquire 记录保留。三个接待按钮分别绑定真实／模拟执行端，不静默回退。大脑源码适配、隔离验证及大脑／网页重载完成，已有真实操控失败回执；后续 VLA／O6 修订按下文注明来源，完整动作验收尚未完成。

## direct-v1：直连操控与接收源选择

2026-11-12 按用户要求，双模型常驻准备的三路相机最低帧率统一为20 FPS（`preparation.camera_min_fps`），生效私有配置、默认值与最低配置界限同步调整；持续15秒窗口、三路时间差≤50 ms、帧间隔≤500 ms和帧新鲜度条件保持。准备统计修订为 `three-camera-15s-20fps-v2`。该阈值用于相机准备，不改变策略动作频率或效果核验。复验见[启动与相机准备](多端联调_操控与抓取.md#capture-start-hash-gate-20261112)。

2026-11-11按用户指令，抓取默认源权重统一为ckpt/checkpoint-30000-cola-101/checkpoint-30000，推理视图为该路径加-inference，使用本次同步的新推理脚本及原grasp-descent配置。正式worker从私有常驻描述符投影source_path／model_path，启动脚本保留这组绑定，不用独立入口默认覆盖；当前正式开门保持200data。大脑六字段请求及双腕／双臂成功判据不改。代码已写回、182项相关回归及单／双模型GPU合成预热通过，正式HTTP尚未启动加载；后续启动VLA应用新版，不能将合成预热当作真实相机／动作就绪。详见[cola-101交付与常驻接线](多端联调_操控与抓取.md#cola-101-resident-integration-20261111)。

2026-11-11 起，direct 模式的 VLA HTTP 每次启动建立新的持物登记会话：不把磁盘中上一会话的 holding 恢复为当前登记，控制状态和持久化状态以 holding_session_id 标明本次服务实例。holding=null 在此表示本会话尚未登记持物，不是视觉确认双手为空。原成功结果、命令身份和历史人工状态回执保留；原命令查询不重放动作，未完成命令仍按原服务重启规则处理，损坏账本继续阻断准入。同一会话中抓取成功后的持物冲突检查保留，当前 worker、O6 占用和实时执行条件仍由 VLA 判断。此变更只随 HTTP 重启加载，不重启 O6 或身体控制器。

本地交付 `/home/fq/workspace/sonic_wbc/g1_brain_vla_bridge/大脑调用接口.md` 与 contracts/direct_vla_v1.json 的修订为 20261028-direct-v1，业务版本 fq/reception-lan/v1。大脑选择 VLA 来源并确认实际生效后，直接 POST `/v1/vla/tasks`；不调用 NAV/VLA prepare、网关 permits、native/O6 操作，不检查模型、相机、身体模式和双手就绪。具体执行条件由 VLA 内部检查并通过原任务回报。大脑规划目录表示已接入的动作种类，不代表远端已经在线或允许真机动作。

请求只发送 contract_version、task_id、command_id、operation、object_id、target_area；operation 支持 open_fridge／grasp_cola；2026-11-11源码增加接待SOP内的close_fridge，见[生成关门](#close-fridge-generated)。原 Runtime 请求和导航业务关联仍保留在本地账本，不携带 control、旧授权会话、worksite_evidence 或 navigation_proof。连续操控保持 vla 选通；切源只在下一条真实命令派发时进行，不因 finish 或查询自动执行下一步。网关字段及原请求查询规则见[接收源开关](接口说明_机器人统一网关.md#sonic-接收源开关source_gate)。

VLA 查询、pause、finish、cancel 继续使用原 `/v1/vla/tasks/{command_id}` 路径族。任务调用使用 body_control.vla_token_env（空值沿用 token_env）；finish 使用原请求级 verifier Bearer，不写入任务账本。2026-10-29 新增 running-finish 扩展：在 running／waiting_result_confirmation 一次提交真实人工完成确认，VLA 内部暂停与收尾，大脑后台查询并核验后自动推进；旧未请求自动推进的 pause／finish 接口保留原语义。开门要求门打开且稳定、双手离门；抓取要求物体在手且 clear_of_fridge=true，不用手指闭合或程序退出冒充效果。大脑不发送 SONIC START／STOP 或 Planner 恢复命令；本轮不改 VLA 收尾策略，STOP 与实际姿态影响留待执行端对齐和真机验证。

真实回执必须属于原 task_id／command_id／operation／object_id／target_area，并声明 execution_mode=direct。当前双腕／双臂抓取v2的交接核对policy_stopped、publisher_stopped、action_port_released均true；其body_stopped、hands_stopped、hold_verified、output_fenced仍按实际观测保存，未知不伪造、不作为额外物体效果否决。其它合同按原策略／身体／双手字段解释收尾，缺失、null、false或矛盾不补成成功；不再要求 navigation_port_ready／authorization_released。准备前明确 action_started=false 且发布未生效的失败，只清除该命令执行槽，原 body_stopped=null 保留，不宣称身体停稳。明确 REAL_ACTIONS_DISABLED 且 action_started/action_state_uncertain 均为 false 的 503 是未受理拒绝；普通 503／断线／超时仍为未知，先查原命令。

持物运送仍由原业务顺序约束。当前双腕／双臂抓取v2在正式成功和发布通路释放后，以本任务已核验的左右腕与回位证据作为运送前置，不再读取额外O6 signed holding。旧合同仍在导航前读取VLA当前holding_evidence并核对其有效性和原任务／命令；人工跳过抓取按[大脑人工条件语义](接口说明_大脑%20Brain.md#83-控制交接与人工跳过)消费本任务持物假设，不要求另有自动持物成功记录；不把历史抓取成功当作当前持物，不签发或续期旧 O6 授权证明。双手具体反馈、保持判断及证据失效由 VLA 负责。导航模拟模式只模拟路线，不据此生成真实到位证据；现场作业确认仍由按钮显式采集。

整轮取消仍立即结束大脑编排，沿原控制账本与工作器向实际执行端请求 cancel，分别保存 NAV、VLA 停止和网关全关结果；VLA 取消后先有界查询原命令，再处理全关，不重复取消 POST。旧取消未确认不阻断新任务；旧收尾不能在新任务开始后再发停止或全关。真实 VLA 与 O6 的启动／部署由操控侧执行，不因大脑加载适配器自动触发。验证见[大脑直连接入](多端联调_操控与抓取.md#brain-vla-direct-integration)。

2026-10-29 操控侧本地对接说明新增修订：撤销 O6 的 0.5 秒输入断流／5 秒首帧期限，静默时等待并保留目标，显式取消、实际反馈失效和 CAN 故障仍停止更新。该说明声明已同步 NX 并核对运行文件哈希；这是操控侧交付记录，本次文档整理未独立复核远端进程，也没有真实动作复验。不能只重启 HTTP 就假定其它机器／旧 O6 进程已更新。原 10-28 零首帧失败仍保留在[操控排障](多端联调_操控与抓取.md#direct-v1-进入-running-后-o6-零有效帧退出)。

初版日期：2026-08-27  
远端现场记录基线：2026-09-20；大脑调用规则核对：2026-10-09
契约版本：`fq/reception-lan/v1`  
适用范围：大脑到 VLA 的任务、原命令查询与核验。2026-10-22 起支持新 native `open_fridge/grasp_cola`；旧 `pick/place` 合同保留给原后端。

> 当前 source_gate 以文首 direct-v1 和大脑接口为准；“新动作接入”保留业务效果及旧后端范围；2026-10-09 及更早章节描述旧抓放链路。源码接入、运行实例更新和真机验收分别确认。

<a id="vla-diagnostic-field-scope"></a>

### 日志对齐中的身份、时间与效果范围（2026-11-02）

来源为 VLA2 对 `e9ce1e4` 及本轮日志代码的核对；后续 API 已加载 1911f8f，实际运行与依赖边界见主文档第 11 节。诊断只读投影这些字段，不替执行端裁决冲突。

| 原字段路径 | 含义／边界 |
| --- | --- |
| 诊断事件顶层 service_instance_id | VLA API 日志进程本次启动身份，不是 worker 内部令牌 |
| handoff.execution.receiver_instance_id | O6 实例 |
| handoff.execution.source_gate_receiver_instance_id | SONIC source_gate 接收实例，不能与 O6 同名字段混用 |
| handoff.execution.controller_instance_id／input_session_id | SONIC 控制器身份／该次执行输入会话 |
| 查询回包顶层 observed_at | 查询回包生成时间，会刷新；不是原效果观察时间 |
| completion.evidence.observed_at | 已提交的原效果观察时间，不能因查询或日志记录刷新 |
| completed_at | 任务终态记录时间，不等同于某一物理停止瞬间；执行准备与暂停完成时间分别保留 execution_ready_at／verification_ready_at |
| state／result.business_outcome／exit_state | 任务结果、效果结论与收尾事实分别表达；效果已核验后仍可能因停止或持物保持未确认而最终 failed |

direct 正常执行异常路径在 handoff.original_execution_failure 保留首因，失败时可投影到 error.details.original_execution_failure；收尾错误可成为顶层 error。服务重启可将未完成任务标为 VLA_SERVICE_RESTARTED 并保留旧 handoff，不能假定所有异常／重启后的首因都固定在 error.details 同一路径。多个路径内容冲突时保留原值和来源，不静默合成停止或成功。

命令和 finish 意图保存在 current_state.json；现有代码没有命令 TTL 清理，也没有固定保存时长保证。新诊断事件有独立轮转边界，不能用事件文件存在代替完整历史。O6 首因匹配沿执行端现有选择规则，不能据本轮日志增强宣称已新增对所有字段的核验。

## 整轮诊断小摘要与归档

2026-11-02 整轮证据提交 `df42387353f406712672cec2e6fdfdf467474874` 在原命令 GET 可选增加 `diagnostic_summary`（`fq/peer-diagnostic-summary/v1`、peer=vla、state_authority=false、最多 16 KiB），仅读预生成缓存。原 task_id／command_id、效果与停止字段不变；归档 pending／partial／缺字段和预算截断不冒充物理结论。原生事件、低频图像、推理时序及哈希索引在 VLA 工作站保留，网关／大脑只收小摘要与引用；不会将 ZMQ／UDP／CAN 数据自动当作 ROS bag 已录。默认预算、测试与用户手动加载边界见[主文档整轮工程](多端联调_大脑日志与跨端诊断.md#rosbag-report-service)。

## O6 反馈故障首因（兼容新增）

2026-10-29 对齐操控侧《大脑调用接口》的 `fq/o6-feedback-fault/v1`。查询原命令的失败／取消回包可带 `result.feedback_fault` 和 `error.details.feedback_fault`，两处内容相同。原始快照也可能位于 `handoff.o6_execution_failure.feedback_fault` 或 `exit_state.runtime.feedback_fault`。旧任务缺省不补造；没有字段不代表没有故障。

| 字段 | 含义 |
| --- | --- |
| schema、fault_id、code | 首因版本、首次记录身份；当前 code 为 ENCODER_TIMEOUT／CAN_READ_FAILED |
| side、can_interface、can_id | 左／右手、CAN 接口、十进制 CAN ID；页面另显示十六进制 |
| first_fault_at | 首次异常的带时区时间；页面标为北京时间 |
| timeout_ms、elapsed_ms | 当次读取预算和实际总耗时，单位 ms；总耗时包含清旧帧、查询、等待与调度，不是设备独占应答延迟 |
| last_valid_feedback_age_ms | 故障侧最后有效反馈年龄；null 保留为未提供，不显示成 0 |
| feedback_at_fault | 故障时双方 received_at（Unix 秒）和 age_ms 的冻结快照 |
| exception_type、errno、message | 原始异常类型、错误号和信息；普通超时 errno 可为 null |
| task_id、command_id、input_session_id | 原任务、命令和输入会话身份 |
| receiver_instance_id、robot_serial、pid | 原 O6 实例、机器人和进程身份 |
| detected_monotonic_s、read_started_monotonic_s、timing_scope | 同一 NX 启动周期的单调时间与计时范围；不与工作站单调时钟直接相减 |

大脑保留原始完整 error／result 和停止字段，按原任务、命令匹配并在有上下文时核对 O6 实例／会话／机器人后，投影当前及历史失败的 feedback_fault。双位置冲突、身份不符或未知 schema 不作为已归属首因展示，原回包继续保留。字段只作诊断，不证明成功、停止、资源释放或允许重试；不改变 VLA 的 200 ms 预算、250 ms 新鲜度或封禁逻辑。状态查询仍以业务 state 判定，不能凭 GET HTTP 200 宣称成功。

2026-11-10源码将受管O6周期性编码器读取预算从1.0秒改为1.5秒，左右手共用；故障 `timeout_ms` 随实际预算为1500，不追溯改写历史1000ms故障。启动首读0.25秒、反馈新鲜度、双臂判据与停止／持物检查保持。该文件在NX执行，需要部署并更新O6进程；只重启VLA HTTP不会替换已有O6实例。此次未部署或重启，详见[持续核验与O6读取预算](多端联调_操控与抓取.md#cola-continuous-observation-20261110)。

离线示例来自操控侧 `contracts/o6_feedback_fault.example.json`，本仓库测试副本使用合成机器人身份，见 [O6 故障字段夹具](../tests/contracts/o6_feedback_fault.example.json)。首次记录冻结和进程退出持久化由 VLA／O6 负责，大脑不为旧任务合成记录；联调验证见[操控故障字段接入](多端联调_操控与抓取.md#brain-o6-feedback-fault)。

### O6 进程退出与只读取证（2026-11-06）

新 O6 记录增加 `process_identity`（PID、boot ID、start ticks）及 `can_at_binding/can_at_fault`，用于区分旧进程和 USB 重新枚举后的同名接口。致命 CAN 读取异常先封锁任务输出、保留首因与接收实例退出记录。2026-11-06 按用户授权，direct O6 对已匹配身份的 CAN_READ_FAILED（errno100／19）在原进程与独占锁内自动恢复；接收实例退出不再意味着管理进程一定退出，记录不能替代物理停止／保持证明。恢复不重播旧命令，也不修改旧任务失败或停止未知。

NX 的 `sonic_managed_o6/last_feedback_fault.json`、`feedback_faults/<fault_id>.json` 和 `last_receiver_exit.json`、`receiver_exits/<receiver_instance_id>.json` 保留最新及逐次记录。工作站沿现有 `g1_brain_vla_bridge/export_o6_diagnostics.sh` 导出，不启动采集、不打开 CAN、不访问相机。自动取证缓存及实机验证见[O6 USB 断连排查](多端联调_操控与抓取.md#o6-usb-forensics-20261106)。历史证据只能解释匹配的故障，不能冒充当前服务状态。

服务启动与执行准入分开：认证 `GET /v1/service/status` 只报告本地 HTTP／模型／任务及输出归属；统一启动器再检查相机与 O6。SONIC、身体状态、输入采纳及最新交接条件在动作阶段检查，服务启动不得生成 `startup_safety_attested=true`。面板使用相同规则，见[统一 VLA 服务](接口说明_控制面板.md#vla-unified-runtime)。

自动恢复先核验已归档首因、机器人、USB来源、CAN类型与当前ifindex，等待UP和设备身份连续稳定3秒，再读取当前双手位置建立保持；新receiver／epoch默认输出封锁，旧输入不能复用。轮询1秒、指数退避2–30秒、两次重建至少间隔10秒，300秒内最多3次自动重建；等待超过120秒或归属／标定不明、限速触发时进入blocked，要求人工检查。手动停止结束恢复循环；正常服务复用，不另起第二个O6。状态合同 `fq/o6-recovery/v1` 通过原运行状态的 `o6.recovery` 暴露，进程存活以PID／boot／start ticks核验，旧状态不能冒充当前恢复。

停止检查独立收集原控制器的身体证据和原O6实例的手部证据。缺失status或接收实例已换时，身体检查继续执行，手部保持仍为unknown并保留匹配的原故障；不会再由KeyError跳过后续检查。新服务ready不回填旧命令的hold_verified。

## 大脑统一授权对接（2026-10-23，接口离线串联通过）

> 当前日志与证据字段补充统一见上方交付映射；下节为历史 relay 接线。

> 历史 relay 接线，以下至“新动作接入”保留当时行为；当前 direct-v1 不执行这些交接。

本轮共同需求统一见[relay 控制权对接说明](多端联调_大脑任务与恢复.md#relay-history)，请与导航侧审阅同一份正文。此前详细控制协议草案已撤回；本轮优先复用现有业务接口、acquire／release／查询及可靠性逻辑。

两端映射反馈已汇入[最小对接表](多端联调_大脑任务与恢复.md#relay-history-收敛后的最小对接表)：保留客户端交接路径形式并更新大脑权威修订，采用操控侧建议的 stop 与 relay 管理入口；不新增整套 participant API。接口代码已实现，现场运行入口尚未切换。

操控侧负责改接大脑授权、拆开本次退出与导航恢复，以及原 relay 的受控切换与状态查询。连续开门／抓取的起始检查也需去掉“导航转发已经恢复”的依赖，保留真实到位和作业条件核验。第一版沿用每任务独立 worker 生命周期；身体和双手的必要退出／保持检查仍由执行侧落实。具体最小入口和字段由操控侧对照代码提出，与大脑确认后在此维护。

大脑与本地操控／relay 的实际代码隔离对接结果见[第一阶段记录](多端联调_大脑任务与恢复.md#brain-vla-peer-offline)：原连续 VLA 状态与 relay 新鲜流序列化问题已修复并完成 34 项复验。后续 NAV 查询已使用独立 NAV_CONTROL_TOKEN／私有凭据文件；最新三端实际 HTTP 完整 Runtime 串联通过，见[三端复验记录](多端联调_大脑任务与恢复.md#brain-three-peer-offline)。真实输出、模式和身体／双手保持仍待完成，真实执行继续阻断。

下文的 NAV 租约、restoring_navigation 和导航就绪终态条件描述迁移前实现，不表示新方案仍要求这些条件。现有真实执行 readiness 阻断不能因授权方迁移而解除。

## 新动作接入（2026-10-22）

Connection 已接入 `GET /v1/vla/capabilities`、原提交／查询／取消路径，以及原命令的 `/pause`、`/finish`。本次按同级 `sonic_wbc/g1_brain_vla_bridge` 的代码与《大脑调用接口》《导航交接接口》核对。新能力用于 `body_real` 通用身体任务，以及用户明确指定的真机接待分支；真机“开始接待”先导航、开门、原地抓可乐并核验，随后精调站位、关门并核验，再沿原中转路线运送到1号桌；仍不放置，具体配置见[大脑接待流程](接口说明_大脑%20Brain.md#81-流程与请求身份)。

| 能力 | 接入规则 |
| --- | --- |
| `open_fridge` | 登记作业路线与冰箱 ID 后可规划；完成要求门打开且稳定、双手脱离接触区 |
| `grasp_cola` | 本任务同一目标的开门步骤必须核验 PASS；抓取要求目标在手中且已离开冰箱接触区 |
| `close_fridge` | 接待v3在抓取核验后调用direct-v1生成轨迹执行器；独立中心、原Harness阈值及头图／双臂回位核验，见[当前关门接口](#close-fridge-generated) |
| 旧 `pick/place` | 当前native配置不支持；整份计划在首个导航派发前检查，不以改名替代 |
| 原后端 | 能力接口明确 404 时保留旧抓放兼容；网络错误、非法能力回包不当作旧后端 |

`configured=true` 只表示执行器已配置。2026-10-22 的旧接线中，动作仍检查 `real_action_enabled`、`navigation_handoff_ready`、`recovery_required=false` 和 `policy_state_uncertain=false`；VLA 自身继续核查导航原命令、硬件与租约。新任务中的导航也检查 VLA 已停、无活动命令、导航通路就绪及无恢复要求，避免失败／取消后直接导航。仅导航接待变体不依赖 VLA 能力发现；真机许可关闭保持原门控行为。

当前 source_gate 不采用上一段的 NAV lease、旧恢复／端口门禁或远端 readiness 前置。大脑使用已支持动作目录编排，VLA 内部检查具体执行条件；direct-v1 请求与收尾见文首。

<a id="door-auto-verification"></a>
<a id="开门前harness四向位置纠偏2026-11-11源码接入"></a>
### 开门前Harness四向位置纠偏（2026-11-12：VLA统一保护）

当前G1导航定位精调已按源端原样迁入并选择recorded_stride_v6，沿原Alignment／HarnessWorker的50Hz循环和唯一发布器执行，不新增大脑步骤、O6手指命令、运动线程或配置源。四向库仍由原fridge_harness.json的translation选择；直接复用迁入的源smpl_keyboard加载器，动作资产合同及原编码器／解码器绑定保持。v6的后退、左移相对v5更新，前进、右移相同；源实验／硬件未验收标记保留。2026-11-12本次只切换动作资产选择，播放代码与源加载器均未修改，见[v6迁移与验证](多端联调_操控与抓取.md#harness-stride-v6-migrated-20261112)。

2026-11-12按用户明确要求，当前真机NAV工厂固定创建NavigationSource(control_only=True)，移除Harness独立的8°倾角／75°/s偏航、静止速度／回稳门限、150ms身体反馈／index与tick间隔、0.5m／65°局部范围、方向净空、2cm无进展、2／3／5次纠偏及内部准备／恢复／总时长拒绝。此内部参数表示当前执行职责，不增加用户配置源；历史scene／校准入口维持原行为。

随后修正本机平移播放：删除motion_start后0.8秒自动收步及TranslationStep额外2秒等待。帧序列对齐源端WASD：先enter_start→loop_start，保持当前方向时重复loop_start→loop_end，释放后原样0.30秒／15帧float32 blend回stand；没有额外固定运动时长、最少循环数、裁剪或重新编码。原Alignment在平移期间复用同一executor读取NAV位置，仍需该方向就保持；位置进入原范围、应换方向或定位真正失效／过期时释放。重复轮询不等于定位失效：保留上一有效位置及其原始时间，不立即松键或刷新年龄；仍在原1秒预算内的新位置可因传输延迟早于按键起始时间。动作后到位确认继续要求动作后的新采样，不能混用。现场问题及修复见[平移定位时序](多端联调_操控与抓取.md#harness-translation-live-pose-20261112)。平移保持期间不因准备段的瞬时yaw误差反复松键，释放后仍按原标准调整最终朝向。释放后取新定位确认，不能把参考播放结束当作物理到位；不补发追赶帧。源端逐帧对照及验证范围见[v5接入](多端联调_操控与抓取.md#harness-stride-v5-aligned-20261112)。

2026-11-13按用户最新要求，当前开门／关门共用NAV Harness最终位置条件改为距各自目标中心≤0.10m，即hypot(x_m,y_m)≤0.10；朝向仍±5°。scene_policy.DEFAULTS.position_radius_m是当前圆形半径的唯一执行定义，平移释放、纠偏方向和到位／交接使用同一圆形判据，保留两份新定位。x=y=8cm虽各小于10cm，欧氏距离11.31cm仍不通过；6cm／8cm边界通过。位置误差以目标中心定义，纠偏方向转入当前身体轴；目标中心、v6原始播放、VLA／SONIC保护和期限保持。此前分别前后／左右±5cm的矩形标准已退出当前NAV路径；历史scene／校准入口不在本次变更范围。地图显示与离线验证见[10cm圆与地图](多端联调_操控与抓取.md#harness-radius-map-20261113)。

2026-11-13按用户要求，当前control_only精调路径去掉外层Alignment的2秒固定settling、HeadingSweep转向前／后各2秒固定等待，以及MicroProbe开始前1秒额外stand；开门／关门共用路径同时适用。源录制从第0帧开始的准备／转动／退出、0.6秒进入与0.5秒返回混合、初次身体准备、到位两份新定位及原保护不变；历史control_only=False校准路径保留原等待。没有借此新增稳定性校验、剪源帧或改变±5cm／±5°。实现和实际离线范围见[等待清理](多端联调_操控与抓取.md#harness-wait-cleanup-20261113)；源码已写回，服务未重启，不能将省掉固定等待等同已验收实机收敛速度。

translation.enabled=false保留原转向／前进能力边界，不增加缺失方向。源码已写回；新的任务worker会读取新版，正在运行HTTP中已导入的生命周期不会热更新，完整一致版本需按原VLA入口重载。本轮未重启服务或发动作。此前转向退出／回站回弹算法未在本次重写；去掉退出门限不代表该轨迹已修复，真实效果仍待验。测试及备份见[VLA统一保护接入](多端联调_操控与抓取.md#harness-vla-owned-20261112)；原迁入来源见[四向接入记录](多端联调_操控与抓取.md#fridge-four-direction-implemented-20261111)。

<a id="harness-remaining-checks-20261112"></a>
#### 现存附加检查与外层等待：完整清理前的核对

2026-11-12按用户追问沿实际G1 `control_only=True` 调用路径核查：此前“精调局部保护已移除”只适用于Alignment／InitialPose／平移和转向的对应分支，不能泛化为整个关门播放器已没有独立门限。**2026-11-13按用户要求已删除本机CloseClip及其7项关门局部硬停止和100帧进入前缀**；下表保留移除前的失败依据。末两行的共用转向小段退出仍在，本次不改开门／共用精调。

| 11月12日核对位置／条件 | 移除前行为／仍保留的转向行为 |
| --- | --- |
| CloseClip相对起点yaw变化>20° | 立即报错并中止关门 |
| CloseClip调用body_feedback，roll或pitch绝对值>8° | 立即报excess tilt并中止关门 |
| 同一公共校验，yaw角速度绝对值>75°/s | 立即报excess yaw angular rate并中止关门 |
| 同一公共校验，身体反馈年龄不在[0,150ms] | 立即报stale measured state并中止关门 |
| CloseClip连续tick间隔不在[0,100ms] | 立即报publisher tick gap并中止关门 |
| CloseClip反馈index回退 | 立即报feedback regressed并中止关门 |
| CloseClip反馈index超过150ms不推进 | 立即报feedback stopped advancing并中止关门 |
| 当前control_only转向MicroProbe，实测朝要求方向的变化<−3° | reason=opposite_direction，退出当前小段、收步后重新观察；不直接判整条任务失败 |
| 当前control_only转向小段运动时间≥20秒，第二段≥28秒 | reason=motion_timeout_target_not_reached，同样结束该小段，外层HeadingSweep仍可继续尝试 |

移除前关门的8°／75°/s／150ms由CloseClip在初始化和tick时默认调用 `body_feedback(feedback,now)` 继承；该调用没有传control_only=True，因而重新启用了精调已绕过的MicroProbe.validate_feedback局部门限。现在GeneratedCloseWorker不再调用这套反馈校验作为关门局部拦截。转向的反向／单段时长分支本身没有control_only条件；“无次数上限”不等于单段永远不收步。两条转向小段退出条件与到位即收步、原录制退出、固定站立等待属于不同机制，不能将退出小段写成整条任务失败。

此外仍有以下不同职责的检查，不统一称为重复：最终位置±5cm／yaw±5°及至少两份递增新定位属于到位标准；定位原1秒年龄、机器人／地图／目标／epoch与消息格式核对仍在；VLA自身的命令／代次、采纳、O6、模型租约、本体保护、取消和worker状态检查保持。VLA外层仍有NAV准备预检最多8秒、传感准备180秒、激活10秒、初始站姿等待20秒、精调等待370秒、恢复交接每次等待370秒、resume等待5秒等边界；不是Harness独立纠偏次数上限，也不能宣称整条流程只有一次370秒总预算。

动作准备仍核对静态资产：四向TranslationBank的manifest摘要；生成关门manifest、Token文件及encoder／decoder摘要；格式／有限值、64维、幅度≤1.25、50Hz、生成片段100—3001帧等合同。之前移除的是训练采集启动preflight的固定源码哈希，不代表这些动作资产校验也已删除。当前control_only已清除外层2秒／HeadingSweep前后2秒／MicroProbe前1秒重复等待；仍保留初始1秒混合＋1秒等待、转向原录制进入／退出与混合、四向松键0.30秒。历史校准路径的固定等待不变；关门100帧进入混合已于11月13日删除。固定等待不证明物理停稳，也不等于新增成功条件。

11月12日仅只读审计，7项CloseClip硬停止及3个转向小段边界场景用真实类和内存反馈复现，详见[原核查证据](多端联调_操控与抓取.md#harness-remaining-checks-audit-20261112)。11月13日根据用户明确要求实施关门局部清理，源码已改、服务未重启，实际范围见[清理与回归](多端联调_操控与抓取.md#close-fridge-direct-source-20261113)。

<a id="harness-current-thresholds"></a>
#### Harness 调整前阈值与失败出口（2026-11-12核对）

本表保留移除Harness独立保护前的排障依据；当前NAV精调的保留／移除范围以上节为准，下表不再作为新版仍生效的门限。2026-11-12按用户要求，VLA采纳回执年龄上限由原 `min(250ms, peer预算)` 改为1秒，源年龄、传输与缓存消耗仍从1秒内扣除；Worker沿同一截止时间检查。SONIC返回的 `current_motor_adoption=true`、控制器／接收实例、session、sequence、epoch、流启用及保护标志仍须符合当前事实，未修改peer自身200ms的采纳状态判定或断流机制。该1秒只调整VLA证据传递上限。

| 阶段／项目 | 当前值 | 未满足时的实际行为 |
| --- | --- | --- |
| 初始局部范围 | 每轴偏差绝对值≤0.5m、朝向差≤65° | 超出进入人工处理／失败；不继续局部迈步 |
| 最终位姿 | 目标朝向轴x∈[−20,+10]cm、y∈[−20,+20]cm、yaw±5° | 在局部范围内继续选转向或平移；不会要求坐标归零 |
| 采纳回执 | VLA年龄上限1秒；等待取得回执最多5秒 | Worker拒绝过期证据，当前仍直接封锁失败，没有自动刷新重试 |
| 初始站姿 | blend1秒、实测到达后连续稳定1秒、总准备8秒；64维token最大差≤1e−4 | 20秒外层等待内仍受8秒内部期限约束；不达标失败 |
| 稳定判据 | 所有关节速度绝对值≤0.15rad/s，基座角速度范数≤2°/s | 初始站姿／转向／平移内部有稳定等待；Alignment非probe路径先调用stationary硬检查，未稳会在等待逻辑前直接异常，仍为已知缺口 |
| 转向后回稳 | 连续稳定2秒 | 未稳重计；HeadingSweep总期限180秒；外围稳定检查缺口见上一行 |
| 转向次数 | 外层最多2次，每个HeadingSweep内最多5段 | 到预算且仍超差失败；单段运动通常20秒、第2段28秒，单段期限另加12秒 |
| 平移单步 | 运动参考最多0.8秒、回站blend0.3秒、稳定2秒；单步总期限25秒 | 超时失败；实际位移由新定位复测 |
| 平移偏航／进展 | 单步朝向漂移≤5°；最多3次；下一步前矩形外残差至少改善2cm | 漂移、次数或无进展会失败，不自动无限重复 |
| 身体反馈 | 源年龄≤150ms；index超过150ms不推进拒绝 | 当前直接异常；这是另一条独立时效条件，未随采纳回执改为1秒 |
| 身体姿态保护 | roll／pitch绝对值≤8°；偏航角速度绝对值≤75°/s | 单帧超限直接异常，无累计确认窗口；r1触发帧读数未保存，不能认定保护误报 |
| 发布节拍 | 原发布器50Hz；Token适配和平移每tick间隔10–60ms，部分上层检查允许到100ms | 超间隔失败，不补发追赶帧 |
| NAV观测 | 源年龄加请求耗时≤1秒；约200ms轮询；连续恢复等待8秒；HTTP超时配置0.8秒 | 可恢复缺测原地等待，超过8秒失败；地图／定位epoch、目标、机器人身份改变直接拒绝 |
| 读定位时身体变化 | HTTP前后IMU朝向变化≤2°，且前后身体静止 | 未通过作为可重试定位不可用，进入上述8秒恢复等待 |
| 到位确认 | 两份递增新定位；相邻≤1秒、位置变化≤6cm、角度变化≤3° | 继续取新定位；交接前当前IMU相对定位观测变化≤2° |
| 方向净空 | 实际运动方向距离≥0.5m；源年龄≤0.5秒；约100ms轮询，HTTP超时0.4秒 | 暂时未知／受阻先等待；运动中结束当前脉冲并受控退出再观察，身份／方向绑定改变失败 |
| 净空与身体一致 | 关联腰部关节变化和整体旋转均≤3°，点云至少1000个有效候选；远距离通道需至少30点 | 当前证据失效，重新观察；不将前方净空用于侧向／后退 |
| 综合传感／RTC重置 | Harness最新综合相机／身体／O6观测≤1秒；每次reset及新观测最多8秒 | 过期／超时失败；相机自身仍有500ms新鲜度、20FPS／15秒准备窗口 |
| 全程预算 | Alignment360秒，生命周期外层等待370秒 | 超时失败；没有无限自动纠偏 |

手部raw目标在准备及精调期间要求不变；O6故障、控制源／实例／epoch改变、取消与原本体保护继续按执行端实际状态处理。此表属于用户审阅阶段的旧检查；随后移除Harness独立保护，新策略以上节为准，原VLA核验保持。相关失败、离线及应用范围见[本轮排障](多端联调_操控与抓取.md#harness-initial-pose-adoption-20261112)。

<a id="harness-initialization-error-triggers"></a>
#### harness_initial_pose 与 fridge_alignment 的调整前触发条件

2026-11-12按移除Harness独立保护前的G1导航定位＋四向Harness执行路径核对，供历史失败排查；新版范围以上节为准。它们是DirectLifecycle `_wait_boundary`的阶段标签，不是两个独立根因码。前者发送prepare_pose后等待同generation的body_ready（外层20秒），后者发送align后等待同generation的alignment_ready（外层370秒）。等待中的检查或worker异常可被包装为 `VLA_INITIALIZATION_FAILED`；纯外层期限耗尽则为 `VLA_INITIALIZATION_TIMEOUT`。显式TASK_CANCELLED沿原取消码返回，不应计作这两个初始化失败。prepare_pose／align之前的启动、相机准备、模型准入和导航preflight失败另有阶段，不混进本表。

**两阶段共有的直接失败条件：**

| 检查位置 | 触发条件 |
| --- | --- |
| 模型租约 | 当前binding不等于原租约、租约已fence、compute缺失或进程不存活 |
| 原任务进程归属 | 原tmux会话不存在／查询失败，或其owner与当前任务session_token不一致 |
| Worker状态 | worker.json身份或token不符；状态单调时间在未来或超过1秒未更新；worker已fenced／停止 |
| SONIC／O6实例 | 当前controller_instance_id或O6 receiver_instance_id与任务绑定不一致 |
| 源控制合同 | source_gate或stream版本不符；receiver／revision缺失或变化；source_gate_peer与controller身份不符；source_gate_error非空 |
| 源控制状态 | effective_source不是vla、pending_requests非0、enabled不为true、safety_blocked不为false、remote_hold不为false、stream_epoch改变 |
| O6执行 | status缺失、error非空或receiver_output_fenced不为false；保留匹配原命令／实例的反馈首因，不用后续通用错误覆盖它 |
| Worker命令 | token不符、generation不是整数／倒退、同generation内容冲突、未知action、prepare_pose重复、align时不是body_ready或已经resume |
| 综合传感 | 相机／身体／O6综合观测缺失或原观察年龄不在[0,1]秒；异步observe／reset任务抛异常；准备或精调期间手部raw目标变化 |
| Worker保护与输出 | thermal_blocked触发SAFETY_OVERRIDE；未配置policy_work；匹配的alignment_cancel文件；当前publisher会话／序号缺失或非法；发包／状态持久化异常 |
| 文件／通信异常 | 状态／命令文件不是同UID私有普通文件、JSON损坏、文件读写／fsync失败；原SSH／查询／进程调用异常。它们也能沿异常出口结束等待，不是数值阈值 |

**prepare_pose和align都使用的采纳证据检查：任一项不满足，统一抛 `motor-stage preparation adoption confirmation required`。**

1. contract_version必须为g1/stream-adoption/v1；receiver_instance_id和source_revision必须等于bootstrap绑定。
2. enabled、current_motor_adoption必须明确true；safety_blocked、remote_hold必须明确false。
3. last_motor_published必须是本任务input_session_id，source=vla、session_metadata_valid=true、protocol=4；帧source_revision／stream_epoch必须等于证据同名值。
4. valid_until_monotonic_s必须是有限数值，Worker当前单调时间不得晚于它。当前VLA总年龄预算为1秒，已消耗的源年龄／传输／缓存时间仍扣除。
5. 帧input_sequence与Worker activation／preparation首末序号均须为整数，满足activation_sequence < preparation_first_sequence ≤ motor帧序号 ≤ preparation_last_sequence。

生命周期在取得证据时另外核对last_received／last_decoded／last_token_adopted／last_motor_published四级身份、epoch、协议及NX时序字段；缺失或不匹配不会凭mode、PUB发送成功或旧回执通过。取得证据最多等5秒，未取得为独立SONIC_STREAM_ADOPTION_TIMEOUT。Worker只要在上面任一项失败，当前实现立即封锁并退出，不自行刷新再试。

**harness_initial_pose额外条件：**

| 触发条件 | 原检查／含义 |
| --- | --- |
| 输入token非法 | start／target／反馈token必须64维、全有限、绝对值≤1.25 |
| 初始参考未采纳 | 反馈token与保留start的最大差>1e−4 |
| 初始身体未稳 | 创建InitialPose时任一关节速度>0.15rad/s，或基座角速度范数>2°/s，直接报Body is not stationary |
| 身体反馈非法／过期 | 缺测；年龄不在[0,150]ms；body_q／body_dq不是29维有限数；gyro不是3维有限数；IMU四元数不是4维有限数或模长偏离1超过0.02 |
| 姿态保护 | 任一roll／pitch绝对值>8°，或gyro Z绝对值>75°/s，单帧即拒绝 |
| 发布／反馈连续性 | 准备tick倒退或间隔>100ms；index非法／倒退；index连续>150ms不推进 |
| 未完成准备 | 1秒blend后，反馈token与target差≤1e−4且连续稳定1秒；8秒内未满足报initial pose feedback/settling timed out |
| 已就绪后又变化 | InitialPose.ready后，反馈token超1e−4或身体不再满足静止判据，报initial pose changed before alignment；align入口也再次调用此检查 |
| 重置未完成 | RTC重置及其后新综合观测超过8秒未完成，或后台重置／观察函数抛异常 |
| 外层等待 | 同generation的body_ready在20秒内未出现，报初始化TIMEOUT；不是FAILED |

此阶段后的原生safe-token／反馈token差>1e−4是独立HARNESS_START_POSE_MISMATCH（harness_initial_pose_confirmation），不能混称本阶段超时。

**fridge_alignment额外条件：**

| 触发条件 | 原检查／含义 |
| --- | --- |
| align入口未满足 | 不是body_ready、已resume、采纳证据不合格；InitialPose完成后又变动；Align.start时不静止、重复start／已cancel；保留token或raw双手目标形状／数值不合法 |
| 身体反馈／姿态 | 继续执行上述150ms、29维、四元数、8°／75°/s检查；Alignment的index非法／倒退或150ms不推进也失败 |
| 静止硬检查 | 非probe路径进入settling／读新定位前要求静止，任一关节速度>0.15rad/s或角速度范数>2°/s立即失败；这会早于原等待逻辑，仍为已知缺口 |
| 定位合同／身份改变 | schema、机器人、target／command、只读／motion_authorized声明不符；预期map／frame／epoch不符或已绑定值变化；目标pose改变；owner消费端发现绑定不全／变化、非法字段或sequence倒退 |
| 定位暂不可用 | NAV未ready／未批准、failed_gates非空、数据字段／时效不足、请求失败、source不递增、读前后身体未稳或朝向变化>2°等可重试情况会等；连续8秒恢复不成失败。不可重试回包立即失败 |
| 初始局部区域外 | 四向路径任一轴绝对偏差>0.5m或yaw绝对误差>65°，直接要求人工；没有在该区域外一直迈步的路径 |
| 转向构造／执行 | target非有限数、容差不在[1,10]°、内部目标相对当前朝向>70°；tick倒退／>100ms，底层Token节拍不在10–60ms；状态index倒退；累计转角超min(80°,剩余目标误差+10°)；单段超过运动期限+12秒；HeadingSweep总长>180秒；5段后仍超差，或外层第3次转向 |
| 平移构造／执行 | 当前stand与库stand不一致（allclose atol=1e−6）；反馈检查失败；tick不在10–60ms、index倒退／150ms不推进；单步朝向漂移>5°、时长>25秒；第4次平移或下一步前矩形外误差改善<2cm |
| 动作未完成 | probe结束但target_reached=false且不是净空控制的受控退出；转向反方向超过3°会先触发退出，未达到目标仍可能在此失败 |
| 净空不可恢复 | 未启用实时方向净空却要求平移；方向／机器人／map／epoch／frame／外参绑定冲突、字段非法、sequence倒退、reader异常或回包明确不可重试；运动中受控退出后仍以原fatal原因失败 |
| 净空暂不可用 | 距离<0.5m、未知、年龄>0.5秒、缺新点云、相对采样身体旋转／腰角变化>3°等会等待或结束当前脉冲后再观察；继续受定位恢复8秒及总预算约束，不是全部即时失败 |
| 到位／交接未成立 | 需要位姿在x[−20,+10]cm、y±20cm、yaw±5°，两份递增新定位相邻≤1秒／变化≤6cm和3°，当前IMU相对观测变化≤2°；不满足通常继续观察或恢复，持续不满足最终预算失败，不是第一次超±5°就失败 |
| 总预算／其他异常 | Alignment>360秒直接失败；外层370秒未得到同generation的alignment_ready为TIMEOUT；动作库／尾段缺失、运行时数值／文件／发布异常也会进入worker封锁出口 |

当前导航定位路径不运行历史scene分支的视觉重定位／跨boot静态窗口、7次scene观测预算；未把这些旁路条件列为本轮触发项。电机90°C同步核验在DirectLifecycle.phase=running时才执行，也不是这两个初始化阶段的当前触发条件。原本体保护仍可通过thermal_blocked或流状态触发公共失败。

排障优先读原worker.error及worker_phase，再读顶层execution_boundary.cause：worker先异常→发STOP→SONIC enabled／remote_hold／epoch改变时，后续SONIC_STREAM_CONTROL_LOST可能成为页面cause，不能倒置为最早原因。该错误表达缺口尚未修改；原始首因仍在worker文件及日志。触发条件列表是代码核查，不代表每个条件都在真机触发过。

<a id="close-fridge-generated"></a>
### 抓取后精调站位与源录制回放关冰箱（2026-11-13）

当前真机接待SOP v3在抓取及原O6＋VLM核验之后，用close_fridge完成关门站位精调、源端录制回放，再沿原O6＋VLM／finish核验推进运送。六字段请求和原command_id不变；失败、取消或UNKNOWN不自动重做或运送。旧v1/v2任务保存原快照；独立通用Planner和历史unified分支未接入此回放。

本机VLA原config/g1_tasks/fridge_harness.json的close_fridge.alignment_center_pose选择关门中心，close_fridge.replay.dataset／episode选择data/g1_2_close_fridge_o6_dual_threecam_v1的episode 0；不再选择生成manifest。关门前沿用同一Harness，距中心≤10cm、yaw±5°、v6和两份新定位，详见[共用精调](#开门前harness四向位置纠偏2026-11-12vla统一保护)。

RecordedCloseWorker复用原fridge启动器、相机／reset准备、任务生命周期和唯一50Hz发布器，直接调用迁入源g1_close_fridge_replay的原load_episode／Replay函数。663帧原action.motion_token按原timestamp保持输出，总时长22.0666676秒；保留源3秒smoothstep进入录制首帧和结束后保持末帧。按时间取录制样本，不重新求解SMPL或编码、不按每tick一行改变原30Hz时间轴，不输出学习模型预测。发布包只含身体Token，录制手指目标不发送，抓取后的当前O6目标保持。

源回放DONE后将原ArmReturnGate回位事件供现有头图VLM和finish消费；原gate在播放期间继续采样，不新增关门判断器或动作包络。recorded_replay.phase／play_time_s／clip_complete只说明回放进度，physical_effect_verified保持false，不能代替真实关门成功。无额外20°、倾角、局部tick或反馈中止阈值；原VLA／SONIC、身份、取消、O6及结果核验保持。源函数对数据格式的原检查保留，不新增运行时哈希门槛。

自动证据合同仍为fq/vla-close-fridge-head-arm-evidence/v1：ego_view给原VLM宽松判断门基本关闭，看不清保持UNKNOWN；复用原ArmReturnGate离开／双臂回位／0.8秒保持。帧收据、command／epoch、置信度、时效与原停止收尾不变。人工finish显式确认door_closed、arms_returned、hands_clear_of_door；成功后沿原持物登记运送，不新增抓取事实。

正式close_fridge继续要求本任务抓取及持物登记。旧生成关门执行器和本机关门生成入口已移除，原生成资产只保留为失败诊断证据。源端独立回放在迁移前已存在；此前“源端仅有生成框架”的来源说明不完整，已在[来源与替换验证](多端联调_操控与抓取.md#close-fridge-recorded-source-20261113)纠正。源码及离线通过不代表运行实例已加载或关门转身根因已实机消除；本次未重启服务、未触发机器人动作。

<a id="close-fridge-test"></a>
### 单独测试关冰箱（2026-11-12）

任务网页新增“单独测试关冰箱”按钮，按登记fridge.close_test v1只执行一个close_fridge_test操控命令和其核验，不执行接待、导航、开门、抓取或运送。该测试身份直接复用close_fridge的同一DirectLifecycle／Harness／RecordedCloseWorker、原关门中心和原对齐阈值、原录制Token及单头图／ArmReturnGate核验；精调完成后直接关门，没有额外精调确认阶段。

请求仍是原六字段，operation=close_fridge_test，object_id／target_area继承当前Skill的关门步骤；没有导航命令，不构造navigation_proof。测试前网页按原现场确认字段提交worksite_confirmed=true，只接受source_gate真机环境和已登记的独立测试入口。该显式测试不要求先抓取，不写抓取／持物成功，不修改正式close_fridge的同任务抓取前置。相机／O6／原生采纳、定位与净空、停止及反馈保护保持；取消和网络不确定沿原command_id查询，不重复发送动作。测试成功不等于接待完成。应用和离线范围见[按钮接入验证](多端联调_操控与抓取.md#close-fridge-button-20261112)。

### 开门自动视觉确认与准备状态（2026-11-05）

**本机应用状态：**大脑、面板和 VLA HTTP 已按用户授权重载；VLA `preparation.enabled=true`、`visual_evidence.enabled=true`，Brain 新任务 `door_verification.enabled=true/mode=auto`。VLM 改为已授权的云端 `qwen3-vl-plus`（DashScope 国际站），本地 9B VLM 服务已停止。完成凭据通过私有 `_FILE` 引用，不复制进账本。旧任务不会追溯启用自动核验。当前相机/O6停止、SONIC未运行，故操作级 `warm_ready=false`，没有发布实机动作；这不是整体就绪或10秒验收。

开门自动核验仅针对 `open_fridge`。大脑通过既有 RuntimeOwner 空闲调度与 I/O 池观察原执行命令，不新建任务循环；抓取自动核验按下节复用同一观察器和原finish流程。VLA负责动作、相机、双臂测量信号和原命令收尾；Brain产生外部视觉证据，Runtime／Verifier核验正式终态和持物事实。

**2026-11-09 源码修订；15:50仅大脑已重载，VLA由用户另行启动，未进行真机动作验收。** 新任务绑定 `reception_real.door_verification` 与 `evidence_contract=fq/vla-open-fridge-head-arm-evidence/v2`。`enabled=false` 为公开默认，`shadow` 只记录观察，`auto` 经原 verifier 凭据提交 finish。VLM 端点复用 `reception_real.verification.vlm`。旧任务没有该合同快照时不按新规则自动完成，保留原人工确认／原始失败记录；服务更新不会重写历史任务。

当前自动核验由两项不同来源的事实组成：

| 完成条件 | 证据来源与判据 |
| --- | --- |
| 冰箱门打开到可以取内部可乐的程度 | 只向 VLM 发送一张当前任务绑定的头图；要求 `door_open=true/door_closed=false` 且报告分数≥0.9。删除连续画面保持打开、像素稳定筛选及双腕图要求，模型不判断手部接触 |
| 双臂完成回位并保持 | VLA 原 HarnessWorker 在现有控制tick内复用迁入的 `_source_auto_return.ArmReturnGate` 与 `auto_return_config.json`。双臂先离开收尾姿态区域达到 departure 条件，再同时回到配置姿态容差内，实测关节速度≤0.35rad/s并持续0.8秒；配置使用双肩／肘共8个关节，不是任意姿态静止或手指保持即可通过 |

上述双臂条件沿迁移源语义作为交接代理信号，字段名为 `arms_returned`，不冒充几何接触检测。离开判据为任一选定关节偏离中心达到原departure_tolerance×1.5并保持0.3秒；反馈年龄上限0.5秒。初始就处于收尾姿态不触发；重复反馈不累计时间，未启用／暂停／封锁及缺失、非法、过期、倒序或断档反馈按原gate复位。完成事件一经触发，在本任务内按源端语义锁存，后续姿态／速度波动不额外清除，也不再要求先重新离开再回来；源gate复位后重新完成才产生新的event_id。2026-11-09从源服务器实读核对后已移除本机额外的逐帧姿态／速度复位，参数未改变；来源哈希与复验见[本轮对齐](多端联调_操控与抓取.md#vla-door-visual-handoff-20261109)。VLA只上报该信号，下一动作仍由Brain Runtime派发，不能复用源端直接切换抓取的动作调用。

绑定取图沿用 `GET /v1/camera/frames/latest?camera=ego_view&task_id=...&command_id=...`，图片schema仍为 `fq/vla-camera-frame/v2`。响应新增 `arm_return`：`fq/vla-arm-return/v1`、valid、event_id、return_dwell_s、配置SHA256及测量状态。valid表达原任务已完成回位判定且执行／反馈仍有效，不声明当前每一帧仍满足收尾姿态或手门几何关系。服务读取匹配原task/command/worker的状态，检查运行阶段和原反馈年龄；相机、任务、服务、控制器与执行epoch仍由 `task_binding`、`frame_receipt_id/binding_proof` 绑定。VLA在私有帧收据中同时冻结arm_return；Brain不能自行签发或把旧任务／旧事件改成当前通过。未具备有效回位信号时不调用VLM，记录 `waiting_for_measured_arm_return`，不追加腕图或产生动作。

原 `/finish` 路径不变，新证据 `source=visual_verifier`、`visual_contract=fq/vla-open-fridge-head-arm-evidence/v2`，包含单张头图收据、原执行binding、`door_open/door_closed`、`arms_returned=true`、原 `arm_return` 和verifier版本；不填写虚假的 `door_stable_open/hands_clear_of_door`。`observed_at` 和窗口起止均为支持头图的源采集时间。VLA核验图像收据及其冻结的回位证据，首次受理finish前再核对当前事件仍有效；受理后的内部暂停仍核验原观察，不因暂停清零代理而撤销原证据。执行／停止失败、取消和旧实例结果依然阻止推进。原v1外部／人工证据按历史合同保留兼容，新auto不再使用v1完成条件。

VLM返回后只复核头图执行身份、源时间继续前进及VLA回位event仍有效，不进行像素相同或门连续稳定检查。2026-11-10取消自动核验的总观察时长及累计模型调用上限：从开门原命令进入执行就绪开始持续检查，开门核验并正式收尾后沿同一Runtime转入抓取观察，直到抓取核验及正式完成；不因已过120秒或累计10次调用停止。旧配置／任务快照中的 `max_observation_sec/max_vlm_calls` 不再作为停止条件，累计调用数仍保留为诊断计数。暂停、取消、原命令失败／终止、finish在途和任务身份变化仍优先，不对已失败原命令继续提交完成或自动重试动作。

单次I/O仍有界：默认采样0.5秒、重试间隔3秒、分数0.9、单模型3秒、单轮总等待 `vlm_timeout_sec+2`、证据6秒；本机配置单模型5秒。某轮超时／不确定不宣布成功，下轮继续观察；原单飞I/O池避免请求重叠。旧 `stable_span_sec/max_pixel_delta` 快照字段继续忽略。原JPEG与小结果仍保存于 `door_evidence/`；第二张头图只作身份／回位复查，不作为连续开门语义证明。

视觉和双臂代理通过后只产生原命令finish意图。必须等VLA内部暂停／收尾、原命令正式成功并经Runtime Verifier通过，才按SOP下发独立 `grasp_cola`；收尾失败不抓取。丢失finish回包只查原命令，不重复POST。2026-11-05—06版本曾用三张头部时序＋双腕联合VLM核验、要求稳定打开与双手离门；该条件导致11-09现场开门后仍unknown，原图、错误与逐次结果保存在[本轮排障](多端联调_操控与抓取.md#vla-door-visual-handoff-20261109)。这段历史不作为新合同的生效规则。

VLA 提前准备状态通过 `fq/vla-preparation/v1` 报告，单列开门模型 `state/warm_ready`，不把 HTTP 在线当模型已加载。纯计算准备不得创建动作发布端或接管机器人；任务仍执行最新观测、安全与交接检查。启用提前准备后，开门模型未就绪返回 `503 VLA_MODEL_NOT_WARM`；计算预热已完成但相机连续检查未通过时返回 `503 VLA_CAMERA_NOT_READY`；相机完整检查缓存失效返回 `VLA_CAMERA_CACHE_INVALID`，准备管理器停止中可返回 `VLA_PREPARATION_STOPPING`。只有原回包明确包含 `action_started=false` 与 `action_state_uncertain=false` 且身份／受理状态无矛盾，才可判定本命令未执行；大脑识别上述准备错误码，不自动重发，不把一般503或超时当作未执行。开门模型已准备后的10秒有效动作目标需真机计时，不能用离线假时钟声称达标。2026-11-10已有日志测得本轮成功开门受理到首策略动作发布13.494秒，抓取37.437秒；该开门样本未达到10秒目标，指标不等同于视频动作起点，冷启动和重复准备的拆解见[启动时延分析](多端联调_操控与抓取.md#vla-action-start-latency-20261110)。准备功能开关、模型资源管理和VLA侧实施证据以操控仓库当前交付为准；源码已在上述新实例应用；运行依赖和现场效果仍须独立核验。

<a id="抓取可乐自动核验2026-11-09-源码待现场复验"></a>
### 抓取可乐自动核验（2026-11-10 双腕均通过＋源端双臂回位）

新任务复用 `reception_real.door_verification` 配置和原观察器／finish流程，绑定 `grasp_evidence_contract=fq/vla-grasp-cola-wrist-return-evidence/v2`。业务完成判据仅两项：

1. 一次联合读取原抓取命令的左右腕图片，VLM分别返回 `left_grasped/right_grasped`；各侧图中近处能明确辨认可乐即判该侧true，两个字段都严格为true才通过。2026-11-10按现场要求放宽为可见性核验：不要求看清手指、夹持关系或接触点；罐体占满画面、只见局部或挡住手部，只要能辨认为可乐即可。一侧成功不能代表另一侧；明确没有近处可乐为false，无法辨认是否为可乐才为null，远处背景有罐不算。字段名及object_grasped合并结果沿用原合同，图片结论不单独宣称测得夹持或接触；仍须同时通过下述双臂条件。
2. VLA抓取worker直接调用与开门相同的 `_source_auto_return.ArmReturnGate`，沿同一源配置判断先离开收尾姿态，再双臂同时回到指定中心／容差内并低速保持0.8秒。包含开门的离开累计和回位累计，不是任意当前位置静止；初始静止、伸手后悬停、仅一臂回位都不满足。原类与配置不修改、不复制算法条件；完成事件按源端锁存，暂停／保护和无效反馈仍按源gate复位，不能加入额外姿态波动reset。

不新增退后距离、撤离区域、门状态或额外手指闭合阈值，亦不填 `clear_of_fridge=true`。步骤名仍描述VLA执行的“抓取可乐并后退撤离”；页面只显示该一个抓取步骤，内部VERIFYING_COLA_GRASP、原命令身份、正式收尾与任务抓取结果登记保留；当前v2不增加O6持物复核。新的回位标准使用源配置的同一姿态中心，现场是否满足须由新一轮真实任务验证，不用任意停住替代回位。

绑定相机仍为frame/v2，左右腕均携带 `arm_return=fq/vla-arm-return/v1`。两图必须属于同一task/command/epoch/controller及同一回位event，图像时序／同步／新鲜度、VLM分数、单次请求预算与调用间隔沿原框架；与开门一样不设累计时长或次数上限。模型提示版本为 `fq/brain-grasp-prompt/both-wrists-near-cola-v3`，结果schema为 `fq/brain-grasp-verification/v2`。支持证据合同为上述grasp v2，包含 `object_grasped/left_grasped/right_grasped/arms_returned=true`、请求物体holding、原arm_return、双腕收据和原采集时间；不使用arm_hold／arms_held来代替回位。VLA核验私有收据中冻结的回位信号，首次受理finish前复核当前原事件仍有效；受理后内部暂停继续核验原观察，不伪造暂停后的新观察。

只对绑定新合同的新任务自动应用。11-09的 `fq/vla-grasp-cola-wrist-arm-evidence/v1` 使用单手总体持物和原位保持，属于旧规则；历史结果仍可查询和按原合同解释，不补成新v2成功，也不续用其自动观察。新Brain读取旧任务时不按新条件改写原账本，旧步骤展示的合并形式保留。旧外部／人工合同兼容保留，但新auto不发送旧v1完成证据；缺少新回位信号时失败关闭。

通过后仍提交原命令一次finish。2026-11-10清除当前direct双腕／双臂v2的旧O6否决：pause只等待worker暂停确认，不要求O6目标／反馈代次／写入计数不变；stop不再强制attest_holding或holding_registered。只确认策略停止、发布停止和动作端口释放后，由Runtime Verifier核验原效果证据并切NAV。导航不再额外索取该路径的O6签名持物证明。发布未停／端口未释放／取消／原效果未通过仍不能推进；当前执行故障和新任务O6准入仍由VLA处理。旧人工／旧合同保持兼容，不篡改历史失败。图片与小结果仍保存在同一 `door_evidence/`，状态仍记原attempt.grasp_observation；没有新增控制循环、任务账本或配置源。

2026-11-10先完成双腕均通过和源端双臂回位接线，来源、原失败与复验见[抓取误判排障](多端联调_操控与抓取.md#cola-false-finish-20261110)。同日后续仅放宽大脑图片提示词，VLA回位及收尾代码不变；67项定向离线测试通过，保存图片的真实VLM回放见[腕部近景核验](多端联调_操控与抓取.md#cola-wrist-visibility-20261110)。此次未重载服务、未修改原任务或发送动作；大脑重启后读取新提示词，VLA无需为这次提示词修改重启。

### 作业点与规划配置

真机“开始接待”的配置在所选 Skill 的 `real_workflow`，直接引用其 `points.table2`；用户已确认这就是冰箱前真机站位，名字与 ID 的沿用不代表另一个物理点。本分支两动作共享原导航证明，中间不导航，坐标只维护在 Skill 一处。

下面的 `reception_real.native_vla` 是通用身体任务的独立动作目录，不是本次接待的必填项；公开模板默认 `points: {}`、`operations: {}`。未登记的通用动作仍拒绝。地址由现有网络／环境配置解析。

- `points.<点键>`：包含 `target_id`、`route_phase`、正整数 `leg_index`、三个有限数的 `goal_xyt`、`motion_mode`（`forward_path` 或 `lateral_path_aligned`）；发出 NAV 请求时固定 `frame_id=map`、要求最终朝向。
- `operations.open_fridge` / `operations.grasp_cola`：包含 `object_id`、按顺序引用点键的非空 `route`、正数 `timeout_sec`。目标与物体 ID 使用 1–128 位 ASCII 机器标识，首字符为字母或数字，其余允许 `_.:-`。
- 通用动作目录中的每个动作执行自己的登记导航路线；抓取前重新取得该路线的真实 NAV 命令证明。两动作路线终点的 `target_id` 必须相同，各自起始位姿仍须适合对应示教。此配置不替 NAV 登记点位。
- 能力、配置和编译步骤绑定进任务快照，恢复旧任务不重读新配置。人工跳过开门不生成开门PASS，但按本任务人工认可条件满足可进入后续抓取。

登记后的动作绑定形式为 `open_fridge: {object_id: fridge_1, route: [fridge_open_start], timeout_sec: 600}`、`grasp_cola: {object_id: cola_can_1, route: [fridge_grasp_start], timeout_sec: 600}`；这些仅示意 ID 与字段，两个起点的导航参数须现场填写。普通“取冰箱里的可乐”可规划必要的开门＋抓取；当前不支持混入额外运送／放置要求，纯导航继续沿原登记路线规划。

### 暂停、效果确认与大脑继续

2026-11-10 source_gate人工“继续”在原VLA明确失败／取消后，去掉对旧O6完整退出回执的重试前置，只核对命令生命周期与当前策略／发布通路互斥；新的开门／抓取请求能否执行，包括O6恢复是否满足，统一由VLA在提交时裁决。旧失败不改成功，新的受理／拒绝分别记录，不降低自动成功核验。完整规则在[大脑重试边界](接口说明_大脑%20Brain.md#91-同一-runtime-的继续与人工重试)。

2026-10-29 当前 source_gate 对接 `20261029-direct-running-finish-v1`；大脑／网页已于 2026-10-29 按用户要求重载，并核对实际页面；本轮未操作 VLA，操控侧须加载匹配版本，不能将大脑应用当作 VLA 已应用。

1. VLA running 后，大脑 waiting_human 只等待完成确认，不主动暂停模型。开门、抓取及模型后退继续由 VLA 执行。
2. 任务网页移除VLA顶部专属提示、持物快照、策略暂停／原命令查询和完成确认区；与导航共用进度、暂停／继续／跳过／取消及失败卡片，VLA错误仍先回Brain原账本。自动开门／抓取按各自视觉和双臂证据完成；人工“跳过继续”直接认可当前条件，按本任务假设进入下一步，不另弹一套VLA完成确认。操控转导航只核对当前无活动VLA策略和身体发布通路已释放，不因旧O6退出／保持未知、O6或身体实例更换而阻塞；自动完成及新VLA动作的原O6核验不变。原pause/query/finish API与核验方凭据继续保留，供自动核验和外部授权调用；网页展示不决定任务状态。
3. 授权核验方或自动核验调用 `POST /api/task_vla/finish`，body 为原 task_id、command_id、evidence 与 `auto_advance=true`；Authorization 原样按请求转发给 VLA。evidence 的 source=operator_observation、reference 为该点击唯一引用、observed_at 为实际点击时间，程序不改成暂停后的时间。该标志只用于大脑推进，不转给 VLA。
4. VLA 受理 finish 后内部暂停并收尾。大脑保存第一次确认意图，只通过原 GET 轮询终态；202、completion.accepted／pausing／paused 都不当成成功。完成回执的 completion.evidence 须匹配原确认；VLA 自行失败、取消、重启 interrupted、超时、身份不符或证据不完整都不推进。
5. 成功后仍经原 Verifier 核对效果、身份、时间及停止事实，再按原业务顺序推进。开门后自动提交抓取，保持 VLA 选通；抓取后另查当前持物，选通 NAV 后下发目标。无需额外点击“继续”。不新增后退命令，也不改 STOP 策略或 NAV Planner 恢复。

原 `/api/task_vla/pause` 和 `/query` 保留。未带 auto_advance 的调用兼容旧人工继续流程；旧 unified／非 direct 后端不因此接受运行中 finish。普通状态 GET 不驱动任务。已明确授权自动推进的确认可在大脑重启后恢复查询，但不重发 finish；暂停、取消或任务变化会阻止迟到结果推进。

首次确认与重发意图保存在原 attempt.effect_controls.finish，不另建业务账本。重复相同确认只查询，不重复 POST；不同证据拒绝覆盖。明确参数／凭据拒绝可重新真实确认，COMPLETION_CONFLICT 与网络不确定保留原意图。客户端不能只因 HTTP 错误就换 reference／observed_at 补发；GET 原任务状态能看到 finish_intent 与 completion。凭据只在当前 HTTP 请求使用，不落盘。自动查询超时后保留原请求进入异常处理，不自动重新执行动作。

效果证据字段：

| 字段 | 要求 |
| --- | --- |
| `source` | `operator_observation` 或 `visual_verifier` |
| `reference` | 可追溯观察记录／图像记录 ID，不能编造 |
| `observed_at` | 带时区，running 确认处于本次 execution_ready_at 之后，原暂停流程处于 verification_ready_at 之后；不晚于当前时间；终态证据时间不得晚于 `completed_at` |
| 开门 | `door_open=true`、`door_stable_open=true`、`hands_clear_of_door=true` |
| 抓可乐 | `object_grasped=true`、`holding` 等于请求物体 ID、`clear_of_fridge=true` |

终态还必须满足原任务／命令／operation／目标／物体身份、完成时间、`success=true`；旧后端另要求 `policy_stopped=true`、`navigation_port_ready=true`，direct-v1 则使用文首分别列明的策略／发布／身体／双手收尾字段；`effect_verified_by/evidence_level` 必须与原证据来源一致，`autonomous_effect_verification=false`。开门自动证据使用上节的新合同；新grasp合同按上节双腕视觉＋双臂保持自动完成，其余范围仍由现场人员确认，Planner不能代写持物证据。2026-11-10起人工跳过抓取建立本任务人工持物假设并放行后续运送，不冒充自动holding证明；本轮实例与当前恢复边界见[抓取后跳过排障](多端联调_操控与抓取.md#cola-human-finish-skip-20261109)。

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

## 服务启动与失联相机启动器恢复（2026-11-10）

VLA的start沿原 `run_agent_vla_runtime.sh start → runtime_lifecycle.Lifecycle` 组织HTTP、相机和O6，不是业务任务启动。健康组件复用，明确缺失的组件调用原入口；必须在原任务／worker／输出归属核清且满足原启动确认后才允许修复依赖，活动或未知任务不动组件。最终仍需三路新帧、模型准备及O6全部核验，HTTP在线或创建进程不单独表示start成功。

同名 `vla_cameras_<profile>` 存在但相机端口关闭时，启动器检查以下恢复依据：

- 只有一个pane，owner标记属于当前profile；原启动命令是本工作区原07脚本，命令携带的NX主机／用户／端口、网络配置、ControlPath及三路相机序列号／启用状态与当前选择一致，不能只凭会话名认领。
- 启动预算未满时，在剩余窗口内观察，不重复拉起；超过原30秒预算后，身份核验的NX必须连续两次明确无相机监听／无相机进程，之间间隔0.5秒。连接失败、进程存在、不可读或未知都不能按停止处理；若相机已出现，直接复用。
- 回收前重新读取session/pane/原进程身份，必须与原快照一致；只对该不可变session_id发kill-session，不结束其它会话或远端USB进程。回收后核验原tmux会话与本机原shell／SSH子进程均已退出，再核对NX相机状态，才允许原07脚本创建一次新启动器。身份变化／退出未确认／远端状态不明均停止本轮恢复，不强制重复启动。

旧日志保留，恢复依据与新启动日志引用通过原启动stdout／VLA monitor留存，不另建任务账本或后台重启循环。相机SSH新增ServerAliveInterval=5、ServerAliveCountMax=3，减少新连接在NX断电后长期悬挂；不追溯改变已经建立的旧SSH连接，选定ControlPath不变。本机G1默认独立连接，复用已有SSH master的场景仍受master自身设置影响。

O6健康时不重复初始化，明确停止时沿原start-direct核对身份／CAN／标定和当前实测位置建立保持，运行中异常或未知不自动强杀。stop保持现有“停止HTTP”语义：先核对空闲和同实例drain，再关闭HTTP；相机和O6保留，不把它当整机停止／断电，不修改业务任务状态或旧停止证据。下一次start负责复用／补齐／恢复已核验的本模块依赖，不要求先反复停止HTTP，更不能靠删任务账本解除阻断。

本轮修复仅写回原生命周期、原相机SSH脚本、测试和说明，未清理现场旧会话、未打开相机／初始化O6或重启服务。启动器CLI每次点击start都会新建进程读取本机代码，因此下一次显式点击即加载修复，无需为此重启大脑；真实恢复仍待现场复验。离线、所有权只读核对和已知失败见[启动器残留排障](多端联调_操控与抓取.md#vla-camera-stale-launcher-20261110)。

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

当前独立 NAV 入口启动配套 NAV／SONIC。2026-11-04 VLA 源码改为由 `run_agent_vla_runtime.sh start` 统一核验／复用相机、受管 O6 和 HTTP；停止仅关闭 HTTP，保留相机与 O6，见[统一 VLA 服务](接口说明_控制面板.md#vla-unified-runtime)。2026-11-04 已按用户授权重载 HTTP 并验证取图，相机／O6 原实例保留；完整统一启动过程没有在本轮重新执行。不能沿用旧 DREAM 三端一键脚本替代当前流程。

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

新版启动 VLA HTTP `:8091`，由官方启动器完成相机核验／缺失启动和受管 O6 准备；不启动导航 relay。2026-11-11 起面板远程 start 在原启动前同壳执行训练采集监测启用；手工启动若需同等监测，也应先 preflight／source 再 start：

```bash
cd <VLA_WORKSPACE>   # sonic_wbc 根，不是 bridge 子目录
capture_python="$PWD/g1_groot_n17_inference/.venv/bin/python"
"$capture_python" -m g1_recovery_memory.training_capture_v1 preflight --project "$PWD"
source g1_recovery_memory/training_capture_runs_20261111/launcher/enable.env
cd g1_brain_vla_bridge
BRAIN_VLA_REAL_ACTION_ACK=PHYSICAL_ESTOP_READY \
  ./run_agent_vla_runtime.sh start

./run_agent_vla_runtime.sh status
curl --noproxy '*' -s \
  http://127.0.0.1:8091/v1/vla/control/status \
  | python3 -m json.tool
```

`BRAIN_VLA_REAL_ACTION_ACK` 只表示现场已完成人工安全确认，不能替代支撑、急停和在线依赖检查。独立启动不会启动第二个 SONIC，也不会直接执行动作。仅 source 不会热更新已运行桥接；须新进程继承配置。任务客户端显示 `TRAINING_CAPTURE_READY path=...` 后才说明新记录器建立；当前采集组 `gpu_nav_ai_20261111_collection01`，归档默认在 `g1_recovery_memory/training_data/run_<run_id>/`。

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
