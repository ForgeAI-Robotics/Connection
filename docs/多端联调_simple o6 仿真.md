# 多端联调：simple o6 仿真

维护日期：2026-10-21。维护 SIMPLE O6 场景与资产适配、服务接入、物理回合、网页接待和仿真证据的验证与排障。当前接口和启动方式见[SIMPLE 仿真接口](接口说明_simple%20o6%20仿真.md)；GR00T 接线、抓放与开关门效果见[操控与抓取](多端联调_操控与抓取.md)，真实 ROS 导航代码和定位问题仍归[导航与定位](多端联调_导航与定位.md)。

快速定位：[排障章节导航](#排障章节导航)。以下完整内容从原模块按职责移动，参数、命令、失败与复验记录保留；历史正文整理不代表重跑；2026-10-21 新增实测见下方相应验证项。

## 验证概览

| 验证项／环境 | 证据日期与结果 | 适用边界／未完成项 |
| --- | --- | --- |
| 通用身体技能共用 NAV/VLA | 2026-10-21：导航 1/1、抓取复验 3/3、通用搬运复验 9/9；原接待对照 12/12 | 搬运复验经 3 次人工继续，首轮抓取与搬运失败保留；真机仅做离线接口验证。[通用技能](#generic-body-skills) |
| O6 原生单步抓取 | 2026-09-24：固定校准物体三轮抓取与持有；旧 O7 失败另存 | 固定场景通过不等于泛化，原 O7 失败不被后续 O6 成功覆盖。[P1](#e-p1) |
| Desk、MuJoCo、3DGS 及通用入口 | 2026-09-28：不同后端分别验证任务、恢复、持久化或观察 | 各后端的证明范围不同，不能合成“所有后端抓放通过”。[P2](#e-p2) |
| SIMPLE 连续接待物理回合 | 2026-10-08～09 的场景结果保留；10-21 新版五点完整接待 12/12、七个物理动作通过，桌前规划失败后一次人工继续 | 10-21 为恢复后完成，不是一次通过；验证了 LLM 读取状态／求助和原继续入口，未执行 LLM revise_plan 物理分支。[P3](#e-p3)～[P5](#e-p5)、[最新复验](#e-p5-recovery) |

## 范围与排障入口

- **场景或服务接入失败**：先查旧 O7／O6 资产与可达性、后端能力声明、健康接口和原命令身份；对应章节保留具体回包、坐标和场景版本。旧 O7 失败不能被随后 O6 成功覆盖。
- **物理回合不能连续完成**：按接待回合的配置、调参、物体持有／释放和停止证据排查；固定站位探测、单步抓取与完整接待分别验收。
- **网页显示完成但不能证明物理成功**：核对本回合任务、命令次数、物体状态和轨迹来源。离线回放不能当作实时相机；调用完成也不代表动作效果正确。
- **其他后端对照**：P2 同时包含 Desk、MuJoCo、3DGS 与 SIMPLE 的通用入口验证，正文保留各自范围，用于识别后端误用；不将这些结果统一称为 SIMPLE O6 通过。
- **策略或控制问题**：GR00T 抓取失败、未核实的 place 执行路径和冰箱动作门限，在[操控与抓取](多端联调_操控与抓取.md#排障章节导航)直接查阅完整方法与失败证据。

## 待验证事项

- 固定物体或单次连续接待通过不能外推为换场景、换物体或长期成功率；新增实验保留失败和原物理回合身份。
- 原 O7 可达性失败没有因切到 O6 而被修复；场景版本、机器人资产和接口能力须与当前运行实例核对。
- SIMPLE 的仿真真值、物理轨迹及回放证据不能替代真实传感器、实体动作或在线视觉核验；不同环境分别判定。

## 排障章节导航

下列章节可直接查阅环境与配置、复现方法、错误原文、修复、复验和证据位置。日期标在章节内；其中“本轮”“当前”“已启用”等措辞指对应证据日期，实际操作以接口说明和当前实例为准。2026-10-21 的历史正文整理未重跑旧实验；当日新增的完整接待物理复验单独见下方条目。

- [旧 O7 失败与 O6 抓取接入](#e-p1)（证据日期：2026-09-24）
- [多后端抓放、观察及任务持久化](#e-p2)（证据日期：2026-09-28）
- [连续接待物理回合：接入与调参](#e-p3)（证据日期：2026-10-08）
- [正式网页接待：动作次数与物理核验](#e-p4)（证据日期：2026-10-09）
- [新办公室物理接待：场景适配与失败重试](#e-p5)（证据日期：2026-10-09）
- [新版五点接待与异常恢复复验](#e-p5-recovery)（证据日期：2026-10-21）
- [通用身体技能与接待共用执行链路](#generic-body-skills)（证据日期：2026-10-21）

<a id="generic-body-skills"></a>

## 通用身体技能与接待共用执行链路

证据日期：2026-10-21。修改基于 `941d502` 后的本地工作区；复用 `BodyAdapter`、NAV/VLA v1 合同和现有 Runtime。
真实模型为 DeepSeek `deepseek-chat`，SIMPLE 为 office_v2、table 源、seed 605、五点 Skill v2、完整抓放配置。
新通用任务经真实 LLM 选择与编译；测试脚本为 `scripts/simple_reception_experiment.py`，
独立账本在 `data/experiments/body-skills-20261021/`，未应用主服务环境或开启真机动作。

远端只更新 `reception_http.py` 和 `reception_store.py`，先通过 `manage.py stop` 检查无在途命令，
备份为 `/home/fangqi/connection-simple-o7/runtime/body-plan-backup-20261021-181446/` 后启动原容器。
没有改抓取阈值、控制算法、场景或种子；回合结果不能外推为长期稳定性。

复现示例：

```bash
venv/core/bin/python scripts/simple_reception_experiment.py run \
  --ledger data/experiments/body-skills-20261021/nav --task '去二号桌'
venv/core/bin/python scripts/simple_reception_experiment.py run \
  --ledger data/experiments/body-skills-20261021/transfer --task '把二号桌的可乐抓起来，搬到一号桌并放下'
```

| 案例 | 原任务与结果 | 物理证据 |
| --- | --- | --- |
| 去二号桌 | `simple-reception-20261021-181502`，1/1 成功 | `body-nav-table2-261021181502`，到达误差 2.82 cm；没有抓放命令 |
| 导航后抓取首轮 | `simple-reception-20261021-181735`，导航 PASS、抓取 FAIL，任务 recovery_required | `body-pick-261021181735`：`lift_hold: state completion timed out after 8s`，抬升 8.08 cm、接触 true、无支撑，但 `lifted_gate=false`，不能算稳定抓持 |
| 通用搬运首轮 | `simple-reception-20261021-182021`，抓取通过；门前段两次人工继续后 PASS，横移段触发倾斜保护，任务 recovery_required | 原门前误差 14.52 cm，第一次重试 9.79 cm，均未达 8 cm；后续横移报 `base_tilt_exceeded_20_degrees`，不重试安全失败 |
| 原开始接待对照 | `simple-reception-20261021-182749`，12/12，7 个物理动作均首次 PASS，0 重试、0 人工跳过 | 五段误差 2.82 / 3.30 / 4.37 / 2.27 / 2.00 cm；抓取抬升 10.66 cm；放置误差 5.29 mm，释放且最终持物为空 |
| 导航后抓取复验 | `simple-reception-20261021-183424`，3/3，导航与抓取首次 PASS，0 重试 | 目标仍为 `cola_can_1`；抬升 11.22 cm、接触 true、支撑 false、`lifted_gate=true`，末段抓取完成后回合正常结束 |
| 通用搬运复验 | `simple-reception-20261021-183634`，9/9；7 个物理动作、10 次尝试，3 次人工继续、0 人工跳过 | 抓取抬升 11.20 cm；最终五段导航误差 2.82 / 5.76 / 6.07 / 4.54 / 2.92 cm；放置误差 14.04 mm，释放 true，持物为空 |

首轮失败回合为远端 `runtime/reception_episodes/simple-reception-20261021-181735-e444876b/`。
回合已结束，操控返回 `retryable=false`；没有在该回合重抓、改成功或清除失败。
LLM 提出 `observe_scene` 建议，未确认派发；其文字不是物理根因证明。
通用搬运的 `body-nav-relay2-261021182021` 与 `-r1` 均记录 FAIL；只在核实停止、持物保持、
`simulation.retry_allowed=true` 后由测试操作者调用原 `continue`，`-r2` 通过。此为人工测试恢复，不是 LLM 自动执行。
`body-nav-relay3-261021182021` 触发机身倾斜保护，回合不可恢复；没有新命令越过保护。
随后使用相同配置的新任务做对照，旧失败仍保留。每条完整 HTTP 回包在各账本 `remote-commands.json`，
汇总在 `verification.json`；这些数据被 Git 忽略，只存本机，物理轨迹保存在远端对应回合。

模型边界检查发现“搬运后开灯”曾出现说明拒绝、动作列表却只保留搬运的矛盾输出。
新增 `supported` 与 `unsupported_requirements` 的结构化要求，明确未完整支持时拒绝整份计划；
修正后真实模型 8/8 案例符合预期。保留修正前 JSON 与修正后回包，见 `model-boundaries-before-fix.json`、
`model-boundaries.json`。这是本组案例结果，不证明自然语言理解永不出错。

离线覆盖原命令身份、导航证明、停止交接、未知能力／参数拒绝、真实路由动作许可、重建查询、
人工继续、模型迟到及取消。真机路由在 localhost 假执行服务验证，没有向实体导航或操控发指令。
原接待 SOP 的请求合同回归保持一致；本次完整离线回归 632 项和 DREAM 自检 27 项通过，日志 `offline-tests.log`。原接待物理对照一次通过，不用后续成功覆盖前述通用任务失败。
通用搬运复验的门前段原命令 `body-nav-relay2-261021183634` 误差 14.09 cm，`-r1` 为 11.45 cm，
`-r2` 为 6.07 cm 才通过。`body-nav-table1-261021183634` 在 `office_nav_table1__carry_clearance`
规划失败，保留停止与持物证据后由操作者继续，`-r1` 到达误差 2.92 cm。
最后 `body-place-261021183634` 放置 PASS，原导航证明引用成功的 `-r1`。
两次门前恢复及一次桌前恢复均由测试操作者调用原继续入口，`plan_revisions` 为空，不能写成 LLM 自主恢复。

本组共 6 个独立任务回合：4 个成功、2 个失败回合保留；29 次物理命令尝试中 7 次 FAIL，22 次 PASS。
四类请求均有成功记录，但不代表固定种子下每轮一次通过，抓取、门前到达和带物过门稳定性仍待执行侧完善。
没有修改物理控制算法、到达阈值、抓持门限或场景来制造成功。采集结束时所有测试回合均已结束、NAV/VLA 无活动命令；
远端两份部署文件与本地哈希一致。主服务 `config/execution.yaml`、`data/system/execution.json` 和
`config/brain.yaml` 前后哈希一致，真实环境没有应用新路由或执行动作。正式实例需另行重载及应用配置才使用新能力。
`manifest.json`、`physical-evidence.json`、`live-config-before.json`、`live-config-after.json` 保存版本、回合与隔离核对依据。

同日提交前按用户要求复核正式生效条件：服务器工作区已是新代码，但已应用配置仍为旧通用执行不可用路由。
`Switcher.ledgers_idle()` 返回 `任务尚未核清，不能切换环境：paused`：
任务 `16db02793645466b82bc50f3224bc271` 的原命令为 `nav-table2-50f3224bc271`。
只读查询 NAV 已返回 `cancelled`、`navigation_stopped=true`，大脑仍保留暂停及 `command_unknown=true`；
不据下游停止自行结束用户暂停的任务，也不手改账本绕过切换保护。本次提交不包含正式实例重载或环境应用；
SIMPLE 部署已生效，正式通用真机路由须待原任务处理完后再应用。


<a id="e-p1"></a>

## 旧 O7 失败与 O6 抓取接入

证据日期：2026-09-24。本节保留对应条件下的完整排查与复验内容；后续变化以文首验证概览及相关问题的更新为准。


发生日期：2026-09-24；整理日期：2026-10-09。范围为旧 O7 失败排查、O6 原生抓取接入及大脑隔离账本实验。本文保留当日版本、结果、命令身份和证据，不代表当前全部能力；当前接口见[仿真接口说明](接口说明_simple%20o6%20仿真.md)。

<a id="e-p1-s1"></a>

### 旧 O7 失败与服务验证

已经由本机现有 Planner 为“抓起桌上的可乐并稳定持有”生成单步 `抓取 coke`，经 Runtime 提交到远端。
首个真实命令为 `simple-o7-1-bd114bc9d8b0`，远端 episode 为 `cbe03d2cbc45d503fef94252`。
本机账本在 `data/experiments/simple_o7/first-20260924/`。

当时 O7 场景版本 `2.0.0`，manifest SHA256 为 `69398b2707c41c03ba9ca59df01d5a6a05b986e0adda96a8323b94957eabc60d`。
按原脚本默认参数，机器人基座约 `[0.7, 1.2, 0.7826]`，可乐约 `[1.4706, 1.1929, 0.8477]`。
六个抓取姿态都未满足可达性阈值，末端位置误差约 0.22–0.36 米。首次规划报 `No eligible grasp endpoint`。
服务随后补充直接识别该前置失败，回包原因是 `no_eligible_grasp_endpoint`，不会继续启动 CuRobo 和物理抓取。

大脑进入 `recovery_required`，未写入持有事实，没有物理抓取成功记录。
这证明了远端调用、失败证据和 Runtime 停止边界；**不代表成功抓取已验收**。
当时读取的 O7 场景文档也没有给出该资产配置的完整抓取验收。
该 O7 场景的可达性问题尚未修复。本次随后识别并接入了既有的 O6 原生任务，结果见下一节；没有修改其控制算法或场景资产。

离线测试覆盖原命令幂等、冲突拒绝、HTTP/账本重建、超时与丢回包、取消、伪成功、真实证据核验、同任务不重置、环境路由隔离。
这一轮的 O7 成功路径使用假执行服务验证，O7 真实服务未通过物理抓取。
完整回归命令为 `venv/core/bin/python scripts/run_tests.py`，当时结果为 310 条测试和 27 项 DREAM 自测通过。

另外已在真实服务上验证：

- 准备阶段进行中取消：`simple-o7-1-ef9a26782884` 最终为 `cancelled`，`stopped` 和 `resources_released` 均为 true；本机 Runtime 同样到达 `cancelled`。
- 重启我们自己的服务容器后，仍能查询首个失败命令，返回相同 episode。
- 再次提交首个命令的原始载荷，返回原失败记录，没有创建新 episode 或重新执行。
- 对照记录在 `data/experiments/simple_o7/validation-20260924.json`。只重启了我们新增的容器。

<a id="e-p1-s2"></a>

### O6 接入与三轮完整大脑实验

<a id="e-p1-s3"></a>

#### 本次定位到的差异

1. 面板此前仅加入了配置选项，没有注册远端服务卡片，运行中的旧面板也尚未重载。已补齐环境层卡片、日志、启动/重启/停止，并实际重载和浏览器验证。
2. 同一 `SIMPLE-o7-verify` 路径仍保留 O7 入口；O6 的完整执行代码位于 `generated-data/o6-planner-production-20260923/src`。仅沿用普通 `src` 导入路径不能使用这套完整 O6 任务。
3. O6 原生入口、物体资产和核验条件均与 O7 不同。适配器按当前服务能力生成 `抓取 can`，身体执行与物理证据使用 O6 合同；旧 O7 记录仍可按原命令查询。

<a id="e-p1-s4"></a>

#### 可复现配置

远端独立目录的 `config.json` 选择 `robot_variant=o6`，`o6_overlay=generated-data/o6-planner-production-20260923/src`，默认 `seed=101`，阶段超时 600 秒。
工作进程将该目录放在子进程 `PYTHONPATH` 最前面，执行既有的脚本：

```text
/workspace/simple/.venv/bin/python /mnt/simple/scripts/validate_g1_o6_cycle.py
  --data-root /mnt/simple/data --task grasp --seed 101
  --world-tracking --torso-feedback --tracking-iterations 10
  --plan-attempts 1 --max-frames 1800
  --output /opt/connection-simple-o7/runtime/episodes/<episode_id>/execution
```

这使用既有 O6 任务参数，不编辑上游代码或物理模型。随机种子变化只用于独立新实验；本次没有测试不同摆位或不同物体。
每个 episode 保存源文件指纹、阶段日志、`report.json`、`diagnostic_trajectory.npz`、`final_physics_state.npz` 和执行模型。

<a id="e-p1-s5"></a>

#### 2026-09-24 实测结果

先进行一次 O6 原生抓取探针，种子 101，743 帧成功。随后由 Connection 的真实模型规划，经现有 Runtime、远端执行器和 Verifier 连跑三次：

| 实验账本后缀 | 种子 | 帧数 | 末帧抬升 | 连续稳定持有 | 最大受检穿透 | Runtime |
|---|---:|---:|---:|---:|---:|---|
| `o6-runtime-01` | 101 | 743 | 12.52 cm | 1.00 s | 1.686 mm | `succeeded` |
| `o6-runtime-02` | 102 | 740 | 12.16 cm | 1.00 s | 1.719 mm | `succeeded` |
| `o6-runtime-03` | 103 | 745 | 12.52 cm | 1.00 s | 1.284 mm | `succeeded` |

三次账本均位于本机 `data/experiments/simple_o7/`，每次只有一个命令，没有 `-r` 重试：

- `simple-o7-1-fdd1ccc749ae` → episode `b8169a11564b821b0342de8c`
- `simple-o7-1-b64c4f676193` → episode `1aeb0243be83a6fba4a2a764`
- `simple-o7-1-4dc6c1d399ad` → episode `7aec9c4d833e039599d029df`

汇总文件：`data/experiments/simple_o7/o6-validation-20260924.json`。这些是实际 MuJoCo 物理执行结果，不是假服务成功回包。
核验通过后大脑持有事实为 `can`。这只证明固定校准场景的抓取与稳定持有，不证明导航、放置、多步接待或泛化能力；原生报告的 `production_ready` 仍为 false。

本轮完整回归为 317 条测试与 27 项 DREAM 自测通过；随后增加物体身份回归，SIMPLE 专项 25 条通过。
覆盖接触丢失、桌面支撑、短时持有、快速运动、子步碰撞、NaN、错误任务类型、O7/O6 对象混用、停止时的新命令隔离。

<a id="e-p2"></a>

## 多后端抓放、观察及任务持久化

证据日期：2026-09-28。本节保留对应条件下的完整排查与复验内容；后续变化以文首验证概览及相关问题的更新为准。


验证日期：2026-09-28。范围为网页入口 → Brain → Planner → Runtime → 执行后端 → Verifier；未执行真机动作，未向飞书发送消息。四个后端的通过范围不同，不能把本记录解释为所有后端都通过抓取、放置及真机验收。

本文保留当日实验结果、修复过程和证据；环境、控制行为及未完成项均以当时版本为背景，不作为当前部署状态或操作规范。

<a id="e-p2-s1"></a>

### 1. 实测结果

所有下表任务均由正式网页 `:8888 /publish_task` 提交，使用大脑 `:5000` 的正式任务账本。环境切换通过运维面板的执行配置接口进行。

| 后端 | 任务与证据 | 结果 | 证明范围 |
|---|---|---|---|
| Desk | `desk-web-01-5333cf43`：抓取 `milk_1` → 放到 `milk_area` | `succeeded`，2/2 | 原始乱桌重新规划；牛奶已释放，最终位置约 `[0.5581, 0.6083, 0.75]` |
| Desk | `desk-web-03-cfe236ca`：抓取 `cola_1` → 放到 `drinks_area` | `succeeded`，2/2 | 前一任务结束后新任务可正常执行 |
| SIMPLE O6（配置名 `simple_o7`） | `simple-web-01-45e8ec11`：抓起罐子并稳定持有 | `succeeded`，1/1 | 正式入口调用远端 O6 原生抓取，核验物体抬升及末段稳定持有；不证明放置、导航或多步世界持续性 |
| 3DGS / MotrixSim | `gs-web-nav-04-85b6a482`：导航到 `(0.5, 0)` 并停止 | `succeeded`，1/1 | 实际底盘从原点移动至约 `x=0.3875`，停稳后核验通过 |
| 3DGS / MotrixSim | `gs-web-nav-05-5d121fb3`：到 `(0.7, 0)` → 到 `(0.1, 0)` | `succeeded`，2/2 | 两段分别停在约 `x=0.5269`、`x=0.2432`，前一步核验后才发下一步；使用原有 0.20 米到达容差 |
| MuJoCo / RoboCasa | `mujoco-web-04-03a91c1d`：抓取 `bread` → 导航到 `plate` → 放盘 | `succeeded`，3/3 | 完整 PandaOmron 厨房、正确后端的相机观察、导航及最终放置几何核验 |
| MuJoCo / RoboCasa | `mujoco-web-05-249b8bfd`：重置厨房后重复同一任务 | `succeeded`，3/3 | 从独立初始场景重新规划、执行，未复用上一轮完成状态 |

**物理真实性边界：** Desk 是抽象状态仿真；RoboCasa 当前沿用原有吸附抓取和坐标放置实现，验证流程与几何结果，不能作为接触抓取算法或真机操控精度的证明。SIMPLE O6 的证据来自其原生物理抓取轨迹。3DGS 本轮通过的是底盘导航。

<a id="e-p2-s2"></a>

### 2. 失败与尚未通过的范围

| 反例 | 观察到的事实 | 处理 |
|---|---|---|
| 3DGS 原地抓瓶子 `gs-web-grasp-03-751be4dc` | 物体约在 `[0.80, 0, 0]`，末端约在 `[-0.09, 0.49, 0.86]`；当前姿态下抓取未完成 | Runtime 为 `recovery_required`，没有记录持有，也没有下发放置。尚未验证导航接近后的完整抓放 |
| 3DGS 早期负 X 方向导航 | 机器人在约 `x=-0.098` 处无进展 | 保留超时失败；不能用后来的正 X 方向成功推断所有路径可达 |
| MuJoCo 旧轻量环境 | 资产缺失时启动的是只提供场景的 LiteKitchenEnv；动作调用报模型接口错误 | 补资产及依赖，后续任务使用完整厨房；轻量模式不计入抓放通过 |
| MuJoCo `mujoco-web-02-ff3c48b7` | 抓取通过，但 `counter` 无法解析，导航 400 | 修复工作点解析及规划提示；未执行下一步放置 |
| MuJoCo `mujoco-web-03-d037e2ba` | 抓取、导航通过；放置自报成功，但缺少几何证据 | 核验保持 UNKNOWN。补充真实物体包围盒，并修复把盘子当障碍而偏移落点的问题，再用新任务验证 |
| Desk `desk-web-02-5b605045` | 测试输入使用了不存在的 `cola_area`；实际区域为 `drinks_area` | 规划阶段拒绝，零动作；保留记录，修正任务输入后通过 |
| 模型临时连接失败 | 任务没有生成步骤，也没有发出身体命令 | 保留失败及正常取消记录；没有把前一个任务的步骤沿用到新任务 |

旧 Slaver 执行底座目前仍缺持久化命令查询和在线停止接口。部分失败实验无法由普通取消核清，因此先停止该实验的 Slaver、大脑和对应仿真进程，保存进程均停止的证据，再经 Runtime 取消状态机结束任务。原失败尝试未删除，也未改成成功。这不等同于完成在线中断、重启续跑能力验收。

<a id="e-p2-s3"></a>

### 3. 本轮修复

- 3DGS 底盘导航使用 `/nav`，修正 XLeRobot 底盘名称、驱动器映射、倒车接近与停止制动；导航核验读取实际位姿和速度。
- HTTP 返回错误、缺少明确成功标志或没有后端执行时，不再合并成动作成功。
- 3DGS 抓取需物体实际抬升且靠近手部；手部开合或单独移动不能证明持有。
- 厨房搜索和工作点只用于 RoboCasa。观察、搜索和原始动作请求跟随所选后端，不再误选配置列表中的另一个已启用后端。
- 保留感知配置的布尔和列表类型，`scan: false` 不再被字符串化后视为真。
- 规划使用当前场景目标；移除已不存在的 `counter_front` 等固定提示。修复重构后的导航地图路径，按实际厨房重新生成 254 个可通行点。
- MuJoCo 场景提供实际物体包围盒。放到盘子上的核验要求已释放、物体中心落在承接物范围内且底部高度接近其顶部；缺失几何证据仍为 UNKNOWN。
- 放置避让不再把请求落点正下方的固定承接物当作杂物。
- 厨房启动生成的初始记忆及后续记忆写入 `data/simulation/mujoco/`，不再覆盖版本管理中的初始模板。

DREAM/VLA 报文与真机动作许可未改变。

<a id="e-p2-s4"></a>

### 4. 环境与回归

本机完整厨房当前使用 MuJoCo `3.3.1`、RoboCasa 提交 `8f3c96ec8d1bfcd8126cad2bca887da98d30e997`、robosuite 提交 `5ce6643f3092639d08f7b0f90ed1c6a84f50552c`。PyPI 的旧 robosuite `1.5.2` 缺少当前 RoboCasa 使用的 `load_model_on_init`；仅看相同版本字符串不足以复现环境。

官方依赖源码在 `venv/deps/`，资产位于 RoboCasa 的 `models/assets/`。补齐了纹理、当前场景使用的 Objaverse 物体、Lightwheel 家具及装饰物；下载缓存在 `venv/cache/`，均由 Git 忽略。资产来源为 `robocasa/robocasa-assets` 和 `nvidia/PhysicalAI-Kitchen-Assets`。更换场景可能需要额外资产，不能将本次加载成功当作所有厨房布局均已安装。

```bash
venv/core/bin/python scripts/run_tests.py
venv/core/bin/python -m unittest tests.robot_api.test_simulation_truth -q
```

本轮完整回归 **336 条通过，DREAM 自测 27 项通过**；最终仿真真实性专项 **12 条通过**。离线测试不能替代本文件第 1 节的在线实验，也不能替代真机交接、停止及取消确认。

原始提交、任务状态、每步账本、场景快照、失败停止证据及测试日志保存在 `data/experiments/all-sim-20260928/`，文件以前述任务名前缀对应。本地实验数据不入 Git。

实验结束后，日常入口留在仿真模式：通用执行为 Desk、观察为 3DGS、接待为 mock；真机派发许可仍为 `false`。没有删除未核清任务来绕过环境切换。

<a id="e-p2-s5"></a>

### 5. 进入真机前的结论

本轮证明了上述有限任务能够通过统一大脑执行，并能在已测失败中阻止错误推进。**不据此宣布“四套仿真全部抓放通过”或“真机接待可以直接放行”。** 3DGS 完整抓放未通过；Slaver 在线停止及持久化命令查询仍有缺口。真机接待还必须按现有联调手册核实原命令查询、控制交接和停止证据。

SIMPLE 的能力边界见 [simple o6 仿真接口说明](接口说明_simple%20o6%20仿真.md)，真机步骤以 [控制面板启动流程](接口说明_控制面板.md#三端启动与任务发布) 为准。

<a id="e-p2-s6"></a>

### 6. SIMPLE 容器与原命令持久化补验

- 面板经真实 HTTP 操作调用 SSH 管理脚本重启了我们自己的容器，O6 健康检查通过。
- 三个原命令查询仍为 `succeeded`；重复提交各自原载荷时，episode 和更新时间均未变化，没有重放。
- 面板日志显示最近命令的结果摘要与阶段进度，过长的完整报告保留在远端 episode 中并显示路径。
- 主大脑默认环境仍为 Desk，真机动作许可关闭。此前阻塞切换的旧任务现已修复并成功续跑，新任务也验证通过，详见下方 Desk 恢复记录。

<a id="e-p2-s7"></a>

#### Desk 原任务恢复与切换阻塞修复

原任务不是执行器仍在移动：第一瓶牛奶已抓起，后续「导航到 milk_area」返回固定桌面的无操作回执，通用适配器却没有对应核验语义，因此进入 UNKNOWN。失败取消留下派发关闭标记；重启后适配器又丢失内存回执。原计划中的「将 milk_1 放置到 milk_area」也不被旧解析器识别。

修复范围：

- 规划提示按当前能力决定是否导航。Desk 计划在第一次抓取前完整校验并统一放置句式；已在固定工作范围内的区域导航从新计划中省去，不发身体命令。不支持的动作、物体或目标先拒绝。
- 已持有物体直接放置；即使其坐标已在目标区，仍需核验释放，不能因坐标重合就把整理判为完成。
- Desk 重启时只恢复 Runtime 已落账、任务/步骤/命令身份一致且确认停止与资源释放的执行回执。缺失、错配、超时或没有停止证据时仍保持 UNKNOWN，不能根据当前物体位置猜测旧命令成功。
- 旧 Desk 导航只有在原无操作回执完整、目标区域存在时才通过固定工作范围核验；此规则不作用于 DREAM 或其他仿真器。
- 显式续跑先查原命令。核清后 Runtime 才解除之前未完成取消的派发限制；更新的并发取消仍优先。确认调用已经停止但效果未知时，也允许正常取消，不伪造效果成功。

实测通过网页入口 `8888 /publish_task` 执行，没有手改任务状态或删除账本：

| 验证 | 任务 | 结果 |
|---|---|---|
| 原任务断点继续 | `34c23fa5bee2476a98e0d8f6ccdc9c3c` | `succeeded`，6/6；沿用原命令，无重抓、无重试 |
| 初始桌面重新规划 | `desk-milk-verify-814c68aacfff` | `succeeded`，5/5；四个抓放动作与一次只读核验，无导航 |

原任务续跑时未重启或重置 Desk，两瓶牛奶最终位于牛奶区，其他六件物体保持原样。
确认原任务成功并保存证据后，才重置 Desk 开展第二项新任务验证。新任务完成后：

- `milk_1` 位置 `[0.54795, 0.60808, 0.75]`，已释放。
- `milk_2` 位置 `[0.54590, 0.59247, 0.75]`，已释放。
- 夹爪为空，`blocks_new_motion=false`，环境切换的空闲检查通过。
- 飞书使用的共用 BrainClient 读取到同一成功任务；本次没有向飞书发送消息。

大脑服务已重启加载修复。运行配置仍为 Desk，未替用户切换到 SIMPLE。
完整回归 324 条和 DREAM 自测 27 项通过；最后补充放置句式后，Desk 恢复专项 6 条再次通过。
原账本快照、续跑结果、新任务计划及前后场景保存在 `data/experiments/desk-recovery-20260928/`。

正式网页 SIMPLE 任务 `simple-web-01-45e8ec11` 的原命令为 `simple-o7-1-eb0145e8ec11`，结果见第 1 节；这补足隔离账本以外的入口证据，不扩大当日单步抓取的验收范围。

<a id="e-p3"></a>

## 连续接待物理回合：接入与调参

证据日期：2026-10-08。本节保留对应条件下的完整排查与复验内容；后续变化以文首验证概览及相关问题的更新为准。


发生日期：2026-10-08；整理日期：2026-10-09。范围为地址迁移、连续接待后端接入、物理调参与大脑实验。来源为原接入记录、代码注释及 `data/experiments/simple_reception/` 账本；本次整理未重新运行仿真。

<a id="e-p3-s1"></a>

### 1. 环境与接入

<a id="e-p3-s2"></a>

#### 地址与环境

SIMPLE 主机地址改为 `192.168.31.69`（主机名 `fangqi-4080s`），容器、端口 18770、令牌和远端目录不变。
本机修改：`config/robot_api.yaml`（不入库）、`.env` 的 `SIMPLE_SSH_TARGET`、`src/ops/simple_remote.py` 与 `scripts/simple_o7_experiment.py` 的默认值、`config/examples/`。

现场发现并处理：

- 远端容器已运行 10 天，容器内 PyTorch 初始化 CUDA 偶发失败（宿主机内核日志 `NV_ERR_NO_MEMORY / Cannot allocate sysmem`）。账本核清后用 `manage.py` 重启我们自己的容器，CUDA 恢复；未动其他容器。
- 本机设置了桌面代理 `HTTP_PROXY=127.0.0.1:7890`，大脑访问 `.69` 被代理拦截成 HTTP 502。`shared/networks.py` 的 `apply_proxy_bypass()` 现在把 `robot_api.yaml` 中 SIMPLE 主机也加入 `NO_PROXY`（与各端 IP 同样处理）。

<a id="e-p3-s3"></a>

#### 远端已有内容的核查

`.69` 上没有现成可被大脑调用的“开始接待”服务。可用的物理能力有两部分：

1. `SIMPLE-o7-verify/.../sonic_release_v1`（2026-09-29 冻结）：O6 灵巧手 + SONIC v1.1 全身控制的 11 个 MuJoCo 任务，其中 `cross_table` / `multileg_carry` 是“抓取—持物行走—放到另一张桌”。
2. `~/g1-navigation-sim`（2026-10-08）：ROS g1pilot 规划器 + SONIC 的纯导航仿真，房屋场景，没有物体和操控。

因此接入方式为：在我们自己的独立服务上实现与真机相同的 `fq/reception-lan/v1` NAV/VLA 接口，后面驱动同一个连续的 O6 + SONIC 物理回合。上游代码只读导入，没有修改。

当前协议、部署和恢复规则统一见[仿真接口说明](接口说明_simple%20o6%20仿真.md)，本文仅记录当日变更。

<a id="e-p3-s4"></a>

### 2. 失败与调参

- 取物站位用 x = -0.62：上游任务从 -0.68 出生，SONIC 起步站稳后实际在约 -0.615 开始抓取。走到 -0.69 时抓取中基座后退把罐子带落（probe reception-2）。
- SONIC 在距目标 3 cm 内开始刹车并继续滑行 1–10 cm。取物桌的直线接近目标提前 4.5 cm；放桌的最后接近改为“粗接近 → 站稳 → 至少 12 cm 外的修正一步”，否则会落在上游 6 cm 完成半径内而不移动（brain-run-03）。
- 中继站稳必须带该段朝向；`StandSpec` 默认朝向 0 会让机器人原地转向（probe reception-4）。
- 横移改为向左，使最后一段与上游已验证的 `x = -0.90` 通道一致。

物理执行不是逐次确定的：CuRobo 在 GPU 上规划，同一种子的抓取轨迹有细微差别，之后的行走会分叉。单次成功不代表成功率。

<a id="e-p3-s5"></a>

### 3. 大脑任务结果

以下状态来自各轮保存的 `current_task.json` / `status.json`，是当时快照；不把未保存的后续处理推断为成功。

| 轮次 | 任务 | 保存状态与结果 |
| --- | --- | --- |
| brain-run-01 | `simple-reception-20261008-181410` | `cancelled`，停在首段导航，派发计数为空；仅凭此账本不补写取消前的唯一根因 |
| brain-run-02 | `simple-reception-20261008-181533` | `recovery_required`，到 table1 导航核验失败，未派发 place |
| brain-run-03 | `simple-reception-20261008-182143` | `recovery_required`，到 table1 导航核验失败，未派发 place；调参依据见上节 |
| brain-run-04 | `simple-reception-20261008-182530` | 18:28:41 账本为 `succeeded`；六个动作各派发一次，`safe_idle=true`、`holding=null`、`object_location=table_1` |

证据目录：`data/experiments/simple_reception/brain-run-01/` 至 `brain-run-04/`，含任务账本、事件、状态及命令索引。最终通过的是同一连续 MuJoCo 回合的固定单罐接待；前三轮仍作为失败或取消记录保留，不把最后一次成功当成全部实验一次通过。

<a id="e-p3-s6"></a>

### 4. 验收边界与后续

本轮证明固定场景的完整接待链路可通过真实物理测量核验，不证明换物体、换场景、成功率或真机动作。接待仿真未接入实时相机，视频回放不能当作在线视觉核验。正式网页的独立复验见[2026-10-09 网页接待记录](#e-p4)。

<a id="e-p4"></a>

## 正式网页接待：动作次数与物理核验

证据日期：2026-10-09。本节保留对应条件下的完整排查与复验内容；后续变化以文首验证概览及相关问题的更新为准。


发生日期：2026-10-09 上午；整理日期：2026-10-09。范围为正式网页 → 大脑 → SIMPLE 连续物理回合 → 核验及视频导出。本记录依据已保存实验数据整理，不是本次文档整理重新执行的测试，也不是同日下午的真机导航联调。

<a id="e-p4-s1"></a>

### 1. 环境与流程

任务：`147ae249f28045fdb075947bce3434a9`；入口 `web`；执行后端 `reception_simple`。证据保存在 `data/experiments/simple_web/20261009-094004/`。

| 动作 | 派发次数 |
| --- | ---: |
| 导航到 table2 | 1 |
| 抓取 | 1 |
| 导航到 relay2 | 1 |
| 横移到 relay3 | 1 |
| 导航到 table1 | 1 |
| 放置 | 1 |

<a id="e-p4-s2"></a>

### 2. 结果与物理证据

09:45:22 任务账本为 `succeeded`，进度 11/11（包含本地及核验步骤，实际身体动作六条）。各动作只有一次尝试，`has_failures=false`，无人工跳过，`safe_idle=true`，持有为空，物体位置为 `table_1`。

放置原命令 `vla-place-947bce3434a9` 的测量回包包含：目标桌支撑为真、手部接触为假、放置门通过、平面误差约 0.00559 米、手罐距离约 0.19163 米；`evidence.source=simple_o6_physics`。这些是 MuJoCo 回合中的物理结果，不是真机测量。

浏览器错误记录为空。实验保存的恢复检查显示原配置、原大脑 YAML 和原任务账本恢复一致，后端恢复为当时的 `reception_real`；此项仅描述实验收尾，不代表此刻的运行配置。

<a id="e-p4-s3"></a>

### 3. 证据与边界

- `acceptance.json`：入口、派发计数、结果与恢复检查。
- `web-result.json`、`tasks/current_task.json`、`tasks/events.jsonl`：任务与原命令核验。
- `episode/segments.json`、`episode/provenance.json`、`episode/trajectory.npz`：分段物理结果、来源与轨迹。
- `episode/web-reception.mp4`、`video-metadata.json`：由本轮完整 qpos 轨迹回放生成，1280×720、25 fps、97.08 秒；不是实时相机视频。

文件均相对于上述实验目录，实验数据不入 Git。本轮证明固定场景的正式网页接待可完成，不证明泛化、长期成功率、在线视觉或真机交接。当前用法见[仿真接口说明](接口说明_simple%20o6%20仿真.md)。

<a id="e-p5"></a>

## 新办公室物理接待：场景适配与失败重试

证据日期：2026-10-09。本节保留对应条件下的完整排查与复验内容；后续变化以文首验证概览及相关问题的更新为准。


发生日期：2026-10-09 下午。范围为定位新办公室资产、将其接入现有连续 O6 回合、正式网页“开始接待”和物理轨迹回放。本轮不是当天上午的旧房间实验，也不是真机实验。

<a id="e-p5-s1"></a>

### 1. 场景与实现边界

在仿真服务器找到 `office_video_v2/reports_hq/office_navigation_123_1080p_pip.mp4`，对应办公室走廊、取物桌和会议室。该演示的五个导航目标均报告到达，但它的场景准备脚本把可乐罐变成静态装饰，不能据此证明连续物理抓放已接入大脑。

本轮场景为 `office_v2`，来源 `office_video_v2/scene/scene.xml`，XML SHA256 为 `c326d734fb05b00f3b1d3faf6a3de7d296874de7efe5e3beac41b798481215aa`。资产快照保存在仿真服务自己的 runtime 目录，未改写对方项目。沿用同一 Reception HTTP、命令存储和单物理回合工作进程，Runtime 仍是大脑任务权威。

导航沿显式办公室路点运行 O6 SONIC 测量闭环，不是对方 ROS 导航演示的在线任务客户端。物体使用已校准的动态 `graspnet1b:2` 汤罐，合同名仍为 `cola_can_1`；不宣称已验证可乐罐。取物支撑改为保持 O6 标定近边和 0.75 米台高的 0.85×0.79 米桌面；会议桌保留来源占地及 0.75 米台高。门墙碰撞保留，未通过扩大门洞或关闭碰撞实现通行。

配置、坐标变换与调用见[仿真接口](接口说明_simple%20o6%20仿真.md#新办公室场景office_v2)。本轮固定种子 601，不证明不同场景或种子的稳定成功率。

<a id="e-p5-s2"></a>

### 2. 接入探测与失败保留

以下三个物理探测在正式网页任务之前执行，保存在仿真服务 `runtime/office-probe-01`～`03`，并归档到本地 `data/experiments/simple_office/office-probe-01`～`03`，不是大脑任务。

| 探测 | 结果 | 后续处理 |
| --- | --- | --- |
| 01 | 桌边导航通过；抓取阶段 `target_fell_below_tabletop` | 恢复取物桌的校准近边、台高和接近站位 |
| 02 | 抬起形成抓持，但 `transport_clearance` 发生 IK 失败 | 取消过大的向身后收手，仅增加 0.14 米高度 |
| 03 | 桌边导航、抓取通过；门前终点误差 0.081315 米，超过 0.08 米门限 | 保留失败，接近瞄准点前移 4 厘米；不修改真实目标和验收门限 |

第三次的瞄准补偿未保证下一次一次通过，后文保留正式任务的实际失败；不能把该调整称为已解决所有停步漂移。

<a id="e-p5-s3"></a>

### 3. 正式入口任务

通过控制面板切换接待模块到 `reception_simple`；Brain、Web 生效修订为 `b44511d415c7428aa9b5120b45f8fce1`，导航和操控健康响应都标识 `office_v2` 和上述来源 SHA。真机派发许可关闭。部署前服务源码及配置备份为仿真服务 `runtime/pre-office-deploy-20261009.tar.gz`，原命令记录未删除。

17:49:09，经网页 `/publish_task` 提交一次“开始接待”，任务 ID 为 `office-web-7f77aab69f4844f9a12bb1e2af5bff46`。证据根目录为 `data/experiments/simple_office/`。

- `20261009-web-01/`：首次受理、配置、原命令、任务与事件快照。导航到取物桌和抓取通过；抓取抬升 0.113378 米。
- 17:53:08，门口导航原命令 `nav-relay2-b1e2af5bff46` 被大脑判为 `NAVIGATION_EXECUTION_FAILED`：位置误差 0.097060 米，未通过 0.08 米门限。测量确认已停止、罐子仍握持且未受桌面支撑，工作进程保持原物理世界并允许重试。
- `20261009-web-01-continue-01/`：17:53:44 调用大脑 `/api/task_continue`，绑定原任务、步骤和命令，生成 `nav-relay2-b1e2af5bff46-r1`。这是一次受控重试，不是六动作各一次成功；前面的导航、抓取未重放。
- 17:54:28，门口重试通过；17:55:17，持物横移过门通过，进入会议桌导航。
- 17:56:26，桌前原命令 `nav-table1-b1e2af5bff46` 的 `office_nav_table1__carry_clearance` 报 `planning_or_precondition_failed`；规划器日志原文为 `No valid move trajectory found: MotionGenStatus.TRAJOPT_FAIL`。机器人停止且罐子仍握持，没有放宽碰撞或抓持门限。
- `20261009-web-01-continue-02/`：17:57:30 在同一回合第二次显式继续，产生 `nav-table1-b1e2af5bff46-r1`；重试规划通过。两次继续之间未修改运行中的任务代码，也未重置世界。轨迹优化失败的具体根因尚未确定，不能把一次重新规划成功当成稳定性修复。
- 17:59:25，会议桌导航通过、进入放置；18:00:40，大脑 `succeeded`、11/11。六个实际动作均最终通过，门前导航和桌前导航各两次尝试，其他四个动作各一次；未人工跳过动作。可选视觉检查按既有仿真配置关闭，不属于本轮验收。

放置原命令 `vla-place-b1e2af5bff46` 的效果证据为：`destination_table_contact=true`、`grasp_contact=false`、`placement_gate_accepted=true`，平面误差 0.006128 米，手罐距离 0.197645 米，`holding=null`、`released=true`、`object_at_target=true`。来源为 `simple_o6_physics`，不是实体机器人。

当前 Brain 状态快照的 `has_failures=false` 与账本中保留的两次 `FAIL` 尝试不一致；本记录和视频依据原命令、尝试账本和物理分段，不把该汇总字段当成“无失败”。未在本任务中重启或修改大脑来修正展示汇总。

<a id="e-p5-s4"></a>

### 4. 视频与证据

完整回放为 `20261009-web-01-continue-02/office-reception.mp4`，由本轮轨迹导出，1280×720、25 fps、225.12 秒（3 分 45 秒），H.264 共 5628 帧，包含 5388 个轨迹画面及八段尝试的结果停留画面。完整 qpos 共 10775 帧，实际仿真时间约 215.5 秒。不是实时摄像头录像，不剪掉失败与重试。已检查过门、最终放置画面并全片解码验证。

同目录的 `current_task.json`、`events.jsonl` 和各 `*-command-*.json` 保留任务、原命令及两次继续身份。物理模型、轨迹、来源和 `segments.json` 归档于证据根目录下的 `office-web-7f77aab69f4844f9a12bb1e2af5bff46-b8e7ab22/`；`office-web01-evidence.tar.gz` 为同轮原始归档。实验数据不入 Git。

本轮 provenance 中 `pick_approach_overshoot_compensation_m=0.045` 是沿用旧房间元数据的标注错误；实际执行源码取物瞄准 X=-0.645、核验目标 X=-0.620，差为 0.025 米。历史文件保留不改写；收尾只修正后续回合的该元数据，并增加场景构建和 Episode 源码哈希，不改变物理执行逻辑。

<a id="e-p5-s5"></a>

### 5. 软件验证与剩余边界

本轮最初完整离线回归通过 477 项测试及 27 项 DREAM 自检。新增视频失败／重试对应关系测试后，再次运行 `venv/core/bin/python scripts/run_tests.py`，478 项测试和 27 项 DREAM 自检全部通过（104.616 秒的主测试集）。最后的 provenance 标注修正后，接待与办公室定向测试 15 项通过。改动文档的 45 个本地链接及章节锚点检查、`git diff --check` 通过；软件回归不等于物理接待验收。

回放读取本轮 `execution_model.mjb` 和 `trajectory.npz` 的完整 qpos，不合成动作、不插值。摄像机隐藏天花板、调整灯光不改变执行物理。每段尝试对应自身原命令与测量结果，不能用重试成功覆盖第一次失败。

收尾保留接待模块为新办公室仿真，未切回真机。结果证明在两次受控重试后，该固定新办公室可完成大脑导航、抓取、持物过门、放置全流程；一次无干预通过率、可乐罐抓放、原 ROS 导航客户端接入和真机接待仍未由本轮证明。


<a id="e-p5-recovery"></a>

### 新版五点接待与异常恢复复验

2026-10-21 17:12:49～17:34:36（北京时间），按用户要求实际运行 SIMPLE 物理仿真的“开始接待”。
使用当前大脑源码和 v2 Skill，调用既有 `scripts/simple_reception_experiment.py`，
以独立账本、进程内 `reception_simple` 路由和关闭的真机派发许可执行；未切换主大脑的真机环境。
这次不是脚本状态替身：NAV/VLA 来自远端 `connection-simple-o7` 服务，实际运行 O6／SONIC／MuJoCo／CuRobo，
同一物理回合覆盖五段导航及抓取、放置。校准物体仍为汤罐 `graspnet1b:2`，合同 ID 为 `cola_can_1`。

**准备与配置。** 服务起初停止，启动后发现旧配置为 `office_v2`、`navigation_only=true`、
`navigation_source=fridge`，路线声明也与当前 Skill 不同，不能用于完整接待。
核清无活动命令后，通过原管理脚本停止服务，在仿真机
`/home/fangqi/connection-simple-o7/runtime/connection-reception-check-20261021/`
保存 `config.before.json`，随后将同一服务设为完整接待（`navigation_only=false`、来源 `table`），
导航点声明来自当前 Skill 快照，seed 保持 605，重新启动并通过双方实例／路线／模式校验。
原配置 SHA-256 为 `2b2967540f50eaad6784159dbc0e0db84965860edb6697935b96de79bd209d63`，
本次配置为 `8fde5ef35d74b684f43a51e031e0df1a178be722a006ba64ec6ccba2f7a3f8e2`，副本为 `config.test.json`。
所用办公室 XML 声明 SHA 为 `5ac1bf0125d11720d90a376f269468f75b5a0119e3b2233e4d8aef0b9d92d0c1`；
派生房间清单 SHA 为 `1e11bdc37d3c60d795472e6e8c8039b057ae7cd73d14e58bdbbe1c72e4a8d2df`。
本次没有调整物理模型、控制器、导航到达门限或抓放核验门限。仿真机同时有其他计算任务，不能将此次壁钟耗时作为无负载性能。

**结果。** 任务 `simple-reception-20261021-171249`，物理 episode 为
`simple-reception-20261021-171249-4ad242fe`。最终大脑 succeeded、12/12，七个物理步骤最终 PASS，
共八次物理命令尝试，保留一次失败、一次人工继续重试、零人工跳过，`plan_revisions=[]`。
可选视觉 inspection 沿原配置未启用，不属于本次物理验收。

| 步骤／原命令 | 结果 | 关键测量 |
| --- | --- | --- |
| table2：`nav-table2-261021171249` | PASS | 位置误差 0.02818 m，朝向误差 0.08044 rad |
| 抓取：`vla-pick-261021171249` | PASS | 抬升 0.10067 m，手部接触 true，桌面支撑 false，持有 `cola_can_1` |
| relay1：`nav-relay1-261021171249` | PASS | 位置误差 0.04316 m，朝向误差 0.03763 rad，仍持物 |
| relay2：`nav-relay2-261021171249` | PASS | 位置误差 0.06836 m，朝向误差 0.11437 rad，仍持物 |
| relay3 横移：`nav-relay3-261021171249` | PASS | 位置误差 0.02542 m，朝向误差 0.03378 rad，仍持物 |
| table1：`nav-table1-261021171249` | FAIL | `office_nav_table1__carry_clearance` 阶段 `MotionGenStatus.IK_FAIL`；尚距目标约 0.74467 m，已停止、物体仍持有 |
| table1 重试：`nav-table1-261021171249-r1` | PASS | 位置误差 0.01470 m，朝向误差 0.02733 rad，原物理回合保留 |
| 放置：`vla-place-261021171249` | PASS | 落点误差 0.005231 m，目标桌接触 true，手部接触 false，手罐距离 0.18454 m，released=true、holding=null |

**失败与恢复经过。** 17:25:13 执行端返回原失败，日志为
`No valid move trajectory found: MotionGenStatus.IK_FAIL`，大脑停在 recovery_required，未推进放置。
该日志只确认了避让姿态逆运动学规划未通过，不能据阶段名断言发生碰撞或几何间隙不足。
执行端明确 `navigation_stopped=true`、`object_retained=true`、`simulation.retry_allowed=true`，
worker 保留原物理回合，允许同段新尝试。

真实 DeepSeek 首先建议 `observe_scene`。测试者通过原建议确认接口执行只读读取，未下发动作；
第一次新建议仍要求同类读取，暴露了“刷新接口状态”与“获得新的视觉证据”的混淆。
补充停止／资源字段及不重复索取同类新鲜状态的提示后，再次读取，模型选择 `request_help`。
测试者核对原失败、停止、持物与同回合重试许可后，在 17:29:06 通过既有 `continue` 入口显式重试；
17:30:18 桌前导航通过，17:34:36 放置通过。前面的抓取和导航未重放、场景未重置、没有修改物理执行逻辑。
因此本轮证明“物理完整接待＋LLM 读取状态／求助＋原人工继续”可工作，**没有验证 LLM revise_plan 建议在物理回合中执行**，
也不是自主恢复成功。一次 IK 重试通过不代表该物理规划不稳定性已修复。

**本次发现的软件兼容点。** SIMPLE 终态命令使用 `error.retryable=false` 禁止自动重放，
但可恢复导航额外明确 `result.simulation.retry_allowed=true`，原继续入口按这个同回合许可工作。
新恢复方案校验最初把前者一律视为禁止新尝试。现已仅对绑定的 SIMPLE／ROS 仿真接待、且明确许可的情况兼容；
安全失败、`next_command_allowed=false`、停止／资源未确认仍拒绝，真机后端不套用该例外。
另明确模型提示：preserve_order 允许原序恢复、阶段名不是确定根因，不能把缺少视觉信息反复转换成状态读取。
原模型输出保留，不以修订后的提示重写本轮决策记录。
该兼容修改后完整离线回归 619 项测试及 27 项自检通过；随后上下文／提示调整的 41 项定向测试通过。
本轮没有重启主大脑或网页，后续重载才采用这些新增修正。

**收尾与证据。** 结束时两端无活动命令，仿真服务保留本次完整接待配置，旧配置备份可恢复。
主机四份配置及生效环境快照哈希均保持，接待仍为 `reception_real`。
主任务账本在测试期间变化，是网页于 17:16:06 另行发布了
`766d0c99fd2f45a09a5ab1832febefba`；检查时它为 paused，不能写成主任务哈希完全未变。
本实验只操作自己的 ledger 和仿真端点，没有恢复、取消或覆盖该真机任务。

本地证据为忽略提交目录 `data/experiments/simple-reception-20261021/`：
`round-01/` 保存原任务、所有尝试、LLM 建议与确认，`remote-commands.json` 保存八次原始命令结果，
`verification.json` 与 `verify_round.py` 保存独立只读核查；`physical-evidence.json` 含分段测量、来源、原始 IK 错误及文件清单。
`observe-scene*.log`、`continue-01.log` 保留人工操作，`prepare-service.log` 保存配置准备和备份依据。
原物理模型、轨迹和分段报告仍在仿真机上述 episode 目录，`trajectory.npz` 约 5.46 MB，
`execution_model.mjb` 约 284 MB；本次没有渲染新视频。这些文件不随源码克隆提供。

## 证据追溯

原 33 份记录已按职责分入六个模块；本模块对应的排障与复验内容已写在上方正文，日常查阅使用页内章节即可。下表仅用于核对迁移来源；原始版本同时保存在本地 Git 提交 `5b37e29`，不是日常排障的必经入口。

| 来源 | 证据日期／范围 | 原文路径 |
| --- | --- | --- |
| P1 | 2026-09-24：旧 O7 失败与 O6 单步抓取 | `docs/多端联调_20260924_simple o6 抓取接入.md` |
| P2 | 2026-09-28：Desk／MuJoCo／3DGS／SIMPLE 通用入口，分别注明证据边界 | `docs/多端联调_20260928_仿真通用任务验收.md` |
| P3 | 2026-10-08：连续物理接待接入与调参 | `docs/多端联调_20261008_simple o6 仿真.md` |
| P4 | 2026-10-09 上午：正式网页六动作及物理回放 | `docs/多端联调_20261009_simple o6 网页接待.md` |
| P5 | 2026-10-09 下午：新办公室适配、失败和重试，与上午旧房间区分 | `docs/多端联调_20261009_simple o6 新房子接待.md` |
