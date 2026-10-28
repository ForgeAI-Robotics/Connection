# relay 控制权对接说明

> **2026-10-27 最新目标方案：** 见[机器人统一网关接口说明（暂定稿）](接口说明_机器人统一网关.md)。本页保留现有实现与历史证据；新方案尚未实施，不能把其拟定接口当作本页现行接口调用。

日期：2026-10-23。状态：**三端实际接口完整离线串联通过；本机大脑已按授权更新并启用控制 API，真机交接尚未验收**。公开模板默认关闭；本次应用范围见[本机应用记录](多端联调_大脑任务与恢复.md#brain-control-live-apply)。面向大脑、导航和操控负责人，说明本轮要达成的行为及分工；执行细节由各端沿现有代码提出最小调整，大脑统一核对。

## 1. 本轮要做什么

> **保留旧联合数据通路；大脑决定当前允许谁控制身体；NAV、VLA 和 relay 执行大脑的决定并报告实际结果；取消操控结束后自动恢复导航。**

大脑负责决定和组织切换，不处理逐帧动作。导航、操控继续负责自己的动作、模式切换、停止和本地保护；本端条件不满足时，可以拒绝执行并向大脑说明原因。

本轮不重做导航规划、定位、VLA 模型或推理，不迁移 relay，不新增统一转发器，不改变 SONIC 的订阅地址。现有独立导航入口保留，不能随文档修改自动切换运行链路。已有可靠性修复继续复用，不整批回滚。

## 2. 数据怎么走

沿用旧联合链路：

```text
导航时：NAV → 工作站上的原 relay → SONIC
操控时：relay 释放共享发布端口，VLA → SONIC
```

relay 实际转发导航数据；VLA 与 relay 轮流使用同一个工作站发布端口。原来的转发、端口释放和重新绑定能力继续复用，修改的是这些操作由谁决定。

旧 relay 根据导航完成、操控停止等消息自行切换的路径需要退出。以后这些消息只报告事实，是否切给 VLA、是否恢复导航，由大脑决定。

## 3. 三方分工

| 负责方 | 本轮职责 |
| --- | --- |
| **大脑：统筹与唯一裁决方** | 决定下一执行者，批准与当前任务匹配的控制申请；组织旧方退出和新方接管；记录当前授权及切换结果；不确定时阻断下一步 |
| **导航** | 保留导航业务和事实查询；撤出 NAV 全局授权裁决；执行大脑要求的让出／准备操作，报告导航停止、输出和模式状态；不自行授权 VLA |
| **操控** | 保留动作执行和效果确认；控制申请改向大脑；任务结束报告停止及退出，不强制恢复导航；实际发布服从大脑授权 |
| **relay，由操控侧维护** | 执行大脑指挥的关闭转发／释放端口、恢复导航转发，提供实际状态；不决定下一任务或下一控制方 |

代码放在操控项目，不代表操控拥有全局裁决权。大脑管理控制授权，Runtime 继续管理任务，不再由 NAV 保存另一套全局控制权决定。

**涉及 NAV、VLA、relay 的跨端调用顺序由大脑统一确认；各端内部执行顺序由各端负责。旧方退出未核清，不批准新方执行。**

## 4. 大脑需要对接的能力

优先沿用现有业务接口以及 VLA 的 acquire／release／查询交互形式。**不要求重新设计全部业务 API，也不把此前草案中的统一 ready／renew、逐帧元数据和复杂权限框架列为本轮必改项。**

| 对接事项 | 大脑的诉求 |
| --- | --- |
| **申请控制** | VLA 为大脑已下发的任务申请控制，大脑核对任务及交接条件后批准或拒绝；申请不能自行决定抢占。导航执行同样受大脑当前决定约束 |
| **结束本次控制** | 执行端停止本任务并报告退出，大脑核清后结束本次授权；release 不代表导航恢复，不要求下一执行者已经就绪 |
| **导航让出／准备** | 导航能按大脑要求停止旧任务及相关输出，或准备导航执行条件；准备完成不等于立即行走 |
| **relay 切换** | 能按指令关闭并释放通道，或恢复导航转发；报告实际完成、处理中、失败或未知 |
| **状态与原请求查询** | 能查询当前控制状态、原命令和原交接请求；重复查询不重新执行动作，历史成功不能冒充当前仍有权限 |

两端原有任务 ID、命令 ID、交接请求身份、请求落盘和丢回复后查询的机制保留。控制服务地址与导航事实查询地址分开，不能全局替换 `DREAM_BASE_URL`。

具体用现有哪个接口、需新增哪些最小入口、返回哪些字段，由负责端对照代码提出；共同请求与回执语义由大脑统一确认，避免两端各自定义。大脑不规定内部线程、进程或 SDK 调用方式。

### 收敛后的最小对接表

下面根据两端反馈固定本轮接线方向与路径。大脑一栏的新增接口已有本仓库实现；NAV／VLA／relay 的新增入口仍以各端交付为准。“复用”保留业务语义，只增加必要的授权关联或可查询退出事实。不新增整套 participant API。

| 提供方 | 入口 | 本轮处理 |
| --- | --- | --- |
| 大脑 | `GET /v1/vla-handoff/capabilities` | 新增大脑实现，沿用 VLA 客户端已有路径形式；明确权威是大脑及新对接修订 |
| 大脑 | `POST /v1/vla-handoff/leases/{lease_id}/acquire` | 新增大脑实现，VLA 原 client 改地址；只批准大脑已登记任务的申请，核清旧方退出后允许接管 |
| 大脑 | `POST /v1/vla-handoff/leases/{lease_id}/release` | 新增大脑实现；确认本次退出并结束授权，不恢复导航 |
| 大脑 | `GET /v1/vla-handoff/leases/{lease_id}` | 新增大脑实现；保留 request_id 查询原事务、observation_id 查询当前授权的区别；同样可查询大脑签发的 NAV 授权 |
| NAV | 原 `POST /v1/navigation/goals`、`resume` 及原业务 GET | 复用；派发关联大脑授权，NAV 保存并校验，不再自行申请第二份授权；resume 使用新命令对应的新授权 |
| NAV | `POST /v1/navigation/handoff/yield` | 新增薄入口；关联来源任务、命令和授权，让出本端输出，单独保存交接结果；不改写已成功业务命令 |
| NAV | `POST /v1/navigation/handoff/prepare` | 新增薄入口；关联目标任务、命令和大脑授权，准备导航执行条件，不开始导航任务 |
| NAV | `GET /v1/navigation/handoff/requests/{request_id}` | 新增；查询原 yield／prepare 操作及未满足项，不重复执行 |
| VLA | 原任务提交、cancel、pause、finish、任务 GET、control/status | 复用；公开独立停止／退出结果，全局 owner 从大脑查询，不再按本端阶段猜测 |
| VLA | `POST /v1/vla/tasks/{command_id}/stop` | 采用操控侧建议的新增薄入口；只要求原任务停止输出，不隐式调用 cancel，不覆盖已有业务效果；原任务 GET 保存并返回对应 stop request_id 的结果 |
| relay（操控侧） | `POST /v1/relay/control` | 采用操控侧建议的新增入口；action 为 `close` 或 `enable_navigation`，复用关闭／释放和绑定／转发能力，禁止启动时默认转发与 Bool 自动裁决 |
| relay（操控侧） | `GET /v1/relay/status`、`GET /v1/relay/requests/{request_id}` | 新增；分别查询实际通道状态和原操作结果 |

控制服务地址使用 `BRAIN_CONTROL_BASE_URL`；导航事实查询仍使用原导航地址。保留 `/v1/vla-handoff` 路径是为了减少客户端改动，**不表示 NAV 继续裁决，也不表示兼容旧 release 语义**。能力声明沿用现有 contract_version，coordination_revision 改为 `20261023-brain-owned-v1`，增加 `authority=brain`；客户端必须核对新修订与权威。旧修订不得静默接入。

### 谁申请、谁结束

- **VLA**：大脑先登记待执行的 task_id／command_id，再按原接口提交业务任务；VLA client 调用 acquire，获批后在本端完成模式和起始检查，再执行。正常完成沿 finish 后退出流程，取消沿 cancel，单独让出沿 stop；三条路径复用底层停止能力，只结束一次授权。停止及退出事实先持久化、可查询，再调用 release，不能相互等待。
- **NAV**：大脑在首次导航或恢复导航前，为目标命令登记并签发授权，再组织 prepare、通道恢复和业务派发。NAV 不调用 acquire 重复申请；它按本次授权检查执行资格，并提供 yield／prepare 回执。大脑收到 NAV 让出及 relay 关闭事实后，通过自己的同一授权管理逻辑结束 NAV 授权，不再要求 NAV 另发一轮 release 才返回 yield。
- **relay**：只执行大脑的 close／enable_navigation，不能签发授权。引用被关闭或获准转发的任务、命令与授权版本；迟到旧请求不能关闭后来重新授权的通道。

NAV 的 goals／resume／prepare 请求新增最小 `control_authorization:{lease_id,authority_id,ownership_epoch}`，沿原 task_id／command_id 关联到大脑记录，不增加另一套业务身份。VLA 业务提交体保持原样，授权仍由现有 handoff client 处理。yield、stop、relay control 带原请求身份与对应授权；启动尚无授权或端口状态未知时先核清，不默认允许输出。

业务流程派发前，大脑须已获得“允许准备导航”的明确授权；这不等于导航已 ready。prepare 可以执行本端必要的模式准备动作，但不能开始走路，实际业务仍由后续 goals 触发。授权或回执未知均不继续派发。

共同关联信息尽量复用现有命名：

| 信息 | 最小约定 |
| --- | --- |
| 任务与动作 | 沿用 `task_id/command_id`；交接操作关联原命令，不另造一套业务任务身份 |
| 本次控制与本次请求 | 沿用 `lease_id/request_id`；lease 关联一次授权，request 区分申请、退出或通道操作，重查不换号 |
| 当前授权版本 | 保留现有 `ownership_epoch` 的作用；NAV 内部 generation 的差异在适配处对齐，旧回执不能恢复已结束授权 |
| 目标与实际状态 | 分开表达“请求给谁”和“大脑当前批准谁”；未知如实返回，不能默认恢复 navigation |
| 结果与原因 | 能区分处理中、完成、拒绝／失败、未知，并带未满足项；业务效果、停止和交接结果分别保留。现有字段能表达的就复用，不要求改成统一状态枚举 |

同一请求重复送达不能重复执行；同一身份但内容改变应拒绝。回执须能对应请求和授权版本；迟到结果不能覆盖后来的决定。这里只约定已有可靠性机制应保持的行为，不新增逐帧协议或独立心跳接口。

### 当前授权查询样例与重启

以下为接口测试示例，机器人、端点和身份均为占位值，不是现场授权。NAV 与 VLA 使用同一个当前查询合同；VLA owner 为 `vla`，NAV owner 为 `navigation`。

```http
GET /v1/vla-handoff/leases/<lease_id>?observation_id=check-001
```

```json
{
  "contract_version": "fq/vla-handoff/v1",
  "coordination_revision": "20261023-brain-owned-v1",
  "authority": "brain",
  "authority_id": "brain-instance-example-1",
  "robot_serial": "EXAMPLE_ROBOT",
  "action_endpoint": "tcp://192.0.2.10:5556",
  "task_id": "task-example",
  "command_id": "nav-example-02",
  "lease_id": "<lease_id>",
  "ownership_epoch": 12,
  "observation_id": "check-001",
  "owner": "navigation",
  "state": "authorized",
  "authorization_current": true,
  "updated_at": "2026-10-23T12:00:00+08:00",
  "blockers": []
}
```

客户端必须匹配原任务、命令、lease、机器人、端点、authority_id、ownership_epoch 和本次新 observation_id，且只有 `state=authorized`、当前 owner 与目标一致、`authorization_current=true` 才认定本授权有效。授权有效不表示模式、通路或业务动作已经就绪。当前查询禁止缓存，不改变授权；`?request_id=...` 返回历史操作结果，不能拿历史 accepted 或成功状态启动输出。

本授权已正常结束时返回 `state=released/authorization_current=false`。owner 表示大脑当前批准方，可以是 `navigation/vla/none/unknown`，与所查历史 lease 不一定相同；身份、版本或有效状态任一不符即不能据此执行。当前大脑实例无法核清授权时为 `state=recovery_required/authorization_current=false`，保留 blocker，不能默认 navigation。原查询的 task／command／lease 与 epoch 描述被查询的授权记录，不能替换成另一个命令使其看起来仍有效。

大脑持久化授权版本，新授权递增；每次控制服务启动更换 authority_id，旧记录只作恢复核查，不自动恢复授权。恢复期间所有旧授权当前查询均为无效，当前 authority_id 也与旧回执不同；原记录仍保留原签发实例供追溯。旧输出是否已经停止须另外核清，未确认前不批准下一任。端侧重启不自动重播历史请求或恢复发布，须按当前查询与本端实际状态核对；查不到、失联或实例变化时不得自行认定权限仍在。

## 5. 切换与连续导航

### 导航 → 操控

1. 导航报告到位及停止等事实，大脑决定下一步执行操控。
2. 大脑组织导航让出输出、relay 关闭转发并释放端口，核对各自的实际回执。
3. 旧控制源退出条件核清后，大脑允许 VLA 接管；VLA 完成自己的模式与起始条件检查，再执行已下发的动作。

首次导航也必须受大脑当前控制决定约束，不能因为没有上一个任务就自行取得权限。

### 操控 → 下一次操控

1. VLA 保存本次效果、停止与退出状态，结束本次授权。
2. 大脑核验本次结果，决定继续下一个操控动作，并重新批准该任务的控制申请。
3. VLA 重新核查起始条件并执行下一动作。第一版沿用每任务独立启动、停止 worker 的方式，不要求复用连接。

**开门 → 抓可乐，中间不恢复导航。** 下一次操控仍需核查原导航到位证明、当前站位、导航无活动输出及本动作条件，但不能再要求“导航转发通路已经恢复”。

### 操控 → 导航

1. VLA 报告本次动作结果、停止、端口释放及退出状态，大脑核清原授权结束。
2. 大脑为目标导航命令批准准备权限，再组织 relay enable_navigation 与 NAV prepare。**接口必须分开“端口已绑定、允许转发”和“已收到新鲜上游数据”**：若 NAV 准备需要先接通 relay，relay 不得等 NAV 已 ready 或已有新帧才报告绑定完成；NAV 再执行准备并提供真实模式回执。是否存在相反的原生模式前置条件，由两端在执行适配时共同核实，若有须反馈给大脑调整这一跨端顺序，不能各自更改或假定已通过。
3. 必要的模式、保持和通路条件确认后，大脑再下发导航任务。抓取后的运送还必须满足真实持物条件。

**恢复 relay 前的共同前提：NAV 旧授权输出仍处于停止／封禁状态。** 两端先落实旧队列清理或等效隔离；relay 绑定完成不能把旧缓存帧算成新准备数据。只有为新授权启动的准备输出可进入已恢复通道。具体封禁、清理和新鲜性证明由两端实现，缺证据则阻断该操作顺序。

### 导航 → 下一段导航

每段业务导航仍绑定自己的命令和授权。第一版采用同一套已有交接操作：上一段到位后保留原成功结果，NAV yield 核清旧输出，relay close 核清通道，随后大脑结束旧授权；为下一段分配新 command／lease／递增 epoch，再按导航恢复顺序 enable_navigation、prepare 并派发 goals。relay 更新到新命令和新授权版本，迟到的旧 close 不能关闭它。

**这不要求切到 VLA，也不要求反复切换 SONIC 物理模式。** NAV prepare 若确认原模式及所需保持条件仍然有效，可复用该状态；不能仅凭上一段到位就推断发布者已退出。第一版不额外增加“原地替换授权”的专用接口；NAV 内部是否保留原发布进程由本端决定，但旧命令输出必须结束或被可靠封禁，不能沿用旧授权继续发送。

## 6. 回执必须把哪些事实说清楚

- **业务完成、原输出停止、授权结束、下一方准备完成分别表达。** 开门成功不等于已经交接，交接失败也不能抹掉已发生的开门效果或自动重做动作。
- **停止回执独立返回。** 执行端核清本端停止后即可报告，不等待大脑先完成 release；大脑也不等待 VLA 整个业务接口先结束，避免双方互相等待。
- **各端报告自己的事实。** NAV 不替 relay 证明端口已释放；VLA 停掉工作站发布者不能单独证明身体和双手已安全保持。必要的身体、手部退出与保持检查由执行侧负责，大脑接收结果和未满足项。
- **失效授权不能继续影响机器人。** 各端优先复用已有控制与停止机制落实这一要求；网络超时、端口空闲或进程退出不能单独当作交接成功。若现有机制做不到，列出具体缺口和最小补改，不默认扩大成整套数据链重构。
- **异常保留原身份和事实。** 丢回复查原请求，失败或未知时不自动抢占、恢复导航、重放动作或清除记录；旧请求和重启不能重新激活已经结束的授权。

原有暂停、取消、人工跳过和效果确认的业务语义不因本文自动改变。取消大脑任务仍不能宣称机器人已停止；如实施中需要改变这些行为，应单列说明，不能隐藏在控制权迁移中。

### 取消与旧控制恢复

2026-10-26 按用户要求补齐联合模式取消后的资源收尾。Runtime 仍立即结束本轮编排，BodyControl 将当前授权失效并用原工作器独立核清：NAV 已提交业务时先发送一次原命令 cancel 并查询停止，再走原 yield／relay close；VLA 使用原 stop，允许其正常退出回调 release。取消本轮不恢复导航、不启动下一轮，不清除原失败。API 与按钮唯一操作说明见[大脑任务控制](接口说明_大脑%20Brain.md#12-任务控制接口)。

收尾复用原 lease、epoch、原 release／stop 与子请求 ID；失联或未知后只查询已发送的操作。再次人工核查可追加独立恢复观察，保留上次失败回执。已确认的当前退出事实才结束授权；重启后的旧工作器不能写入新 authority 的记录，迟到旧来源不能影响新来源。新任务在登记 Runtime 前检查未结束控制，避免被旧授权阻断时又创建一份未知执行记录。

对“失败 prepare、业务未提交”提供受限核销：读取原 prepare 的身份和执行记录，原 native_steps 必须明确为空；NAV 当前无活动命令／交接、navigation_stopped／output_fenced／hold_verified=true，关联不属于其他源。随后仅关闭原关联 relay，再复核当前 NAV／VLA 退出和同一当前 controller，才记录 exit_confirmed。原生步骤不空或原查询不可查继续阻断，不用缺命令／端口空闲替代实际退出。后台不删除原 prepare／yield 失败，核销只是追加退出证明。

同日补齐整机重启场景：默认核对原 controller；若发生变化，须从已认证原请求回执取得合法的 `boot UUID:PID:start ticks`，原 controller 与原发布者 owner_instance 属于同一个旧 boot，当前 controller 属于另一个合法 boot；原请求明确 historical=true，嵌套执行记录的任务／命令／request_id／授权关联仍与原发送一致。不同 boot 确认旧启动周期的进程不能继续存在，再结合当前 NAV 输出封禁、无活动操作、停止／保持和 VLA 同实例证明核销。保存独立 restart_exit_evidence，不改写历史失败。同一 boot 只换 PID、未知 owner、格式错误、核查期间当前实例变化均不能套用；接口未恢复时不执行核销。

后续收紧核销范围与就绪事实的区分：上述跨 boot 分支还要求大脑没有业务提交记录、原 native_steps 为空。它只结束旧启动周期已失效的 NAV 准备授权，复核当前 NAV 停止／输出隔离、无在途操作与 relay 已关闭、当前实例在核查中未变化；不要求尚未启动的新 VLA／O6 冒充就绪。记录明确 `physical_readiness_verified=false`，下一轮新授权仍经过原 `idle()`、保持与执行准入检查。同实例的退出核查继续要求当前保持和 VLA 退出证明。

另修复首次 `idle()` 观察失败即产生残留占用的问题：NAV 在发布授权时持久化 grant_published；若失败阶段仍为 acquiring、没有发布过授权、没有控制子请求或业务提交，只结束本地未取得控制的意图，保留原失败。对旧版本记录，仅在原 acquire 为本地 `STALE_EVIDENCE`、无任何 acquire 子请求／业务提交／已发布标记，且后续最多只有未确认的 stop-only yield 查询时，按原实现阶段证据核销；不能将存在控制写入或业务提交的未知结果套用。失败信息标明 NAV／VLA／relay 来源，避免所有新鲜度错误只显示“本端”。

未下发业务且没有任何控制子请求、只因旧源未释放而被阻断的非当前记录，可在收尾后记录未取得控制并结束；所有旧请求保留。仅当持久化记录确认业务没有提交且控制已核清时，大脑才向原 Runtime 返回本地未开始证据，供下一次人工继续创建新尝试，不把查询变成自动重试。

2026-10-26 补齐 relay 开启阶段的收尾：若原 acquire 的全部控制子请求只有 `relay-enable`，没有任何已落盘的 NAV prepare 或业务提交，则只核查 relay，不调用 NAV yield，也不要求 NAV 解除本轮未接管的身份。读取并严格匹配原 relay 开启回执；未知、处理中、不可查询或身份不符继续阻断，不重发 enable。原开启明确失败、回执确认未绑定／未转发／端口已释放，且新鲜状态确认 relay 已关闭、无需恢复、订阅队列已关闭时，可以保留其历史 applied_authorization 并结束本轮准备授权。否则只允许关闭与本轮身份匹配的 relay，沿原 close 请求查询结果；最后再次核对关闭、队列与同一 relay 实例。原失败和此前错误的 NAV yield 请求仍保留；退出证明明确 `navigation_prepare_not_sent=true`、`physical_readiness_verified=false`，不声称 NAV 曾执行或下一轮已就绪。只要 NAV prepare 子请求已落盘，即使没有回复，也必须按原 NAV 退出路径处理。

### 当前退出重新核验与已清除关联的 NAV 回执

2026-10-26 对接 NAV 新退出证明：当旧 prepare／yield 回包丢失，原结果继续保持未知。显式恢复请求可选择 `new_exit_check=true`，BodyControl 为同一 task／command／lease／authority／epoch 建立独立的当前退出核验请求，取得新的 yield request_id，不重放 prepare、不改写旧 yield。每份 lease 的该核验使用固定身份，重复调用只查询同一新请求，不不断换号。自动取消仍沿原停止请求收尾；网页「核清旧控制」显式选择当前退出核验。

NAV 成功 yield 后可清除本地关联。大脑接受当前关联明确为 null、阶段 yielded、effect_status=confirmed、historical=false，并携带完整停止／输出封禁／mode_idle_hold_fenced／保持证据的同身份完成回执。对 observation_only 退出证明，另核对 requested_association 为本轮、observed_association 属于同 authority 的更早 epoch、stop_command_sent=false、original_request_outcome=unknown、native_receiver_sealed=true；不把新退出证明改写成旧 prepare 未执行。身份不符、历史回执或缺少退出事实仍拒绝。

取得 NAV 当前退出证明后，沿同一新恢复事务关闭原 relay，并重新读取 relay 当前状态，确认身份、转发关闭、端口释放、无需恢复和订阅队列关闭，再释放大脑授权。NAV 证明不替代 relay 退出；新核验仍未知时不再回落到旧 prepare 的 404 猜测结论。

### 交接 HTTP 拒绝与原回包留存

2026-10-26，大脑在原控制子请求下保存 `observations.first_post`（方法、路径、HTTP 状态、原始响应体／通信错误与接收时间），每次原请求查询更新 `last_query` 和计数。首次 POST 不被后续 404 或重启查询覆盖；非 JSON 响应保留原文本，HTTP 回包不保存凭据头。控制错误及任务原始错误详情携带这些字段，页面优先显示首次拒绝原因；旧代码丢失的首次回包不能通过本次升级补造。

HTTP 拒绝和执行结果分开处理：未知通信、5xx、请求冲突及单独 UNKNOWN_REQUEST/404 都继续查询原 request_id，不重发 POST，也不据此认定未执行。有效拒绝回包会显示 `HANDOFF_REJECTED`，但一般拒绝不自动构成资源退出证明；原请求查询随后取得身份、时间有效的成功回执时，仍按真实结果处理，并保留首次错误。

对 NAV 当前共同接口在受理前明确抛出的有限错误，增加准备未接管判据：原首次 prepare POST 的合同／修订正确、accepted=false，HTTP 400 对应 INVALID_REQUEST／INVALID_AUTHORIZATION／UNSUPPORTED_CONTRACT_VERSION，或 HTTP 409 对应 HANDOFF_BUSY／NAV_NOT_YIELDED／STALE_HANDOFF；可选回显身份不得冲突，不能有成功／处理中或已执行证据。该判据只适用于本次新 prepare，不适用于 REQUEST_CONFLICT、任意未知错误码或 yield 的拒绝，更不声称旧控制源已退出。收尾还需核对 NAV 当前输出封禁、停止、无活动命令／操作，且未关联本轮；随后核对并关闭原 relay，保留原拒绝及关闭证据，以 `navigation_prepare_not_accepted=true` 记录。既有无原 POST 回包的未知 prepare 仍走原恢复核查，不因新的代码名单清账。

### VLA stop 的任务结果

stop 设置独立停止意图，使原任务循环进入已有退出流程；不能只杀 worker 而留下任务循环继续执行，也不能暗中调用 cancel。停止操作结果先独立保存并通过原任务 GET 查询；release 仍由原退出流程处理，重入时不得重复执行。

| 原任务情况 | stop 后的表达 |
| --- | --- |
| 已有成功或失败终态 | 保留原业务终态与效果，另加本次停止／残留检查记录；终态不能免除实际进程核对 |
| 动作尚未完成、效果未经确认，停止与授权结束均已核清 | 业务终态为 failed，错误码 `OUTPUT_STOPPED_BEFORE_COMPLETION`，business_outcome=unverified；这是任务被停止，不能解释为物体效果已证明失败或从未发生 |
| 效果已验证，正在正常退出 | 保留 effect_verified；在原成功条件及授权结束均满足后才完成成功终态，stop 本身不签发业务成功 |
| 停止、退出或授权结束仍未知 | 分别保留已知事实与 unconfirmed／恢复要求，继续阻断下一动作；不能因为停止请求受理就填写停止成功 |

未验证效果的物体／现场状态仍未知，保留相应恢复要求，不能因 failed 终态自动重做动作。显式 cancel 仍保留其原业务取消语义；大脑消费 stop 结果时分别读取业务终态和控制退出事实，不新增业务状态机。

## 7. 本轮修改与验收边界

本次已只读核查本机 `sonic_wbc` 操控工程，以及 G1 上的 `/home/dev/g1_navigation_migration_20260930/dream` 导航工程。现有 VLA 交接客户端可以复用；导航交接硬件适配、VLA 模式切换与持物保持仍有未完成或未验证项。它们需要执行侧明确报告，不能通过更换授权方宣称已解决。

两端的接口映射反馈已经收到；本轮保留的真实能力缺口如下，不再要求两端重写需求文档：

| 场景 | 待执行侧落实的条件 | 负责方 |
| --- | --- | --- |
| NAV → VLA | NAV 持续输出、模式命令、IDLE 和退出 HOLD 不再影响接管；VLA 自身模式准备与原生采纳确认 | NAV 落实旧源退出，操控落实接管；relay 自己报告关闭／端口状态 |
| VLA → VLA | 原 worker／发布者／双手程序的退出事实与任务间保持；NAV 维持让出；原到位证明结合当前站位检查 | 操控负责退出和再次启动，NAV 提供必要事实；保持未知则阻断 |
| VLA → NAV／首次联合导航 | 模式准备与 relay 绑定／转发的可执行顺序、真实模式确认及必要的手臂／手部保持 | 两端确认本端条件，大脑组织跨端调用；旧七轴手目标改写退出本轮双 O6 路径，不能视为已有替代保持 |
| 失联、撤销或重启 | 旧授权、旧进程及其他入口不能恢复旧输出；具体执行办法与失败回执 | 各端落实本端约束，大脑在未核清时不批准下一任 |

大脑不把 relay 进程存在、端点信息或 navigation_transport_ready 单独当作授权及接管证明。未知模式、保持或输出退出条件继续阻断对应真机动作。

离线验收至少覆盖三种切换、连续 NAV、初次导航、丢回复、重复请求、迟到停止／relay 指令、切换失败及重启；停止事实能在 release 尚未完成时查询，历史业务结果不被让出覆盖，VLA release 成功但导航未恢复应是合法结果；未完成任务 stop 后不得报告业务成功或自动重试。真机执行能力与离线接口通过分别验证。

本表是大脑根据两端反馈汇总的实施接线，大脑侧新增实现保持可选且默认关闭，不代表两端已实现或逐路径验收。后续只核对具体字段差异与执行能力缺口，不恢复已撤回的复杂协议。代码实现、服务部署和真机动作仍按用户授权范围分别进行。


## 8. 大脑侧已实现接线与离线验证范围

2026-10-23：大脑新增控制 API、持久授权记录和受控 BodyAdapter，复用原 Runtime／Runner／Verifier 及应用工作器。只对启用联合控制的真实 NAV/native VLA 路径生效；旧后端和操控模拟保持原行为。新联合模式每段动作都核查退出，人工跳过不能直接补齐授权。业务计划含尚未接入的动作时在整份计划派发前拒绝。

配置维护在 `reception_real.body_control`，模板见 [brain.yaml](../config/examples/brain.yaml)：`enabled` 默认 false；启用还需 robot_serial、action_endpoint、relay_base_url，原 dream_base_url／vla_base_url 沿用。大脑／VLA／relay 服务凭据来自 `BRAIN_CONTROL_TOKEN`（可用 token_env 指定环境变量名），跨端配置需满足操控侧现有至少 32 字符的要求；NAV 的 --api-token 不同时，用可选 nav_token_env 指向保存 NAV 凭据的环境变量。控制请求、NAV goals／resume 与业务查询使用相应 Bearer；VLA 效果核验仍用其独立凭据。鉴权请求只发送到配置的同一 origin，不跟随重定向、不使用系统代理；凭据不入账本。大脑必须通过标准 BrainApplication 启动，不能单独创建另一份授权器；单机进程锁拒绝同一本账的第二个写入者。

控制记录位于原任务目录的 `body_control.json`，与 Runtime 共用目录写锁，只保存授权、交接请求、证据和精确发出请求；不复制任务状态机。业务派发意图、交接及子请求均在网络写入前落盘。重启保留历史、换 authority_id、使旧授权失效；查询本身不重放或推进控制。交接 UNKNOWN 保持阻断，不自动重试或清账；已有可匹配的授权及控制器身份、实际退出核清后允许提交 release，但不重新激活原授权。缺少原接管身份的未知操作没有自动清除捷径，须先完成执行侧核清。控制服务生命周期与主应用一致，不独立部署。

生产凭据可通过 `BRAIN_CONTROL_TOKEN_FILE`／`NAV_CONTROL_TOKEN_FILE` 指定私有普通文件；大脑授权 API、控制调用及业务 HTTP 共用同一读取规则。文件优先于同名无 `_FILE` 的环境变量，每次请求重新读取；配置了文件但不可读、为空、权限过宽或为符号链接时拒绝，不回退到另一份凭据。若自定义 token_env／nav_token_env，则文件变量名为对应名称加 `_FILE`。`.env` 只保存路径，凭据内容不进入 YAML、任务账本或文档。该能力不改变授权与交接协议。

### 实际读取的最小回执字段

以下是大脑适配器已经检查的字段，供两端直接核对；不要求按这些字段重写内部执行机制。所有控制操作必须能核对 `contract_version/coordination_revision/robot_serial/action_endpoint/task_id/command_id/lease_id/authority_id/ownership_epoch/request_id/action` 和 `control_authorization`。允许平铺回显，或在对端持久化的 request 中完整回显；大脑先严格核对原请求，再读取 result／exit_state 事实，不能用本地请求填补对端未回显的身份。完成用 completed=true 或 state=succeeded/completed 表达，失败／未知如实返回；HTTP 受理不等于完成。 NAV 交接回执允许其已交付的最小身份结构：合同／修订、task_id／command_id／request_id／action 及完整 control_authorization，evidence 单独保存执行事实；不要求为适配而补造未回显的机器人字段。其历史标记、本端当前关联及阶段也要一致，重启后的旧完成回执不能恢复准备资格。

| 回执 | 大脑检查内容 |
| --- | --- |
| NAV yield | navigation_stopped、output_fenced、hold_verified 均为 true，可位于 evidence；保留原业务结果，mode_idle_hold_fenced 不能替代 hold_verified |
| relay close | bound=false、forwarding_enabled=false、action_port_released=true；端点与原请求匹配 |
| relay enable_navigation | bound=true、forwarding_enabled=true；不在此时强求上游已有帧 |
| NAV prepare | mode_ready=true 或 evidence.mode_confirmed=true；独立 hold_verified=true；navigation_motion_authorized=false 或 evidence.navigation_not_started=true 确认未启动业务。运送请求含 required_holding 时须传给准备检查并返回匹配 holding；posture_preserved 不替代保持证明 |
| 准备后的 relay status | task_id／command_id／control_authorization 匹配新授权，bound／forwarding_enabled／upstream_fresh 均为 true |
| VLA stop | policy_stopped、publisher_stopped、output_fenced、action_port_released 均为 true；可直接返回操作回执，也可由原任务 GET 的 stop_requests[request_id] 返回原停止回执 |
| VLA release | 请求的 exit_state 与原任务 GET 当前 exit_state 完全匹配，摘要按原 ensure_ascii=True 算法核对；exit_state 的 policy_stopped／publisher_stopped／output_fenced／action_port_released／hold_verified 均为 true |
| 当前空闲事实 | NAV 无活动命令且 output_fenced／hold_verified=true；VLA 无活动策略且 output_fenced／action_port_released／hold_verified=true；relay 已关闭。正在申请授权的本 VLA 命令可处于等待状态，但不能已输出 |
| 当前站位证明 | NAV status 的 proof_context 含原 dream_command_id／target_id，valid／pose_within_tolerance=true，以及 map_id／localization_epoch／navigation_epoch；还需 controller_instance_id。仍核查原 NAV 命令真实成功，不要求导航转发已恢复 |
| 当前持物证明（2026-10-26） | 联合模式运送使用 VLA `current_holding` 和 `holding_evidence`，不以旧业务 `holding` 单独放行；证明规则见下节 |

各状态和执行回执需带 robot_serial 及实际观测的 observed_at 或 updated_at。默认证据新鲜度 10 秒、控制操作有界查询 10 秒，可按模板配置；缺失、过期或身份不匹配均阻断，不能用本次 HTTP 查询时间冒充新的物理观测。VLA release 可以省略重复的 controller_instance_id 回显；若提供必须匹配原 acquire。预期实例始终来自大脑原接管记录，当前 VLA control/status 的 controller_instance_id 或 execution_facts.controller_instance_id 必须独立匹配，两处都提供时不得冲突；没有当前实例证明或控制器变化时，旧保持证据不能释放授权。VLA 原任务的退出回执还需关联原 control_authorization；其全局 control/status 可保留正在结束的同一活动命令，但 policy_running 必须 false，不能有其他活动命令。VLA 成功结果保留 policy_stopped，publisher_stopped 可平铺或位于 result.exit_state，重复提供时不得冲突；是否已结束授权由大脑自己的记录核实，navigation_port_ready 不再是该路径的成功条件。

授权 API 当前回执状态为 pending、authorized、released 或 unconfirmed；未登记的请求返回明确错误。capabilities 的 ready 只表示接口实现可用，physical_readiness_verified=false 明确不代表真机就绪。旧 acquire 的 navigation_proof／proof_context／controller_instance_id 等证据继续返回；release 去掉导航恢复条件但不去掉身份、摘要或退出校验。

### 当前持物证明

2026-10-26 接入操控的 `g1/o6-holding-evidence/v1`。该证明由操控将真实认证观察绑定到 O6 当前实例和目标代次，O6 验证签名及连续性；大脑只消费结果，不持有签名密钥，不签发或续期物体证明。证据有效不代表有持续物体接触传感器。

大脑在抓取后交接、每段运送准备前以及准备完成后业务目标派发前读取当前 VLA 状态，检查如下：

- `current_holding`、证明物体与 `required_holding` 相同；schema、`valid=true`、`signature_verified=true`，且无 `invalid_reason`；原观察和绝对期限均有效。
- 原机器人、task_id、抓取 command_id 对应本 Runtime 已确认退出的 `grasp_cola` 授权，anchor.association 保留原 VLA lease／authority／epoch／owner，不改写为新 NAV 授权。
- `execution_facts.o6` 含 `authenticated-holding-evidence-v1`，与证明中的 O6 接收实例、输出实例、当前 native 实例、原关联、目标代次、反馈代次及双手目标哈希一致；当前双手保持、停止及接收封禁仍须明确。顶层与 O6 内的证明必须一致，proof_id 与原证明内容摘要一致。
- 缺失、过期、反馈断档、目标或实例变化均阻断；异常原因和当前证明随原回执保留。导航准备前拒绝且未尝试控制操作时可明确记录未下发；若准备已发生，期限失效不撤销已经发生的准备事实，保留原命令和授权等待核清，不能换号重发。

历史抓取成功及退出快照保留；证明到期只影响当前运送许可，不倒改历史效果。两端现有查询均不能刷新 `observed_at`／`valid_until`。正式确认和人工复核入口见[大脑当前持物检查](接口说明_大脑%20Brain.md#当前持物检查与重新确认)。

离线用例与实际运行结果集中见[大脑控制授权离线验证](多端联调_大脑任务与恢复.md#brain-control-offline)。离线阶段没有部署或真机动作；之后本机大脑按用户授权完成[代码重载及控制 API 启用](多端联调_大脑任务与恢复.md#brain-control-live-apply)，未切换两端数据链路。模式、旧输出和身体／双手保持仍由执行端提供真实证明。


第一阶段已使用操控实际 HTTP／任务循环／授权 client 和 relay 管理代码对接；大脑已适配其持久请求与嵌套事实。**原连续 VLA 状态、新鲜流字段序列化两项已由操控修复，大脑移除 expectedFailure 后 34 项复验全部通过。** 版本、复现、具体修改位置与通过范围见[第一阶段对接记录](多端联调_大脑任务与恢复.md#brain-vla-peer-offline)。


NAV 当前事实输出接口与 required_holding 传递、VLA 导航查询鉴权已补齐并由大脑复验。VLA 的 NAV 查询使用独立 NAV_CONTROL_TOKEN 或私有 NAV_CONTROL_TOKEN_FILE，不回退使用大脑凭据；缺凭据仍由 NAV 拒绝。NAV 的 driver.observe() 提供当前物理快照，未知字段为 null，observed_at／updated_at 保留采样时间，status_generated_at 仅表示响应生成时间；真实观测源尚未接入。

已通过实际 Runtime 和三端 HTTP 完成导航、开门、连续抓取及四段运送的离线串联，51 项合并定向断言全部通过。物理执行／保持／观测和 relay 数据传输为替身，真实模式、旧输出封禁与身体／双手保持仍待实现验证，不能据此启用真机。版本、复现与证据见[完整三端复验记录](多端联调_大脑任务与恢复.md#brain-three-peer-offline)。
