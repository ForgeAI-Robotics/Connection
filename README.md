# Connection 具身大脑

生产代码统一在 `src/`。任务网页、飞书、语音文本入口分别直连大脑；所有业务任务使用同一个 Runtime。旧调度、旧接待循环及导入兼容层已退出，历史代码可在 Git 提交 `d85e9b9` 查看。

```text
网页 :8888 ──┐
飞书 ────────┼→ Brain API :5000 → Runtime ⇄ Planner / 业务包
语音 :8890 ──┤                     ↓ Runner → 执行适配器 → 身体 / 仿真
外部 API ────┘                     ↑ Verifier ← 执行证据
                                  └→ 账本与记忆 → 异步反思、评测、发布
Ops :5678 → 服务启停、健康、日志、运行环境切换
```

## 目录

| 目录 | 用途 |
|---|---|
| `src/brain/` | 大脑应用、内核、业务包、技能、适配、账本、学习、API |
| `src/entries/` | 独立网页、飞书与语音文本入口；不持有大脑账本 |
| `src/ops/` | 运维面板、受控环境切换与服务管理 |
| `src/execution/` | 机器人语义接口、Slaver、协作通信、DREAM 服务及可选扩展 |
| `src/contracts/`、`src/clients/` | 数据合同与大脑 HTTP 客户端 |
| `src/shared/` | 配置加载、路径、日志与环境配置基础设施 |
| `venv/` | 本机 Python 环境、CUDA 和下载依赖；整个目录不入 Git |
| `config/` | 公开模板、业务规则、场景配置及忽略提交的本机 YAML |
| `data/` | 任务账本、经验、示教、媒体、飞书数据库与运行环境快照 |
| `logs/` | 按日期、服务保存日志 |
| `tests/` | 机制、应用、入口、执行、运维及进程集成回归 |
| `scripts/` | 配置初始化、测试、环境切换、一次性数据迁移 |
| `simulation/` | Desk、MuJoCo、3DGS、SIMPLE 远端后端及资产 |
| `infra/` | systemd 与 Docker/ROS 部署设施 |
| `docs/` | 接口、按模块持续维护的联调验证与系统架构 |

`src` 是源码根，不是 Python 包名。项目只有根目录这一份 `pyproject.toml`；启动和导入直接使用 `brain`、`entries`、`ops` 等包。

## 安装和启动

Python 3.10；普通服务用 `venv/core`，GPU 仿真用 `venv/gpu`。两套依赖分开安装，避免 GPU 工具链影响普通服务。`venv/cuda` 存本机 CUDA 工具链，`venv/deps` 存下载的第三方源码与资源；整个 `venv/` 不入 Git。可复现的依赖声明仍保留在 `pyproject.toml`、`uv.lock` 和 `config/dependencies/`。

`__pycache__/` 是 Python 自动缓存，已被 Git 忽略，可删除并自动重建；`.github/workflows/` 是自动测试配置，保留原位置并提交 Git。

```bash
uv venv --python 3.10 venv/core
uv pip install --python venv/core/bin/python -e .
python3 scripts/bootstrap_local.py
# 飞书 / Slaver 按实际需要安装
uv pip install --python venv/core/bin/python -e '.[feishu,execution]'

venv/core/bin/python -m brain
venv/core/bin/python -m entries.web
venv/core/bin/python -m entries.feishu
venv/core/bin/python -m entries.voice
venv/core/bin/python -m ops
# 使用 Slaver 执行路径时另起 Redis 和 Slaver
venv/core/bin/python -m execution.slaver
```

网页 `:8888`、语音文本入口 `:8890` 和飞书默认从 `config/networks.yaml` 当前 Wi-Fi 解析大脑地址与端口；网页访问运维面板也使用同一地址表。独立部署可显式设置 `MASTER_URL`、`LARK_BRAIN_URL`、`CONNECTION_OPS_URL` 覆盖，现场部署应留空，避免换机后残留旧地址。语音入口只转发已识别文本。运维面板 `:5678` 独立运行。沿用的面板服务 ID `master` / `deploy` 只是既有管理 API 标识，启动的都是新模块。

