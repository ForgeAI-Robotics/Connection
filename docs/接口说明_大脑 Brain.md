# 接口说明：大脑 Brain

> **2026-10-28 当前源码：** NAV 接线沿用统一网关；新增 VLA 按需准备、授权、业务提交与退出候选，具体字段见[网关 VLA 接入](接口说明_机器人统一网关.md#162-vla-按需接入候选2026-10-28代码与隔离联测未部署)。规划不要求所有执行端在线，派发当前步骤时核验该端；普通导航不要求 VLA 或 O6 启动。新增代码未部署，持物导航仍待对齐；2026-10-27 的应用记录不能作为本轮运行版本证明。整轮取消后的新任务准入与后台退出按网关第 16 节处理。

初版日期：2026-08-27  
大脑实现核对日期：2026-10-22（本次核对新 VLA 接入；其他章节保留各自证据日期）
契约版本：`fq/reception-lan/v1`  
适用范围：共用大脑任务 API、任务控制与状态、单罐接待及仅导航流程、DREAM/VLA 调用与核验。

> 本文的大脑行为已按当前工作区代码核对。网页、飞书和语音文本入口调用大脑 `:5000`，任务网页为 `:8888`。任务取消、人工重试、弱证据与交接规则以本文为准；旧规划和历史联调中的不同规则不作为当前接口依据。本次只更新大脑相关内容，不据此宣布导航、操控的全部合同已实现或真机已验收。

## 1. 大脑侧职责与 API 归属

Runtime 是任务状态唯一权威；Planner 只给计划，Runner 不改业务目标，Verifier 输出 PASS / FAIL / UNKNOWN。接待、通用任务、观察、整理及演示共用 Runtime；真机端口与仿真端口由运行配置选择。大脑不直接实现 L1/L0，不发 NX 裸 START/STOP。交接依据及人工跳过例外见第 8.3 节。

| 接口 | 行为 |
| --- | --- |
| `POST /publish_task` | 保留 task、task_id、refresh、resume 与 accepted 等现有字段；一次只接受一个顶层任务，拒绝时不自动重发 |
| `GET /api/task_status` | 只读返回当前任务、步骤、尝试、失败与控制状态；字段见第 1.3 节 |
| `GET /api/sops` | 返回已登记业务包的 SOP 标识、版本、范围和明确触发词（`packages`），以及只能由明确触发词选中的 SOP 变体（`variants`，如仅导航接待）；不是候选经验文件 |
| `POST /api/task_pause`、`task_continue`、`task_cancel` | 暂停、继续与取消；联合控制模式取消后独立核清原控制退出，见第 1.2 节 |
| `POST /api/task_vla/pause`、`query`、`finish` | 操作当前新 VLA 原命令；必须绑定 `task_id/command_id`，finish 另需真实证据与核验凭据；查询不推进流程，见[操控接口](接口说明_操控VLA.md#暂停效果确认与大脑继续) |
| `POST /api/task_skip` | 必填 `task_id`、`step_id`；跳过当前未完成动作，执行中先请求停止，核对停止与资源后推进；保留人工跳过记录，不伪造成功 |
| `POST /api/task_preflight` | 当前默认实现仅返回提示，不实际检查三端就绪；见第 8.1 节 |
| `GET /health`、`/api/execution_profile` | 进程工作器状态与已应用配置版本 |
| `POST /api/reception/run`、`GET /api/reception/report` | 同一 Runtime 的 mock 演示与报告，不另起旧接待循环 |
| `/api/scene_state`、`belief`、`update_scene` | 大脑侧现场读模型；人工修改写入带来源和有效期的线索，不直接伪造已确认事实 |
| `/api/experiences`、`save_experience`、成功/失败经验接口 | 读取或人工保存经验，保持既有字段；不自动发布规则 |
| `/api/demo/parse`、`status`、`result`、`save` | 有界后台示教作业，job_id 关联；保存为未经评测候选，不直接写入生效 SOP |
| `/api/quad_latest`、`robot_status`、`record/*`、`timeline`、`task_timeline/*` | 严格使用选择的观察源；任务画面采集由大脑负责，网页退出不终止采集 |
| `/api/task_events`、`task_intent`、`chat`、`chat/route` | 事件查询、共享分流与飞书闲聊服务；没有绕过 Runtime 的观察执行 |

网页保留原路径，通过 `BrainClient` 转发。配置校验由 Ops `POST /api/validate-config` 提供。反思线程读取任务快照，按任务和账本版本去重；评测、影子和发布继续在学习侧，只有新任务绑定新快照，已开任务保留原版本。

`GET /api/reception/sop` 优先展示本机 `data/business/reception_sop_v2.yaml`（`version=v2`），
没有生成文件时展示 `config/business/reception_sop.yaml`（`version=v1`）。不再读取配置目录中的
历史 v2 副本；生成文件解析失败时返回错误，不以基础 SOP 冒充生成结果。`GET /api/sop`
仍展示 `config/business/demo_sop.yaml`。这些接口只展示内容，不发布经验或修改任务规则。

任务网页首页仅展示大脑连接状态、接待环境，以及任务发布、控制、进度、异常、历史和总结。
机器人坐标、录像、四宫格与回放、配置校验、工具列表、belief 和桌面整理 SOP 已从首页移除，
不另设调试页，也不再由首页请求或轮询这些数据。现有后端接口与大脑侧任务证据采集保留；SIMPLE O6 仍可通过脚本运行。

### 1.1 共用任务选择与异常建议

大脑先持久化任务再调用 LLM，任务选择与规划等待期间仍可取消。明确的“开始接待”等登记触发词直接选择当前环境登记的接待版本（当前真机为 `reception.fridge_pick`）；其它表述由模型按登记范围选择业务包。模型只能选择包，不能向接待 SOP 注入动作、坐标或跳步。通用任务继续通过 Planner 读取实际能力与场景生成步骤。

普通抓取、放置、导航请求不因未匹配接待 SOP 而拒绝；没有要求接待的普通动作交给通用 Planner 校验能力与场景。
接待的数量、物体、桌子和路线限制适用于接待请求，不扩展为全部任务的限制。

真机旧抓放后端及 SIMPLE 新建通用任务使用与 SOP 相同的导航、抓取、放置技能。`body_real` 直连原 DREAM / VLA，
`body_simple` 使用同一合同的物理仿真端点；不经过 Slaver。旧任务继续按账本原后端查询和恢复。
模型输出完整支持标记 `supported`、未支持要求 `unsupported_requirements` 及 `navigate(target)`、`pick(object)`、`place(object,target)`；缺少标记、未完整支持或含未知技能时拒绝。程序以任务绑定的
`selection.body_capabilities` 快照解析点位、超时、导航证明、持物前提、交接与核验，拒绝坐标注入和未知参数。
旧抓放能力目录复用已登记 Skill 的点位和操控合同，尚不等于任意地点、任意物体的执行能力。

2026-10-22：真机新任务读取 VLA 能力，快照保存在 `selection.vla_capabilities`。支持 native 动作时，通用操控规划使用 `selection.native_vla` 中登记的 `open_fridge/grasp_cola` 与作业路线；纯导航保留原路线。真机“开始接待”使用同一 Skill 中显式登记的开门／抓可乐分支；右侧真机导航＋操控模拟共用同一分支；旧物理仿真及 ROS 导航仿真变体保留原抓放步骤。不支持的动作会在任何步骤派发前拒绝。下面的抓放示例只适用于旧抓放后端／SIMPLE；新 native 的配置、证据与能力边界统一见[新动作接入](接口说明_操控VLA.md#新动作接入2026-10-22)。

| 普通任务示例 | 编译后的动作 |
| --- | --- |
| 去二号桌 | 导航 table2，核验后结束 |
| 抓取可乐 | 先导航 table2，再抓取 `cola_can_1` 并核验持物 |
| 把二号桌的可乐放到一号桌 | 导航 table2、抓取、沿登记中间点导航 table1、放置并核验 |
| 去一号桌 | 沿登记正向路线到 table1，不添加抓放 |

当前路线支持正向前缀，不支持回头、任意新坐标；抓取限定 table_2 的单罐，放置限定 table_1。
单独放置已有持物需要跨任务持物与导航证据，目前拒绝；不假定上轮物理世界仍可沿用。
这些是当前执行合同的限制，后续扩展须同步接入执行端与核验。通用身体任务的异常建议保留人工确认，
恢复保持已编译步骤顺序，不让模型改写导航证明或插入新的抓放。暂停、继续、跳过、取消入口保持原语义。
首次升级已有真机配置需重新应用运行环境并重载大脑，旧 `unavailable` 生效快照不会被代码自动改写。

Desk 通用规划中的每个 `subtask` 只能包含一个抓取或放置动作，抓取后放置必须拆成两个步骤。
派发前核验整份计划；“抓取 milk_1 并放置到 milk_area”等合并动作会被拒绝并进入
`recovery_required`，不会截取其中的抓取动作后把整项请求记为完成。已知目标区的定点导航
仍按原规则省略，其他后端已支持的搜索抓取合同不变。

正式大脑的任务选择、规划、异常建议、`/api/task_intent` 的模型分流、`/api/chat`、`/api/chat/route`
及后台反思共用 `config/brain.yaml` 的 `model` 配置；反思来源记录实际模型名称。
切换 OpenAI 兼容服务时一起设置 `model_select`、`model_dict.cloud_model`、`cloud_server` 和对应凭据引用，
重启大脑后生效，不更改任务账本、路线或核验标准，不自动故障切换。

`model.max_tokens`、`temperature`、`top_p`、`seed` 可显式设置，缺省分别保持
2048、0.2、0.9、42；`presence_penalty`、`extra_body` 只在配置时转发。
思考模型可通过 `extra_body.chat_template_kwargs.enable_thinking` 及后端支持的
`top_k` 等参数启用对应模式，不能只修改模型服务而让大脑仍发送原来的输出上限。

现场描述使用 `config/slaver.yaml` 的 `camera.vlm` 配置，支持 `max_tokens`、
`temperature`、`top_p`、`presence_penalty`、`seed`、`extra_body` 和 HTTP `timeout_sec`；
未配置时保持原默认值。`config/brain.yaml` 的 `brain.observation_timeout_sec`
控制描述步骤期限，默认 30 秒，必须为正的有限值。长思考模型须同时核对生成预算、
HTTP 等待时间和步骤期限；增加等待不降低核验要求，也不改变取消与迟到结果隔离。

本地 VLM 可通过上述端点配置接入，并用
`camera.vlm.extra_body.chat_template_kwargs.enable_thinking=false` 对现场描述关闭思考；
现场描述适配器每次读取配置，环境变量端点覆盖仍有优先级。图像尺寸上限、任务专用
结构化输出与延迟必须分别验证，不能把单次缓存命中的速度当作新图或并发保证。
本机切换及原图／4MP 实测见[本地 VLM 识别稳定性](多端联调_模型接入与评测.md#e-m5)。
模型服务执行序列数与大脑 Runtime 任务并发不是同一配置；增加推理服务的并发不改变
任务状态权威、动作派发顺序或核验标准。当前本机推理服务的四序列档及单通道三机器人
适用边界见该联调第 6 节，不能将短字段模型耗时当作整条业务链路验收。

视觉／照片核验仍使用 `reception_real.verification.vlm/llm` 的独立配置，需要分别核对；
切换图片 VLM 不会同时切换文字裁决 LLM，也不会开启原来关闭的照片核验开关。
实际模型切换与验证见[千问切换联调](多端联调_模型接入与评测.md#e-m1)。

2026-10-20 图片接入更新：上述视觉配置均支持显式 `auth_required: false`，用于无鉴权本地模型；
未配置时仍要求各入口原有的云端凭据，不从模型地址猜测是否免鉴权。`camera.vlm` 还被桌面
双图判断、Slaver 相机／候选物体识别及抽帧学习复用，显式生成参数与 `extra_body` 会转发；
未配置的调用预算保留各入口默认值，配置后的 `max_tokens` 会覆盖对应默认预算。
`extra_body` 不能覆盖 model、messages、流式方式和标准生成参数。SDK 回环模型请求不继承桌面代理。
Slaver 诊断遇到空回答、模型失败或无合法状态时返回 unknown，工具结果为 failure，不报 normal。
接待核验配置在大脑初始化时绑定，旧 Slaver 相机配置在模块导入时读取，修改文件不等于既有进程生效。
本机配置、调用范围、真实测试与部署限制见[本地图像 API 替换](多端联调_模型接入与评测.md#e-m6)。

2026-10-21 代理兼容修正：服务启动及模型 SDK 构造前，将当前进程继承的
`HTTP_PROXY / HTTPS_PROXY / ALL_PROXY`（含小写）中的 `socks://` 别名规范为 `socks5://`。
保留代理地址、端口、认证和 `NO_PROXY`，不修改桌面系统或 tmux 的全局设置；
未知协议仍明确报错，不静默改为直连。普通 core 安装包含 `socksio`，避免规范化后再因缺少依赖失败。
已有大脑进程需重载才采用修复，模型客户端初始化错误表示请求尚未发出。
故障与复验证据见[模型代理兼容排查](多端联调_模型接入与评测.md#model-proxy-compat)。

`GET /api/task_status` 增加以下只读字段：

| 字段 | 含义 |
| --- | --- |
| `selection` | 包名、选择来源、原因、SOP 标识和版本；执行环境选择后绑定，恢复时不重新选择 |
| `recovery_advice` | 异常分析与建议，包含 `action`、`summary`、`reason`、`source`、原任务／命令／版本、`applicable`；`automatic=false` 表示尚未执行 |
| `handoff_observation` | 原命令停止与现有导航／操控状态接口的只读诊断；通路就绪不等于控制器接管 |
| `subtask_list[].reported_success` | 下游是否自报成功；非此类端口或尚无回包时为 null |
| `subtask_list[].evidence_level` | 下游报告的证据等级或适配器的证据等级 |
| `subtask_list[].effect_verified` | 当前尝试是否已通过 Verifier；不能由 LLM 建议改变 |

基础建议包括等待、人工协助、取消，以及当前状态允许的查询原命令或继续；绑定恢复策略的新任务还可选择只读刷新现场和受限剩余计划调整，见第 1.1.3 节。LLM 不直接调用控制接口；模型异常或格式非法时保存规则回退建议。建议基于的账本版本变化后 `applicable=false`，迟到结果不能覆盖新状态。默认任务网页显示失败原因和历史尝试；开启下述建议确认功能后，另外展示模型建议，明确区分事实和模型解释。API 保留 `recovery_advice`。

操控模拟混合后端的异常分析上下文明确携带 `manipulation_simulated=true`，要求模型把开门、持物、抓取和放置
写成模拟状态。新建议、规则回退及已有建议的状态展示均在 `summary`、`reason` 前标明模拟操控范围，
不把模拟持物当成真实效果；普通状态读取仅修饰返回文本，不改历史账本、建议动作或确认权限。

异常分析只接收当前任务已编译的 `bound_steps` 与 `recovery_planning.remaining_steps`，
不接收 Skill 中未选用的其它流程定义；模型上下文的 `selection` 只保留所选身份、版本与定义摘要。
建议正文也须遵守任务步骤范围：明确引用未登记步骤编号，或提及当前流程不含的放置／关门常见动作术语时，
整条模型建议按既有机制回退为规则建议并记录原因，不修改任务计划。该文本校验是保守的常见越界拦截，
不替代 Runtime 的动作、恢复方案及确认校验；旧版已登记放置的任务仍可描述放置。

### 1.1.1 接待 Skill 定义与任务快照

2026-10-12 起，现有单罐接待从 Python 固定步骤表抽取为
[`reception_single_can_v1.yaml`](../src/brain/packages/definitions/reception_single_can_v1.yaml)。
原流程作为 v1 基线保留。新版 [reception_single_can_v2.yaml](../src/brain/packages/definitions/reception_single_can_v2.yaml)
登记 2026-10-12 提供的五个点，在抓取后增加 relay1，relay3 保留横移，抓放参数与核验条件保持原行为。
五点朝向于 2026-10-13 按现场数据修正，仍属同一 SOP v2，以内容摘要区分标定修订；
同日按用户现场指令将 2 号桌 Y 减少 0.40 米，X 与朝向保持不变，当前值以定义文件为准。
新任务读取修正后的定义，旧任务保持原快照。对接与当轮证据见[导航五点定义](接口说明_导航NAV.md#61-五点接待定义2026-10-12)。
SOP 版本为字符串 `2`；Skill 格式版本与 LAN 请求协议版本是不同概念。明确触发词仍直接选流程，
其他表述仍由 LLM 判断是否符合登记范围；本次没有开放模型生成动作或在线更改参数。

定义使用结构化 YAML，程序严格校验，不通过模型解释任意 Markdown 来执行：

| 字段 | 含义 |
| --- | --- |
| `schema_version` | 定义格式：`1` 冻结原四段合同；`2` 支持配置导航点和连续段序 |
| `contract_version` | 执行协议，目前只接入 `fq/reception-lan/v1` |
| `sop` | 业务 ID、字符串版本、说明、范围与明确触发词 |
| `parameters` | `object_id` 和整数 `quantity`；现合同限一罐指定物体 |
| `points` | 命名导航点：目标 ID、路段、段序、`goal_xyt` 和运动模式 |
| `points.<点键>.display_name` | 可选现场名称，例如 `冰箱前`；不改变点键、target_id 或坐标，也不发送给 NAV |
| `steps` | 有序动作列表：`id`、`action`、`command_prefix`、正数 `timeout_sec` |
| `steps[].display_name` | 可选完整步骤显示名，优先于由点位名称推导的导航／检查标题 |
| `steps[].verification_display_name` | 抓放步骤的可选核验标题，写入生成的核验节点 |
| `real_workflow` | 可选真机接待分支，含独立 `sop` 元数据和 `steps`；先导航、open_fridge、grasp_cola，再依次执行其余登记导航点，引用同一份 points |
| `steps[].point` | 导航动作引用 `points` 中的点名 |
| `steps[].after_navigation` | 观察或抓放引用最近的导航步骤 ID，作为原命令凭证来源 |
| `steps[].optional` | 仅观察支持；可选检查仍受 `dream_inspection_enabled` 控制 |
| `steps[].verification_id` | 抓放之后的核验节点 ID；不提供时自动生成 |

编译器生成初始化、核验节点和跨导航／操控交接，抓放要求物体证据，放置要求安全收尾。

2026-10-22：点位与步骤显示名称由 Skill 配置，限制为 1～120 字符的非空单行文本。导航默认组合为“导航到＋点位名称”，横移使用“横移到”，检查使用“检查＋点位名称＋的物品”；显式步骤标题优先。当前 v2 将 `points.table2.display_name` 定为“冰箱前”（用户确认该真机点位就是冰箱前），`target_id=table_2`、步骤 ID、命令前缀和坐标均保留。中文显示不再从这些旧 ID 推断现场物体。

名称编译到 `phase_specs[].display_name/target_name` 并随任务持久化，状态接口返回 `subtask_list[].display_name` 和 `phase_labels`。网页步骤、失败环节、跳过提示、总结与进程日志优先读取该任务的名称；导航顺序错误也使用任务中的点位名，原始错误仍保留。通用身体规划继承同一 Skill 名称，模型仍输出点键，不得用中文名称替换协议 ID。NAV 报文、SIMPLE 路线声明与比对只使用 `skill.navigation_points` 中的执行字段，不携带显示元数据。

已有任务保持原快照：缺少显示字段时兼容旧中文表，不读取当前 YAML 去改写历史。仅修改配置不会改变正在显示的旧任务名称，新任务才绑定新名称；新后端代码仍需正常重载后生效。

2026-10-22 后续按用户提供的现场截图更新冰箱前位置与朝向，当前坐标统一维护在
[`reception_single_can_v2.yaml`](../src/brain/packages/definitions/reception_single_can_v2.yaml) 的 `points.table2.goal_xyt`。
真机与操控模拟混合流程共用该点，协议 `target_id=table_2`、段序和其它点位不变；
新任务创建时重新加载定义，无需因本次 YAML 点位修改重启大脑。旧任务继续使用原坐标快照。
这次更新是用户指定点位的配置变更，不代表新点已完成导航或操控实测。

定义不能关闭这些条件。未知动作、重复 ID／前缀／YAML 键、错误引用、无效超时、缺失抓放或
非有限坐标、不支持的运动模式、物体或路段顺序，均在创建动作端口前拒绝。
schema 1 额外检查原四段精确坐标；schema 2 允许修改坐标和增减导航点，段序必须从 1 连续编号，
每个登记点恰好引用一次。横移仍要求先到 `door_approach`，再执行 `door_lateral_exit`。仅导航变体从同一份已校验定义派生，
不包含操控和跨端交接；它的完成仍不代表送达物体。

离线校验和预览不会创建任务、连接机器人或启动服务：

```bash
venv/core/bin/python scripts/reception_skill.py
venv/core/bin/python scripts/reception_skill.py --config config/brain.yaml
# 当前真机接待分支；仅离线展开，不连接机器人
venv/core/bin/python scripts/reception_skill.py --config config/brain.yaml --real
venv/core/bin/python scripts/reception_skill.py --file src/brain/packages/definitions/reception_single_can_v2.yaml --nav-only
venv/core/bin/python scripts/reception_skill.py --file /path/to/reception_skill.yaml
```

后续流程从基线复制成新版本，保留基线文件，通过 `config/brain.yaml` 的
`reception_real.skill_file` 指定。空值使用内置基线；相对路径以 `CONNECTION_WORKSPACE` 为根，
绝对路径也可用；显式文件不存在或非法时拒绝，不静默回退。路径配置随大脑进程加载，
定义文件在新任务选择时读取。修改代码或配置不等于已重启、部署或切换现场实例。

`GET /api/sops` 展示该定义的 SOP 和仅导航变体，并返回 `reception_workflow_sha256`；
非法定义返回 HTTP 422。旧 `/api/reception/sop` 仍是历史演示的学习规则视图，
`config/business/reception_sop*.yaml` 不作为本单罐 Skill 的执行步骤源。

新正式接待任务在 `selection.workflow` 保存完整 `definition` 和规范化内容的 `sha256`；
模型等待期间文件变化不会换掉本次定义。规划只编译该快照，展开后的 `phase_specs` 保存每步的
导航参数和导航凭证引用。重启、暂停后继续和原命令查询沿用原计划，即使定义文件已修改或删除，
也不重新选业务或改已发出的请求。既有无新增字段的历史步骤仍按原 v1 固定合同读取，不迁移或清空账本。

本机配置选择 `src/brain/packages/definitions/reception_single_can_v2.yaml` 后，新任务使用新版；
需回归旧仿真时显式选择 v1。`reception_protocol` 仍只接受旧四段合同，新版在创建端口前拒绝。
`reception_simple` 在每个新任务创建端口前比对 NAV/VLA 健康响应声明的 `navigation_legs` 与任务快照；
当前新房子 O6 适配器支持五段路线，旧服务未升级或点位不匹配时拒绝，不静默改回四段。
另外比较 `simulation.navigation_only` 与所选 SOP：仅导航执行器不接受完整接待，完整执行器也不接受仅导航任务，
避免省略抓放后在仿真阶段顺序上卡住。仅导航使用明确触发词“开始接待（仅导航）”。
物理场景映射与准入字段见 [SIMPLE O6 接口](接口说明_simple%20o6%20仿真.md#新办公室场景office_v2)。
真机仍使用 `fq/reception-lan/v1` 请求结构，但必须由导航端登记相同坐标、目标、段序与运动模式；
编译成功不等于远端接受。新版对接字段见[导航接口新版路线](接口说明_导航NAV.md#61-五点接待定义2026-10-12)。
冰箱操控、开关门或其他新动作仍需接入执行与核验，本轮没有新增。

### 1.1.2 异常建议的人工确认

本功能已应用到当前项目；本机于 2026-10-12 启用。配置 `brain.reasoning.confirm_recovery_advice: true`
才允许确认建议，公开模板默认关闭。原接待任务保留基础控制建议；新建接待与通用任务可绑定第 1.1.3 节的恢复策略。模型不能修改坐标、物体、动作参数或自动执行恢复。
正常成功步骤仍按任务 Skill 快照推进。人工同意一次建议不修改长期规则。

网页异常区默认仅显示失败环节、失败原文、简短原因解读；有当前有效建议时，追加一句操作建议及确认按钮。
任务 ID、任务环境在进度区保留。基础建议的原命令、模型完整建议与理由、来源和证据折叠到“详细信息”；新的现场刷新／恢复方案显示理由，恢复方案还显示全部未完成步骤的建议顺序及证据 ID，文本均转义显示。
原因解读使用已知错误的确定性中文说明，不把模型解释当作事实；建议按已授权动作显示简短文案。
例如扫描覆盖不足解释为“末端修正受阻：扫描覆盖不足，导航无法确认修正区域的通行条件”；
末端路径不直、直线段不足和近障碍拦截分别说明，原始英文与数值保持原样。
建议须与当前任务、步骤、命令匹配且版本有效、未被确认。重试、步骤推进或任务结束后，旧建议仅留在折叠历史中，
不再显示为当前异常或可操作按钮；含跳过的终态仍明确显示未全部成功。
页面忽略晚于新响应到达的旧状态响应及旧请求错误，状态变化后清理过期控制提示；同一记录的详情展开状态在轮询中保持。

状态查询共享一个在途 GET，网页转发等待上限 5 秒，浏览器等待上限 8 秒，禁止缓存。
正常连接时不显示更新计时。查询失败保留上次任务进度，并标明连接异常及距上次更新的秒数；达到 10 秒未更新时显示状态待更新提示，并停用基于旧状态的任务按钮。恢复正常后提示自动消失。
刷新页面后会恢复展示当前账本，不只显示新创建的任务。已取消任务退出当前进度展示，收进默认折叠的“上一轮已取消 · 查看记录”，
不自动加载总结；仍可手动查询和复制上一轮总结。若原命令未核清或仍阻止新任务，相关提示保持可见。
折叠历史不删除账本、不改变任务控制与派发条件。上述时限只约束页面读取，不取消机器人命令。

继续等控制请求的浏览器等待上限为 8 秒。超时、连接失败或 5xx 只表示控制回包未确认，
页面只刷新查询，不重发 POST，并暂时禁止再次点击继续；观察到新命令、任务变化或任务终态后解除该等待。
明确的 4xx／`accepted=false` 与回包丢失分别提示；原任务、步骤、命令身份仍由服务端核对。
`current_command` 返回当前尝试的 `command_id`、`attempt_id`、`stage`：
`prepared`、`submitted`、`unknown`、`rejected`、`terminal`、`succeeded`、`failed` 或 `cancelled`。
其中 `submitted` 仅表示账本已登记下发意图，不能解释为导航已受理或已经行走；
`succeeded` 表示该尝试通过核验。网页不把点击、HTTP 202 或下发登记当成到达。

“本轮总结”读取既有 `/api/reception/report`，异常等待和任务结束时自动加载，默认折叠，支持复制。
新增 `summary_text`、`unresolved` 和 `execution_backend`，保留原报告字段。
总结由 Runtime 当前状态和历史尝试确定性生成，包含任务环境、完成情况、失败环节与英文原文、
重试／跳过次数及结果未知事项；成功不清除历史失败，模拟抓放和取消不冒充真实效果或机器人停止。
旧任务或旧状态迟到的报告不能覆盖当前页面。
普通暂停／继续／跳过／取消按钮保留原语义；模型不可用不阻止这些入口。

正式大脑应用的异常建议在独立的 `brain-advice` 有界工作器上计算，不占用任务推进或新任务规划工作器。
最多一份分析在执行、一份最新上下文待处理；中间过期上下文不积压。结果回到 Runtime 所属线程，
按原任务、账本版本、状态和命令校验；继续、跳过、取消、换任务或关闭进程后，不应用迟到结果，也不自动执行建议。
独立库调用 `BrainService` 仍为同步模式，正式入口使用 `BrainApplication`。

控制请求进入大脑应用后立即记录收到、开始处理及受理／拒绝结果，用 `control_id` 关联；
下游实际调用、受理、状态与结果继续沿原命令日志记录。异常分析也记录后台开始、等待、完成或过期丢弃。
页面在 `running` 且当前尝试尚未登记下发时显示“等待下发当前步骤”，登记后显示“等待动作执行结果”，
核验阶段显示“正在核验执行结果”。这些标签不声称导航已行走，真实下游受理和状态可在大脑日志核对。

状态接口额外提供 `recovery_confirmation_enabled` 和 `advice_confirmations`。
建议增加 `advice_id`、`step_id`、`control_command_id`、`execution_target`、`facts`，仍以 `applicable` 表示版本适用性。
`facts` 来自任务快照，含原尝试、异常、查询回执和已建立事实；未知项不能推断为成功。

```http
POST /api/task_advice/confirm
Content-Type: application/json

{"task_id":"当前任务ID","advice_id":"状态接口返回的32位建议ID","expected_revision":42}
```

`expected_revision` 必须使用该建议的 `record_revision`；三个字段必填，不接受客户端指定动作或执行参数。
参数错误返回 400；功能关闭、过期建议、任务／执行环境变化或操作被拒绝返回 409。
受理返回 200 与 `accepted=true`、`confirmation`；确认回执不是机器人动作完成证明。

| 建议动作 | 确认后的处理 |
| --- | --- |
| `observe_scene` | 按绑定能力只读查询现场接口，记录范围和读取时间，再异步请求 LLM 分析；不创建物理命令、不核验推进 |
| `revise_plan` | 人工确认具体方案后，重新核对原命令与证据，原子记录计划版本并恢复执行；受限规则见第 1.1.3 节 |
| `query_original` | 仅查询原命令、保存身份匹配的证据并重新分析；不新建尝试、不核验推进或启动后续动作。查询到成功后，另行确认继续才核验原成功 |
| `continue` | 调用同一 Runtime 的现有继续逻辑；先核查原命令、停止和资源。原动作已成功则核验，确认未完成才准备新尝试；随后仍受原前置条件约束 |
| `cancel` | 结束本轮大脑任务；联合模式沿第 1.2 节组织原控制退出，任务 cancelled 不表示机器人已停 |
| `wait` / `request_help` | 记录本次处理意见，不派发身体动作或自动发送外部联系消息 |

确认先在原任务账本记录，再处理操作。相同任务和建议的重复请求返回原确认记录及 `no_op=true`，不重复执行。
`advice_confirmations` 保存建议快照、确认时间、原步骤／命令、操作结果；`status=processed` 表示控制处理结束，
动作成败以任务步骤核验为准；`rejected` 表示操作未获执行；`processing` 表示确认已记录而处理结果尚未确认。
进程在确认后退出或响应丢失时，刷新核对原账本和命令，不能通过再次确认自动重放。
任务、步骤、命令、账本或环境版本变化后旧建议失效；模型返回迟到建议也不会覆盖新任务。

### 1.1.3 执行异常后的受限恢复方案

2026-10-21 增加第一版恢复方案能力。沿用 `POST /api/task_advice/confirm` 和单次人工确认，
不增加任务循环、独立账本或直接执行模型代码。正常步骤继续按原计划推进，仅在原恢复状态产生建议。
第一次确认可只读刷新现场，读取成功后用新证据重新分析；第二次可确认具体恢复方案。
普通暂停、继续、跳过、取消继续独立可用。未确认不自动执行，不自动解除下游保护。

新任务在选择业务包和后端时绑定 `recovery_policy`。`brain.reasoning.recovery_plans` 默认 true，
但派发确认仍受 `confirm_recovery_advice` 控制；`max_recovery_revisions` 默认 2，可设 0～5，
限制每任务接受的恢复方案版本数。旧任务未绑定策略时沿用原控制建议，重启不会补写新策略。
恢复策略由既有 `PackagePolicy` 声明；未来业务包须显式声明适用模式与真实能力才能复用。

| 路径 | 第一版能力与限制 |
| --- | --- |
| 通用任务／Desk | 保留所有未完成步骤和原动作参数，可以调整独立物体抓放的顺序；同一物体抓放顺序、单持物约束及物体间放置依赖不得破坏 |
| 接待、仅导航、混合接待 | 只读刷新导航状态与世界接口，然后根据证据建议按原顺序恢复；固定坐标、门段、抓放和核验顺序不变 |
| 通用身体任务（`body_real` / `body_simple`） | 保留编译后的导航、抓放和核验顺序；可只读刷新状态并经确认恢复原步骤，不得越过下游保护 |
| 旧 SIMPLE 通用单步抓取 | `/v1/scene` 只提供场景定义；允许读取并分析，不据此开放同任务物理回合重置或恢复方案派发 |
| SIMPLE／ROS 接待 | 复用接待状态读取；下游明确禁止重试、安全失败或不可恢复物理结果时不开放恢复方案 |
| 未接入的后端或业务包 | 不声明新增恢复能力，不借用别的后端的观测 |

`observe_scene` 的回包范围明确区分接口状态、仿真状态和场景定义；不调用相机、检测器或
`inspection` 动作，不冒充重新识别物体。Runtime 保存 `recovery_observation` 与历史
`recovery_observations`，关联原任务、命令、计划版本和建议授权。读取时间不是传感器拍摄时间；
缺少来源、失败或迟到响应不更新现场证据，也不改变原动作是否停止的事实。

LLM 的 `revise_plan` 回包在原 `action/summary/reason` 之外增加：

```json
{"plan":{"step_order":["STEP_3","STEP_4","STEP_1","STEP_2"],"observation_id":"本次现场证据ID"}}
```

这里只能引用原计划的全部未完成 step_id，不能增删动作或提交工具名、坐标、任意参数。
Runtime 在保存建议、确认及应用时检查；当前方案引用的证据须仍属于原命令和计划版本，读取时间在 60 秒内。
通用任务可调整的业务顺序还需人工审阅；SOP 必须与原剩余顺序完全一致。
若未来需要新增观察动作或替换物体／目标，须先接入真实技能与对应校验，当前不支持。

恢复方案只接受已确认失败且停止、资源已释放的原动作；提交未知须先查询原命令。
确认时再次查询仍须返回身份与时间有效的失败终态；若变为成功、结果未知、未停止或下游禁止重试，
方案不执行。未知转为已知失败时保留原 UNKNOWN 回包，把新核查证据另存，不覆盖来源。
`failure_scope=safety`、`next_command_allowed=false` 或明确 `simulation.retry_allowed=false`
都阻止方案派发；一般 `retryable=false` 也不开放新方案。SIMPLE／ROS 仿真接待是一个已核对的特例：
终态命令的 `retryable=false` 禁止自动重放，但若同时明确 `simulation.retry_allowed=true`，
允许经人工确认、原失败／停止／资源重新核查后，以新 command_id 恢复同一回合。
此特例只适用于绑定的仿真接待后端，不能覆盖安全失败或禁止下一命令，不能用于真机解除保护。

接受方案原子增加 `plan_revision`，在原任务 `plan_revisions` 中记录修改前后步骤、原因、
证据和原命令；已完成前缀与全部旧尝试保留。新派发仍走 Runner／Verifier，需重试的步骤生成新的尝试和命令。
状态 API 增加 `recovery_policy`、`recovery_observation`、`plan_revision`、`plan_revisions`；
网页历史可查看修改前后，人工重试统计包括恢复方案授权的重试。
双击或重启不重放已消费的确认，取消／换任务期间的迟到模型结果、读取结果或原命令查询结果不得应用。
模型请求仍在有界工作器执行，远端查询通过原协作 I/O 返回 Runtime 所属线程核对。

本次软件验证与实际模型探测范围见[受限恢复验证](多端联调_大脑任务与恢复.md#bounded-recovery-plans)。

### 1.2 任务控制接口

四个按钮均先作用于大脑，再由大脑决定是否调用当前执行端。任务、步骤和命令是不同层级，`cancelled` 必须结合所属层级解释。

| 接口 | 大脑行为 | 下游调用与完成条件 |
| --- | --- | --- |
| `POST /api/task_pause` | 关闭后续派发，保留当前任务和步骤 | 有在途命令时调用该执行端的命令取消接口，再查询停止；未确认时进入 `recovery_required`，`pause_requested=true`，不假装远端已停 |
| `POST /api/task_continue` | 继续当前环节，不重开整轮任务 | 未下发时重查条件；原命令已成功时核验；确认未完成、停止和资源释放后才准备新尝试。详见第 9.1 节 |
| `POST /api/task_skip` | 人工放弃当前未完成动作，推进下一动作 | 执行中先请求停止；原命令停止、资源和身份核对通过后才推进。保留失败与跳过记录，不生成虚假成功 |
| `POST /api/task_cancel` | 废弃本轮大脑任务，关闭后续派发并结束本地等待 | `completed`／`scope=task_only` 只确认本地取消。启用 body_control 时另撤销旧执行权限，并行组织原命令停止／让出及网关撤权、封禁；`control_recovery` 单独报告收尾，不以 cancelled 冒充停止 |
| `POST /api/task_control_recovery` | 人工重查／收尾原控制，不重新发布任务 | 提交当前 task_id、状态中原 expected_lease_id（无当前源时为 null）；仅取消／暂停／待恢复或终态可用，源或任务变化拒绝。返回 202 后通过状态查看，不自动继续任务 |

暂停的实现是结束当前下游命令、保留大脑流程；继续可能使用新命令重新执行未完成步骤，不要求导航具备同一命令的暂停／恢复状态。新 VLA 在 `waiting_human` 等待效果核验时，普通暂停同样取消原命令；专用 `/api/task_vla/pause` 才是保留命令的策略暂停。该等待阶段“继续”只查询原命令，未收到身份匹配的终态就拒绝推进，不生成新尝试。

新动作的 `vla_confirmation` 包含 `command_id`、`operation`、VLA `state`、`pause_requested`、`verification_ready_at`、`finish_requested`，用于网页区分仍在执行、已暂停待核验、正在停止／归还、已到终态。它是 Runtime 保存的下游观察，不是另一套任务状态。请求受理、效果证据提交与大脑任务通过分别展示；普通状态轮询不主动查询或重发动作。

请求规则：

- 暂停、继续、取消可传 `task_id`，调用方应绑定已显示的任务，避免操作新任务；传入任务与当前不符时拒绝。
- 跳过必须同时传非空 `task_id` 和 `step_id`。`can_skip=true` 只表示当前步骤可申请跳过，不代表停止检查已经通过。
- 继续可传 `step_id` 和 `expected_command_id`，从状态接口的 `control_step_id`、`control_command_id` 获取；无命令时传空字符串。网页和飞书使用这些字段防止陈旧请求误重试新命令。
- 重复暂停已暂停任务、继续正在运行或核验的任务，以及对匹配的已结束任务执行控制，可返回 `no_op=true`，不会再下发一次动作。
- HTTP 200 不能单独代表控制成功：还需检查 `accepted`、`state`、`error` 及当前任务状态。参数错误可返回 HTTP 400。

取消当前活动任务的请求与响应示例：

```http
POST /api/task_cancel

{"task_id": "reception-example-001"}
```

```json
{
  "accepted": true,
  "completed": true,
  "scope": "task_only",
  "task_id": "reception-example-001",
  "command_id": "nav-table2-example-001",
  "state": "cancelled",
  "error": null,
  "message": "本轮任务已取消，不再执行后续步骤"
}
```

`command_id` 是取消时大脑记录的原在途命令，可为 null；不是新发出的停止命令。大脑保留尝试历史及取消时的事实，隔离迟到执行／规划结果。未启用联合控制时保留本地取消行为；启用时，普通取消和人工确认的取消建议都通过同一个 Runtime 取消事件组织控制收尾，重复取消可核查尚未结束的原控制。

**2026-10-27 当前网关接线：** 大脑先关闭派发和续期，再并行发起 NAV 停止与网关撤权；接收隔离、身体停止和原 O6 保持证据全部核清后释放占用。原任务／命令、首次错误、未知结果和查询恢复保留。新增 source 中的 control_resource、gateway_binding、permit_phase、permit_seq、revoked、confirmed_remaining_ms 都是只读投影；本机剩余时长只代表最近确认许可的保守期限，不是新的现场观察。当前实现及未完成项见[网关实际接线](接口说明_机器人统一网关.md#15-nav-阶段实际大脑接线2026-10-27)。

**2026-10-26 旧 relay 实现排障依据（以下至当前持物规则保留历史语义；新网关不执行 relay）：** 任务先 cancelled、旧授权立即失效，后台按原身份执行 NAV 原业务 cancel（仅存在提交意图时）、yield 和 relay close；VLA 复用原 stop，并等原退出流程回调 release。无需旧任务成功，也不要求停止 NAV／VLA／SONIC 服务。子请求发送前落盘，丢回包与再次点击只查询原请求；未知保持阻断，原失败不删除。

prepare 失败且业务 goal 从未提交时，NAV 可能没有可 yield 的执行绑定。此时要求原 prepare 查询身份匹配、原生步骤为空、当前 NAV 无活动命令／操作且输出封禁、停止和保持明确，关闭对应 relay 后复核 NAV／VLA 的同一当前控制器。控制器变化时可按共同接口的 boot ID 证据核清整机重启前的旧进程；不因单纯 PID 变化或服务不可达放行。原生步骤已发生、原命令丢失或缺少退出证据仍阻断。明确被旧源阻断且从未发出任何业务／控制写入的排队记录可核销，但原失败回执保留。规则详见[共同恢复接口](接口说明_relay控制权对接.md#取消与旧控制恢复)。

尚未发送 NAV prepare、仅 relay 开启阶段失败时，取消／核清按原 relay 请求及当前关闭证据收尾，不要求 NAV 释放本轮未接管的身份；原开启结果未知或端口／队列关闭未确认时仍阻断。完整判据见上述共同恢复接口。

交接失败保留首次 POST 的 HTTP 状态、原始回包和后续查询错误，任务的原始错误详情可追溯；明确拒绝显示 HANDOFF_REJECTED，404 不覆盖最初原因。有限的 NAV 受理前拒绝可在核实当前输出已停止、封禁及 relay 已关闭后按本轮未接管收尾；一般拒绝与网络异常仍不等于未执行，完整判据见[交接 HTTP 拒绝](接口说明_relay控制权对接.md#交接-http-拒绝与原回包留存)。旧记录中已经丢失的回包不会被补造。

`POST /api/task_control_recovery` 可额外传布尔值 `new_exit_check=true`，为原控制身份请求一次新的当前退出证明；默认 false 保留原退出查询行为。任务与 expected_lease_id 校验、原记录保留和禁止自动继续仍适用。该接口保留用于运维排障，任务网页不提供调用按钮；旧 prepare／yield 的未知结果不被重写，重复请求沿同一新退出请求查询。字段与 NAV 已清除本地关联后的回执要求见[当前退出重新核验](接口说明_relay控制权对接.md#当前退出重新核验与已清除关联的-nav-回执)。

`control_recovery` 是既有授权账本的只读投影，包含 blocked、pending、source、unresolved；source 给出原 task_id／command_id／lease_id 与 cleanup_error。持续控制会话下，取消整轮立即解除该轮本地准入阻断，原会话退出在后台独立核查，不要求人工核清后才能发布；新执行权仍由下游准入核验。已取消会话的未核清退出只计入 previous_exit_unconfirmed，不能冒充已停。新任务自己的准备失败可以再次进入 recovery_required，不能据此判断上轮取消失效。

任务页面不展示控制会话、退出核查详情或手工清旧按钮。准备检查未通过时用直接原因说明目标尚未下发；导航提供分项原因时翻译具体原因。原错误、完整回执及诊断字段保留在折叠的原始回包中。取消结束本轮，新发布进入新任务；后台保存原请求并处理原会话退出，下游拒绝或故障在新任务中直接展示原始错误，不要求用户操作控制账本。若本轮仍为待恢复且账本证明业务从未提交，可由人工「继续」按原 Runtime 规则创建新尝试；不会自动重发。状态轮询不触发任何退出操作。

环境应用若遇到已取消任务仍保留 `resources_cleared=false`，会通过 Runtime 只读查询
`task_cancellation.abandoned_command_id` 对应的原请求。原执行地址必须匹配任务快照；回执的命令身份、
时间、终态、停止和资源释放均确认后，Runtime 追加 `cancelled_resource_checks`，更新停止与资源核查结果。
取消状态、取消时的历史事实及原尝试不变，不产生新命令、不继续任务。查询失败、未知回执、身份不符或
查询期间任务变化仍拒绝环境应用；取消接口和普通状态轮询本身不触发这项核查。

文字控制中的“暂停”“停止”“/stop”映射为暂停；“取消任务”“取消”“/cancel”映射为任务取消。

### 1.3 任务状态与失败字段

当前任务状态使用小写：`running`、`verifying`、`paused`、`waiting_human`、`recovery_required`、`succeeded`、`failed`、`cancelled`。旧账本可能出现 `cancelling`，当前本地任务取消不需要等待该中间状态。

| 字段 | 当前含义 |
| --- | --- |
| `active`、`terminal`、`state` | 大脑任务是否处于非终态、是否结束及状态，不等于机器人是否在运动 |
| `phase`、`control_step_id`、`control_command_id` | 当前步骤与最新尝试身份；已准备的新命令号不一定已发送 |
| `can_resume`、`can_skip`、`skip_step_id` | 控制入口的可申请状态；调用时仍需重新检查 |
| `pause_requested` | 已请求暂停；与 `recovery_required` 同时出现时应提示停止尚未确认 |
| `failure_detail` | 当前待恢复原因，含 `kind`、`code`（可缺省）、`message`、`display_message`、`not_started` |
| `failure_detail.kind` | `rejected` 为已识别的提交前拒绝，`execution_failed` 为有失败／取消终态回执，`unknown` 为结果未明确 |
| `failure_detail.not_started` | 只有已识别的提交拒绝才为 true；false 不等于已确认运动发生 |
| `failure_detail.downstream`、`failure_history[].downstream` | 当前尝试的下游任务／命令身份、`state`、完整 `error` 和 `result` 原始字段副本；未知嵌套字段原样保留，未提供的字段不补造 |
| `failure_history` | 按尝试保留失败步骤、命令、错误码、原文与中文说明；不是逐行异常日志总数 |
| `attempt_summary` | `failure_count`、`retry_count`、`manual_retry_count`；查询原命令不算新尝试 |
| `manual_skips`、`flow_finished` | 人工跳过及流程是否因含跳过而结束；含人工跳过不记为全部成功 |
| `subtask_list` | 每步的成功、失败、未知、跳过及证据；`done` 可能包含跳过，不能只据此统计成功 |
| `completed`、`total` | 进度节点计数，包含准备／核验节点；不等于实际动作数。成功数量需排除跳过 |
| `all_done` | 仅任务 `succeeded` 为 true；不表示历史上从未失败 |
| `task_cancellation` | 本地取消范围、时间、原命令和当时远端已知状态；不新增物理停止事实 |
| `blocks_new_motion` | 大脑任务是否阻止新任务准入，不代替下游安全检查 |

**通用回包展示。** 大脑不根据某个导航错误名称决定是否展示证据。当前失败、历史失败和复制总结
均读取各自尝试的 `error.details` 与其 `terminal` 字段；已知字段 `goal_distance`（米转厘米）、
`final_yaw_error`（弧度转角度幅值）和 `turn_clearance`（转向许可、最近障碍、要求净空及原因原文）
按字段展示并注明回包／采样快照来源。缺失或无效数值不显示，不用其它尝试补齐，不据此推断到达或当前现场状态。
远端仅在净空原因字符串提供 `nearest_m`／`required_m` 时可从明确键值提取；已有结构化字段优先。

已有中文翻译仅用于辅助阅读，不是错误准入名单。新错误码、新原因和未识别的嵌套字段直接保留在
`downstream`；网页无论是否存在模型建议，都可展开“下游原始错误与结果”，复制总结也包含这些数据。
只有失败终态和 `result.message`、没有 `error` 对象时，仍展示下游提供的原因。
网页“回包说明”与“模型解释（非执行回包）”分开呈现；完整执行回执仍在原尝试账本中。
这层展示不改变 Verifier 判定、任务控制、停止核验或下游准入策略；新增导航故障原因不要求大脑跟随发版。

中文说明与英文错误原文、错误码一起保留。任务后来成功时仍保留历史失败与人工重试；失败历史不能覆盖最终成功，也不能被最终成功抹除。

## 2. 局域网拓扑

| 服务 | 地址 | 调用方向 | 用途 |
|---|---|---|---|
| 大脑 API | `http://<BRAIN_IP>:5000` | 网页／飞书／语音入口 → 大脑 | 任务、控制、状态的统一接口 |
| 任务网页 | `http://<BRAIN_IP>:8888` | 浏览器 → 网页 → 大脑 | 发布、展示和转发控制 |
| 语音文本入口 | `http://<BRAIN_IP>:8890` | 上游识别文本 → 大脑 | 只提交已识别文字 |
| DREAM | `http://<NAV_IP>:8001` | 大脑 → DREAM | 世界、状态、导航 |
| VLA | `http://<VLA_IP>:8091` | 大脑 → VLA | 抓取、放置、可选相机图片 |
| RealSense | NX `<ROBOT_IP>:5555` 唯一相机服务打开 USB | VLA 子系统内部 | HTTP 桥只读订阅；照片开关开启时生成动作后图片 |

地址由 `config/networks.yaml` 的当前网络及显式环境配置解析，表中使用角色占位符。联调采用大脑主动轮询，不要求 DREAM 或 VLA 回调大脑。三台主机需要互相可达，防火墙放行 TCP 8001、VLA 服务端口和大脑网页端口。

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

- `config/execution.yaml` 与已应用的 `data/system/execution.json` 决定接待、通用执行和观察各自的后端；存在已应用配置时优先按该路由选择。
- 真机接待端点由 `reception_real.dream_base_url`、`vla_base_url` 提供，网络配置可解析并写入运行配置；HTTP 客户端使用 `contract_version`、`request_timeout_sec`。
- `reception_real.kernel_enabled` 是真机派发许可。2026-10-09 本机已用于真机仅导航联调，但文档不把开关值作为所有部署的默认值或验收结果；查询生效配置与现场条件后判断。
- 无已应用路由时，接待后端依次读取 `RECEPTION_MODE`、`brain.reception_backend`，再按 `reception_real.enabled` 回退选择；不能仅凭模板中的环境变量推断当前后端。
- `dream_inspection_enabled=true` 可启用完整接待中的可选 inspection；仅导航变体不包含该步骤。

当前共用 Runtime 按结构化执行证据核验，不使用旧接待循环的 `COMPLETED_HAND_STATE_ONLY` 成功终态。模板中的 `vla_result_policy`、`grasp_photo_verification_enabled`、`place_photo_verification_enabled` 不能作为当前共用链路已启用“手状态通过”或“VLM/LLM 图片双判真”的依据；这些旧字段不能绕过第 8.2 节的物体证据要求。

步骤时限及请求构造见 `src/brain/packages/reception.py`。本次更新未改变配置或接口实现。

## 5. 大脑调用的 DREAM 接口

下表列出客户端能力及执行链路使用的接口，不表示当前任务启动时必定依次调用全部端点。

| 类别 | 方法 | 路径 | 用途 |
|---|---|---|---|
| 健康能力 | GET | `/health` | DREAM HTTP 健康查询 |
| 执行门／交接 | GET | `/v1/status` | 导航通路、定位、安全门和活动命令状态 |
| 世界能力 | GET | `/v1/world` | 客户端可获取世界入口，不等于准备节点已实际查询 |
| 关系图能力 | GET | `/total_scene_graph_latest.json` | 客户端默认关系图路径，也可使用世界回包中的 URL |
| 导航 | POST | `/v1/navigation/goals` | 提交每段导航 |
| 轮询 | GET | `/v1/commands/{command_id}` | 获取真实导航终态 |
| 命令停止 | POST | `/v1/navigation/cancel` | 暂停／跳过及联合模式取消后的控制收尾，按原命令请求停止并查询实际结果 |

导航成功硬条件：

```text
state == "succeeded"
AND result.success == true
AND result.reached == true
AND result.navigation_stopped == true
```

## 6. 大脑调用的 VLA 接口

下表区分执行时调用与客户端保留能力，不将相机接口存在视为图片判真已接入。

| 类别 | 方法 | 路径 | 用途 |
|---|---|---|---|
| 健康能力 | GET | `/health` | VLA HTTP 健康查询 |
| 控制交接 | GET | `/v1/vla/control/status` | 动作端口、策略和相机总状态 |
| 动作 | POST | `/v1/vla/tasks` | 提交 pick/place |
| 轮询 | GET | `/v1/vla/tasks/{command_id}` | 查询 VLA 终态 |
| 命令停止 | POST | `/v1/vla/tasks/{command_id}/cancel` | 暂停或执行中跳过时，请求结束指定操控命令；取消大脑任务不调用 |
| 相机能力 | GET | `/v1/camera/status` | 客户端保留的相机状态查询，不代表当前接待自动调用 |
| 快照能力 | POST | `/v1/camera/snapshots` | 客户端保留的动作后图片能力；不据此声明当前图片判真已接入 |
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

## 8. 当前接待流程与核验

### 8.1 流程与请求身份

2026-10-28 网页提供三个明确入口，共用当前 real_workflow、任务身份和原有暂停／继续／取消／效果核验：

| 按钮 | 任务后端 | 实际执行 |
| --- | --- | --- |
| 开始接待（导航真机＋操控真机） | reception_real | NAV 与 VLA 均为真实接口，不回退本地模拟；显式按钮在非真机环境拒绝 |
| 开始接待（导航真机＋操控模拟） | reception_real_sim_manipulation | 真实 NAV；开门、抓取及效果为本地模拟，不连接 VLA |
| 开始接待（导航模拟＋操控真机） | reception_sim_navigation_real | NAV 全部在本地模拟，不查询或提交真实 NAV；VLA 准备、动作、暂停、效果确认及退出均走真实网关接线 |

第三种模式发布前由现场人员确认机器人已摆好冰箱作业姿态；网页向原 publish_task 入口附带布尔 worksite_confirmed=true。大脑确认当前身体保持、记录本任务的机器人／SONIC 实例、唯一作业点及确认时间，随后派生 kind=operator_ready 的 worksite_evidence 传给 VLA。该确认最多有效 300 秒，绑定原任务和当前动作，重试／重启不延长；过期、实例或作业点改变仍阻止真实动作。模拟导航回执不进入真实到位证据，也不请求任何身体许可。这个模式仅支持同一冰箱作业点上的开门与抓取，不新增点位或另建任务状态机。

模拟导航步骤明确标记“（模拟）”，保存 simulated=true、physical_execution=false、evidence_level=simulated_navigation。仅从原 Runtime 回执恢复，缺失保持 UNKNOWN；不能凭原请求补造成功。操控仍需要原 pause／finish 真实效果确认，完成页面显示“导航为模拟，未实际运送”。第一种模式是真实执行路由，不表示全部下游能力已经验收；真实持物导航仍按现有网关准入限制处理，不能用第三种模式的模拟运送作为实物搬运成功。

2026-10-22 起，当前 v2 Skill 的 `real_workflow` 为真机“开始接待”登记 `reception.fridge_pick` v2：

```text
初始化 → 读取现场 → 导航到冰箱前
→ open_fridge → 暂停、授权效果确认、继续核验
→ grasp_cola → 暂停、授权效果确认、继续核验
→ 中转点 1 → 中转点 2 → 横移到中转点 3 → 1 号桌 → 结束（仍持有可乐）
```

首点直接引用 `points.table2`，保持 `target_id=table_2` 及现有真机坐标。开门与抓取引用同一个原 NAV command_id，开门与抓取之间没有第二次导航；抓取核验后保留中转点 1、2、3 和 1 号桌的原路线。不执行关门和放置。每个动作仍由 VLA 核查原导航证明及当前交接条件；原证明不再有效时拒绝，不能伪造到位或自动重走。开门 PASS 才能抓取，人工跳过不能代替。运送步骤要求 Runtime 已核验持物，派发前也检查 VLA 的当前持物与恢复状态；跳过抓取不能解锁运送。到达 1 号桌后仍检查停止、通路归还和持物记录，成功不代表已放到桌面。持物记录保留为 `cola_can_1`。旧 v1 三动作任务保留原快照，不自动增加导航段。

新任务将 `workflow_variant=real`、独立 SOP 身份及包含真机分支的完整定义绑定进 `selection`。真机环境的目录／模型选择按此范围判断；要求额外关门或放到桌面的接待不能截断为取物／运送成功。VLA 两个动作必须明确可用，旧能力接口缺失不能将本分支回退成 pick/place。当前真机分支不依赖通用任务的 `native_vla` 额外点位配置。

仅导航、ROS 导航仿真＋操控模拟、SIMPLE／旧协议模拟继续使用根 `steps`，其 `reception.single_can` v2 流程为：

```text
初始化 → 读取现场 → table2 导航 → 可选 inspection
→ 抓取 → 抓取核验 → relay1 导航 → relay2 导航 → relay3 横移
→ table1 导航 → 放置 → 放置核验与最终交接
```

明确触发词“开始接待（仅导航）”选择同版本的 `reception.single_can.nav_only`，包含两个准备节点及定义中的全部导航，不派发抓取、放置、inspection，也不执行跨导航／操控交接。v1 为四段、v2 为五段；完成只证明仅导航流程完成。

**2026-10-27 身体独立联调范围：** “开始接待（仅导航）”和“开始接待（导航真机＋操控模拟）”都使用 hand_mode=not_required／hold_reference_id=null，不要求 O6 在线。后者的开门／抓取与持物为本地模拟，不请求真实手部权限，也不向 NAV 传携物要求；只有 Runtime 已绑定的混合后端可使用该虚拟条件，真实携物与真实 VLA 仍拒绝。身体安全、实收帧和退出检查不变。最新合同见[身体独立模式](接口说明_机器人统一网关.md#身体独立模式更新2026-10-27)。

2026-10-22：右侧明确触发词“开始接待（导航真机＋操控模拟）”已同步当前真机 Skill，登记为 `reception.fridge_pick.sim_manipulation` v2，绑定 `workflow_variant=real_sim_manipulation`、`execution_backend=reception_real_sim_manipulation`。它与左侧生成相同的步骤、名称、五个导航点及顺序，仅由执行适配器把操控切到本地模拟；不单独维护另一份路线。不关门、不放置，到 1 号桌后虚拟持物记录仍是可乐。

导航仍是真机，受 `kernel_enabled` 和 NAV 原有准入、到达、停止核验约束。开门／抓可乐由现有本地模拟客户端在默认约 1 秒后返回模拟终态，Runtime 自动核验推进，不等待真实效果确认，也不接收真实 VLA 核验凭据。模拟提交前仍查询原 NAV 命令的任务、目标、身份、完成时间及到达／停止事实；导航失败或忙碌不能伪造成功。此模式不连接 VLA HTTP、不要求真实 VLA 在线。2026-10-27 起，其所有真实导航步骤与“仅导航”共用 BodyControl 的网关授权、续期、退出和取消；不向模拟操控签发实体控制权。真实 O6 保持仍由网关实际观测核验，模拟握持不能代替该证据。

开门、抓可乐及两个核验节点均显示“（模拟）”。原始回执为 `simulated=true`、`physical_execution=false`、`evidence_level=simulated_object`，不伪造 operator_observation 或 visual_verifier 的实物证据。只有混合适配器允许这些新动作的模拟效果通过；真实适配器拒绝把同一回包当实物成功。操控模拟模式的暂停、继续和取消沿用现有任务控制语义。

运送使用本任务已通过核验的虚拟持物及本地模拟状态，虚拟持物丢失或来自别的任务仍阻断后续导航。原 request 和任务身份不改写；BodyControl 从 Runtime 的 reception_real_sim_manipulation 后端识别虚拟持物范围，不把 required_holding 转成真实持物许可或伪造 O6 反馈。模拟状态从已保存的原终态回执恢复，不另建任务账本；回执丢失保持 UNKNOWN，不凭原请求补造成功或重发。完整流程及恢复、历史失败与手工跳过的规则与真实分支一致，任务完成只表示混合联调通过。

2026-10-14 登记的旧 `reception.single_can.sim_manipulation` 抓放流程继续供旧任务快照及没有 real_workflow 的旧 Skill 使用，不转换已有任务。修改后的分支只对新建任务生效。

隔离 ROS 仿真另使用明确触发词“开始接待（导航仿真＋操控模拟）”与
`reception.single_can.ros_sim_manipulation`，绑定 `execution_backend=reception_ros_sim_manipulation`。
该变体只接受已应用的 `reception_simple` 路由，并核对服务身份、同一实例、完整点位合同、
`navigation_only=true` 和 `scene=office_ros_8001`；固定路点执行器不满足该模式的准入条件。
真实导航项目在隔离 O6 仿真中执行，操控使用同一本地模拟客户端，页面环境明确写“导航项目仿真”。
它不要求打开真机派发许可；真机模式和仿真模式触发词不能跨环境执行。
实验启动方式见[ROS 仿真接入](../simulation/backends/ros_navigation/README.md)。

大脑从原任务 Skill 快照中的导航点构造请求；没有快照字段的旧任务才兼容读取原固定合同。第 7 节请求示例为 v1 基线，新版字段见导航接口第 6.1 节。模型不重新生成这些坐标、路段或动作模式。每段独立提交、查询和核验，正常流程核验通过才推进；人工跳过另作记录。

所有动作沿用原 `task_id`；新尝试使用新 `command_id`，原请求、回执和失败不覆盖。操控请求包含 `operation`、`object_id`、`target_area` 和引用原导航命令的 `navigation_proof`。完整请求示例见导航、操控接口文档，不使用旧版 `runtime_phase`／`verified_state` 作为当前任务状态字段。

`/api/task_preflight` 对明确登记的接待触发词、真机接待后端执行只读导航预检：
以当前 Skill 读取 `/v1/status` 和 `/total_scene_graph_latest.json`，每次读取上限 3 秒。
启用 `reception_real.body_control` 时，这两个只读请求使用与正式执行相同的 NAV 服务凭据来源（优先 `nav_token_env`，否则 `token_env`／`BRAIN_CONTROL_TOKEN`，支持对应私有 `_FILE`）；缺失或文件无效继续拒绝，不回退无鉴权。预检不创建控制授权。
核对接口在线声明、协议版本、map 坐标系，以及导航公布的各段目标、阶段、运动模式、坐标和最终朝向要求。
XY／yaw 的比较容差为 1e-5 米／弧度，yaw 按周期比较；此容差不是物理到达门限。
两端仍按固定点位准入时，定义不一致会列入 `blockers` 并在提交前阻止任务创建。
导航明确声明 `independent_map_goal` 且不强制参考坐标或路段元数据时，对应差异只写入 `warnings`；
不会把导航旧参考路线恢复成业务段序约束，也不修改大脑点位。

响应包含 `ready`、`required`、`checked`、`checked_at`、`blockers`、`warnings`、`scope`，
适用时附当前 `skill_sha256`、`navigation_revision` 和 `motion_blockers`。
缺少可比较的声明时 `checked=false` 并提示未核实，不能理解为点位一致；协议／坐标系冲突、
读取失败或固定合同差异则 `ready=false`。非明确接待触发词或仿真后端不执行这份真机导航预检，
明确返回未检查，仿真仍走已有实例／合同校验。预检不检查 VLA 真实抓放能力、不批准定位、不 Arm，
也不因 `motion_ready=false` 单独阻止发送目标。发布接口会重新预检；继续原任务不套用新任务点位预检。

`INITIALIZING`、`FETCHING_WORLD` 仍是本地进度节点，不表示真实三端均已就绪。
真正派发时，真机端口仍受派发许可约束，导航门读取下游 `navigation_transport_ready`；
门不可达或未开时等待人工，继续后重新检查。导航定位批准、规划和执行条件仍由导航判断。

### 8.2 成功证据

大脑先检查命令、任务、目标等身份，以及带时区的完成时间，再由 Verifier 输出 `PASS / FAIL / UNKNOWN`。下游自报成功不直接等于大脑任务成功。

| 动作 | 当前适配器接受的主要结果条件 |
| --- | --- |
| 导航 | `state=succeeded`、`success=true`、`reached=true`、`navigation_stopped=true` |
| 抓取 | `state=succeeded`、`success=true`、`object_grasped=true`、`holding` 等于请求物体；`evidence.object_presence_verified` 不能显式为 false |
| 放置 | `state=succeeded`、`success=true`、`object_grasped=false`、`released=true`、`holding=null`、`object_at_target=true`；`evidence.object_at_target_verified` 不能显式为 false |

抓取和放置还要求 `policy_stopped=true`、`navigation_port_ready=true`，步骤要求物体等级证据。`evidence_level` 或 `evidence_grade` 为 `hand_state_only` 时，适配器不认定物体效果通过；只有手部开合或弱成功回执时保留未知，不能用 LLM 建议补成 PASS，也不能因为未知就重复抓放。

这些条件说明大脑如何解释下游合同，不证明导航／操控已在真机上提供了全部可靠传感器证据。已发布的规则还可能追加核验要求；不能用回包字段齐全代替真实效果验证。

### 8.3 控制交接与人工跳过

普通交接先核对来源命令身份、时间、成功终态、停止及资源释放，再读取导航 `/v1/status` 与操控 `/v1/vla/control/status`。要求两侧无活动命令、操控策略未运行、导航通路已归还。

交接受阻的原因分别记录：回执身份或时间不合格为 `source_receipt_invalid`，停止未确认为
`source_stop_unconfirmed`，资源归还未确认为 `source_resources_unreleased`。回执有效且停止、资源归还
已确认时，失败／取消分别为 `source_failed`／`source_cancelled`，尚无成功终态为
`source_success_unconfirmed`；不能把动作失败表述为停止未确认。真实执行与操控模拟混合模式使用同一分类，
原始来源回包仍保留，交接成功条件不变。

- 下游不提供 `control_receipt` 时，满足上述条件可按 `transport_ready_without_receipt` 放行。
- 下游提供该字段时，必须通过任务、原命令、目标控制器和时效核验；无效凭证不等于缺省凭证。
- 从来源命令步骤到交接目标之间包含人工跳过时，大脑记录 `manual_skip` 并补齐该次交接，不再重复查询交接。跳过操作自身仍核对停止和资源。
- 导航被人工跳过后，大脑可继续提交操控，`navigation_proof` 仍引用原真实命令。请求中的声明不改写原导航终态；操控仍按自身合同查询并决定接受或拒绝，不承诺必然执行。

通路空闲不是实际控制器接管的物理证明；上述内容是大脑当前放行规则。最终放置还需完成 `safe_idle` 交接核验。

<a id="brain-owned-control-draft"></a>

### 8.3.1 relay 控制权对接（大脑侧离线实现，默认关闭）

2026-10-23 按用户要求收紧为职责与最小对接需求，统一见[relay 控制权对接说明](接口说明_relay控制权对接.md)。此前未冻结的 draft1／draft2 详细协议已撤回，不作为实施要求；不要求两端照搬其中的统一 ready／renew、逐帧元数据或完整权限框架。

大脑沿现有 Runtime 与执行适配器组织切换，复用任务、命令及交接请求身份；原交接检查中“操控结束必须导航就绪”的依赖随实际接线调整。本文第 8.3 节仍描述迁移前代码，不能用其无凭证放行或人工跳过补齐规则冒充新联合模式的控制授权。大脑侧可选实现已接入，配置、API 回执与验收边界统一见上述对接说明；公开模板默认关闭，本机已按授权完成[服务重载与控制 API 启用](多端联调_大脑任务与恢复.md#brain-control-live-apply)。联合模式取消后的控制收尾以第 1.2 节为准，未知资源占用保留在控制记录中。

### 当前持物检查与重新确认

2026-10-26：联合控制模式已接入操控当前持物证明。运送前读取 `current_holding` 和 `holding_evidence`，校验原抓取任务／命令／已退出授权及当前 O6／控制器身份、目标和反馈连续性；不把 VLA 业务库存 `holding` 或旧成功任务当作当前有效持物证明。具体字段和跨端边界统一见[共同接口](接口说明_relay控制权对接.md#当前持物证明)。

原 `/api/task_vla/finish` 与核验凭据保持：暂停并查询到 `waiting_result_confirmation` 后，提交实际 `source`、`reference`、带时区的 `observed_at` 及本动作效果字段。抓取确认可选传 `evidence.valid_until`，原样转发；缺省为原观察起 60 秒，显式指定最长 300 秒，必须尚未到期。大脑在发 finish 前校验期限，明确拒绝后允许人工修正；请求已发送但回执未知时保留原意图，只查询原命令，不重新生成时间或重发 finish。历史结果核验不因当前期限已过而抹掉曾经确认的效果。

任务网页展示退出证明或最近交接／派发核查中的持物值、原观察时间、绝对有效期和失效原因，并明确标注为最近核查快照。网页按到期时间提示重新确认，不通过页面轮询续期，也不将快照冒充持续实时物体检测。

到期或反馈／目标／实例异常后，应在原执行与控制权均已安全退出的边界，由核验方提供真实新观察，使用操控已有的认证 `/v1/vla/operator-state` 或其 `confirm_effect.py operator-state` 入口，关联原终态 task_id／command_id。操控先核查 idle，再登记证明；重复旧观察不延长期限，原失败不改写成功。此轮没有新增大脑 operator-state 转发或网页自动复核按钮；登记新证据后仍按原 Runtime 的人工继续／原命令查询规则处理，不自动继续动作。

本次只修改源码并离线验证；正在运行的大脑和任务网页需由用户在原授权核清后重载才能应用，不能把代码更新当作现场已加载。

### 8.4 流程终态

全部必要步骤通过后为 `succeeded`；过程中有人工重试不清除失败历史。含人工跳过的流程结束为 `failed` 并设置 `flow_finished=true`，不显示全部成功。取消整轮任务为 `cancelled`，仅表示大脑已废弃编排。

当前不再生成 `COMPLETED_HAND_STATE_ONLY`。原始弱证据仍可保留在尝试回执和 `subtask_list[].evidence_level`，不能转换成物体成功终态。

## 9. 本地状态保存

任务状态由 Runtime 单写，当前账本位于 `data/tasks/`。`KernelStore` 使用目录互斥、版本检查和原子文件替换，保存每一步的尝试、不可变请求、原 `command_id` 及证据。旧接待记录在 `data/retired/reception/` 仅作历史留存，不作为新内核的续跑入口。

动作发送前记录意图；重启或丢回包时优先核查原命令。未知结果不转换为成功，也不靠删除账本、清 holding 或 `force_new_task` 续跑原动作。显式取消任务按第 1.2 节本地废弃整轮，不伪造原命令停止证据。

## 9.1 同一 Runtime 的继续与人工重试

| 项 | 当前行为 |
| --- | --- |
| 入口 | `POST /publish_task` 的 `resume: true`，以及 `/api/task_continue`，均进入大脑的原任务控制通路 |
| 任务身份 | 沿用原 `task_id`，附着账本记录的业务包、执行后端和目标地址；不重新发布一整单 |
| `recovery_required` | 有在途原命令时先查询；已识别的提交拒绝可准备新尝试；未下发且交接失败时重查交接。结果未知时不重发 |
| `paused` / `waiting_human` | 按各自状态继续，重新经过相应条件检查；继续不等于已取得身体动作许可 |
| 新尝试 | 人工继续核对旧尝试停止、资源释放和明确未完成后才创建；自动恢复预算仍为 0，只读重查不增加 `-r{n}` 计数 |
| 取消与新任务 | 本地任务先结束；联合模式另组织原控制退出。授权尚未核清时，在创建新任务前拒绝并提示原控制，退出后可新建；取消回执本身不等于远端已停 |

当前不承诺下游跨重启一定保留历史命令。继续原任务时，原命令不可查仍保持 `recovery_required`；用户另行取消任务不依赖该查询。控制交接与效果证据见第 8 节，不能套用旧规划中的“取消必须确认下游停止”规则。

人工“继续”核查原命令结果、停止和资源释放；新导航是否满足当前定位、安全门及 Token 条件，由导航端
在提交时裁决，大脑按准入回包记录接受、拒绝或未知，不另行裁决导航恢复许可。
这与第 1.1.3 节受限恢复方案的确认约束是不同入口。

## 9.2 提交拒绝与不确定事务

导航提交前先持久化原 `command_id`，不因异常自动换号重发：

- 只有 HTTP 400／403／409／422 且错误结构、身份和错误码符合 `src/contracts/submission.py` 的识别规则，才视为明确提交拒绝，记录 `failure_detail.kind=rejected`、`not_started=true`。忙碌错误还需明确占用者是其他命令。
- `STOP_NOT_CONFIRMED` 属于提交准入拒绝：导航未收到新鲜停止反馈，未受理本次新目标。符合上述拒绝合同才记录为未开始；这不证明此前动作已经停止。已保存为 UNKNOWN 的同类原始拒绝回包可由现有拒绝核对流程重新分类，保留 `original_unknown_progress`，不自动继续或重发。
- 任意 HTTP 400 不能一概视为未执行；HTTP 500、裸 404、同 ID 冲突或不符合拒绝合同的回包，也不能证明动作未执行。适配器保留结果未知。
- 人工跳过明确拒绝且未执行的步骤时，复用与当前任务、尝试、命令绑定的原始拒绝回执，不查询未创建的下游命令。若此前还有实际提交的命令，仍查询其停止和资源释放；拒绝当前命令不证明其它动作已停止。跳过抓取／放置同时标记关联效果核验为人工跳过，保留全部失败尝试，不写为成功。
- 导航 POST 连接断开或超时时，客户端有界查询原命令；查到则继续跟踪，查不到进入恢复处理，不自动重新 POST。
- 轮询阶段连接失败或原命令不可查，同样不能改写为成功，也不能自动换号补发。
- 下游明确失败／取消终态与提交前拒绝分别记录；人工继续的条件见第 9.1 节。下游 `retryable=false` 不应被文案直接解释为“永远不能人工重试”，也不意味着其他恢复条件已经满足。

取消任务是独立的本地控制决定，不用于推断原命令究竟是否执行。

## 10. 当前代码落点

| 位置 | 职责 |
| --- | --- |
| `src/brain/api/app.py` | 大脑 API 路由，提交与控制任务 |
| `src/brain/service.py` | 附着原任务、规划与调度的应用入口 |
| `src/brain/kernel/runtime.py` | 状态迁移、原命令核查、人工重试与跳过、本地任务取消 |
| `src/brain/storage/tasks.py` | 任务和尝试账本 |
| `src/brain/packages/reception.py` | 接待相位与合同 |
| `src/brain/adapters/execution.py` | 下游回执解释、停止与资源证据、交接条件和可选凭证核验 |
| `src/entries/web/`、`src/entries/feishu/`、`src/entries/voice/` | 独立入口，通过大脑接口操作任务 |

## 11. 验收边界

2026-10-09 最后一轮仅导航任务四段成功，含两次人工重试、零跳过；不等于无需干预一次通过，也不代表抓放或完整三端接待通过。最终任务取消改动部署在该轮之后，不把这轮算成新按钮版本的真机复测。

当天大脑侧修复及回归范围见[多端联调记录](多端联调_大脑任务与恢复.md#e-b3)。本次文档核准基于工作区实现，未修改协议路径、导航目标或远端程序，也未新增真机验证。

## 接待协议模拟路由（2026-09-28）

运行环境的接待模块可选 `simulation_backend: reception_protocol`。从正式 `/publish_task` 入口执行「开始接待」，任务状态与账本标记 `execution_backend=reception_protocol`，仍执行 `reception.single_can` v1。独立下游默认延时成功，任务核验不设模拟特例。旧 `/api/reception/run` 是旧会议演示入口，不用它验证本流程。

`handoff_observation` 可包含共用可选合同 `fq/control-receipt/v1` 的核验结果。凭证缺失、有效或无效时的处理统一见本文第 8.3 节。详细启动、端点和真机差异见[模拟真机接口说明](接口说明_模拟真机.md)。

## 语音文本入口（2026-09-29）

`python -m entries.voice` 默认监听 `:8890`，通过当前网络配置解析大脑地址，可由 `MASTER_URL` 显式覆盖。它不采集音频，也不筛选文字。

`POST /publish_task` 要求 JSON 字段 `task` 为非空字符串。缺省时入口生成 `task_id`，并把 `refresh` 设为 true，然后原样转发到大脑 `POST /publish_task`。请求头固定为 `X-FQ-Source: voice`、`X-FQ-Via: voice`。空文本返回 400；大脑不可达返回 503。`GET /health` 只表示入口进程在听，不表示大脑已接受任务。

大脑收到后的分类、预检、控制和执行与网页、飞书提交的同一段文字相同。NX 上的 `reception_voice.py` 仍只向网页 `:8888` 发送固定任务「开始接待」，尚未改为调用本入口。


### 持续 NAV 控制会话候选接线（2026-10-27）

修订 20261027-control-session-v1-draft1 已接入原 Runtime／BodyControl 并完成隔离验证，随后按用户要求应用大脑、网页和本机面板；导航迁移与 SONIC 就绪仍待核清。多个导航命令复用同一控制会话；单段完成／单命令取消核验 current_hold 并保留发布器，整体取消才并行 yield／revoke。任务结果、命令停止与身体资源释放分别展示和保存。管理查询超时不自动撤权，重启不恢复旧执行权。配置、字段、schema=3 迁移及部署顺序统一见[共同合同第 16 节](接口说明_机器人统一网关.md#16-会话持续授权大脑与导航隔离接线)。


2026-10-27 整轮取消行为更新：取消立即关闭 Runtime 流程，解除本轮本地占用／等待阻断，新任务可直接发布；旧记录及未知结果留作历史。旧会话的停止、撤权和真实退出在后台按原身份收尾，不能误停、恢复或清除新任务。网页“已取消”只证明大脑流程结束，不宣称物理已停止；新运动仍由导航／网关核验接管。具体状态字段和并发边界见共同合同第 16 节“整轮取消与新任务准入”。