从仓库外部署时设置 `CONNECTION_WORKSPACE` 指向配置和数据根目录。配置不会从进程工作目录猜测。入口可独立部署，无需挂载大脑的数据。

安装系统级面板：`sudo bash infra/systemd/install_panel.sh`。服务单元直接运行 `python -m ops`，不经过旧脚本。三端启动、现场确认和收工顺序见[控制面板接口说明](docs/接口说明_控制面板.md#三端启动与任务发布)；操控独立维护见[操控接口说明](docs/接口说明_操控VLA.md#操控侧检查与独立启停)。

## 日常配置与数据

- `config/networks.yaml`：本机网络地址和 SSH 用户，参考模板为 `config/examples/networks.yaml`；不入 Git。
- `config/brain.yaml`：大脑模型、任务与反思设置。
- `config/execution.yaml`：接待、通用执行、观察各模块的仿真／真机选择。
- `config/robot_api.yaml`、`config/slaver.yaml`、`config/dream.yaml`：执行配置。
- `config/business/`、`config/scene/`：业务规则和静态场景。
- `.env`：凭据与本机环境变量；不入 Git。
- `data/tasks/`：唯一任务账本，包含步骤尝试和原 command_id。
- `data/memory/`、`data/business/`：经验、反思、示教、观测输入输出。
- `data/timelines/`、`data/feishu/`：任务回放和入口数据库。
- `data/system/execution.json`：已经应用的环境快照。
- `data/retired/`：只读保存的历史数据，不运行旧程序。
- `logs/YYYY-MM-DD/<服务>/`：服务日志。

多服务器通过 Git 同步代码和公开模板，本机配置、依赖环境及运行数据各自保留。
首次解除网络配置跟踪的升级步骤与日常同步流程见[配置说明](config/README.md#多服务器代码同步)。

网页、飞书、语音文本入口、外部 API 以及接待演示共用 Runtime。通用任务由 Planner 生成步骤；接待与观察使用业务包；桌面整理通过现有规则展开。Runner 不改路线；Verifier 只输出 PASS / FAIL / UNKNOWN。反思读取任务快照，不改正在执行的任务，也不自动改内核代码。

大脑先建立可取消的任务，再选择业务包：明确的 SOP 触发词直接命中，其它表述由 LLM 结合登记范围选择；通用任务继续按当前现场能力规划。当前真机接待为导航到冰箱前、开门、原地抓可乐并核验，再沿中转点 1、2、3 运送到 1 号桌；关门和放置暂不执行。网页三个按钮分别为“导航真机＋操控真机”“导航真机＋操控模拟”“导航模拟＋操控真机”，共用同一流程；第三种发布前确认现场作业姿态，模拟到达不作为实物到位证据。原物理仿真分支仍保留抓放路线。模型不能改写绑定的路段或命令字段。异常时 LLM 读取原命令、回包和账本生成受限建议，API 保留建议；网页默认展示失败原因及原始错误。按配置可启用异常建议展示与单次人工确认，由原 Runtime 校验并处理；公开模板默认关闭，本机已启用。建议不会自动执行，也不改变核验标准。`/api/sops` 返回登记目录，任务状态返回所选版本和异常建议。配置与验收边界见[任务选择与异常建议](docs/接口说明_大脑%20Brain.md#11-共用任务选择与异常建议)。

## 仿真与真机

日常在运维面板「运行环境」选择 SIMPLE 仿真或真机并应用，任务入口与接待流程沿用现有实现。旧协议模拟保留为命令行维护能力：使用原 v1 Skill 和协议模拟预设，启动两个独立 NAV/VLA HTTP 进程，再从任务网页首页发布「开始接待」。共用大脑通过正式指令与结果查询完成流程；默认延时成功，不依赖真机或物理仿真器。接口边界、持久化、控制器凭证扩展和故障场景见[模拟真机接口说明](docs/接口说明_模拟真机.md)。

接待已抽取为版本化 Skill：[v1 原流程](src/brain/packages/definitions/reception_single_can_v1.yaml) 保留四段基线，[v2 新流程](src/brain/packages/definitions/reception_single_can_v2.yaml) 使用新坐标并增加 relay1，根步骤保留原抓放；同文件 `real_workflow` 为真机登记开门／抓可乐流程，开门与抓取之间不重复导航，抓取核验后保留原运送路线。新任务校验并绑定定义快照，继续旧任务不重读文件。可用 `venv/core/bin/python scripts/reception_skill.py --config config/brain.yaml` 离线预览原步骤，添加 `--real` 预览真机分支；旧协议模拟仅支持 v1，SIMPLE O6 新房子需部署匹配的五段路线；配置、字段及 NAV/VLA 合同限制见[接待 Skill 定义](docs/接口说明_大脑%20Brain.md#111-接待-skill-定义与任务快照)。

在面板侧栏「运行环境」选择配置，或修改配置后执行：

```bash
venv/core/bin/python scripts/execution_mode.py preview
venv/core/bin/python scripts/execution_mode.py apply
```

配置支持接待、通用执行、观察分别覆盖。运行环境变化仍检查在途任务，失败回退配置，不重置任务账本。Desk 直连不依赖 Redis/Slaver，MuJoCo/3DGS 执行继续使用现有底座。

`reception_real.kernel_enabled=false` 仍表示真机派发许可关闭，所有任务照常进入新 Runtime。DREAM/VLA HTTP 合同与 command_id 不变，交接能力未核实时不会假装接管成功。该开关的当前值以生效配置为准；工程替换、仿真成功和真机仅导航成功都不等于真机完整接待验收。

大脑统一控制权接线通过 `reception_real.body_control` 选择，公开模板默认关闭。大脑向独立网关授权，NAV／VLA 经网关进入 SONIC，旧 relay 不参与。普通 NAV 只检查身体，不要求 VLA、相机或 O6 在线；混合模式的操控与持物仍为本地模拟。2026-10-28 新增 VLA 单动作候选，按当前步骤准备、核验、提交和退出，代码与隔离联测已完成，并于当日重载大脑生效；网关新模式也已启用，VLA 真实依赖及真机验收仍待完成，真实持物导航仍阻断。Runtime、命令身份及原账本保留。当前字段与限制见[网关 VLA 接入](docs/接口说明_机器人统一网关.md#162-vla-按需接入候选2026-10-28代码与隔离联测未部署)，此前 NAV 部署与迁移见[实际接线记录](docs/接口说明_机器人统一网关.md#15-nav-阶段实际大脑接线2026-10-27)。

“开始接待（导航真机＋操控模拟）”已适配身体独立网关，可从网页第二个按钮发布；操控为本地模拟。
开门、抓可乐及其核验仅返回本地模拟回执，页面明确标记；该模式不代表真实操控完成。操作及恢复边界见
[大脑接待流程](docs/接口说明_大脑%20Brain.md#81-流程与请求身份)。

## 验证与迁移

```bash
venv/core/bin/python scripts/run_tests.py
```

测试使用临时账本和本机假服务，不连接真实机器人。旧实现专属测试的退出及替代覆盖记录在 `tests/contracts/retired_test_coverage.json`，不以测试数量相同代替行为验收。

一次性迁移工具 `scripts/migrate_layout_data.py` 默认只预览；应用前必须停止写入者、核清旧任务。它拒绝未核清命令和不同内容的目标文件，先备份再复制并验证哈希，不把旧接待账本转换成新任务。之后旧源码目录才可以退出。

工程重构已完成，后续接入与规则变化见[重要变更记录](docs/架构设计_系统架构与演进.md#4-重要变更记录)。SIMPLE 新通用身体任务与接待共用连续物理执行接口，旧单步抓取路由保留用于原命令查询和实验；当前能力见[仿真接口](docs/接口说明_simple%20o6%20仿真.md)，各轮通过范围见[文档索引](docs/README.md)。

详见 [文档索引](docs/README.md)与[系统架构与演进](docs/架构设计_系统架构与演进.md)。
