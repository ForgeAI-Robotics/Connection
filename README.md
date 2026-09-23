# FQPlanner 大脑项目

面向要在本机把大脑跑起来、从网页或飞书发任务、看仿真画面的人。下文命令默认在项目根目录执行，大脑主机地址为 `192.168.5.35`。

真机接待逐步启动看 [联调说明：三端联调启动](docs/联调说明_三端联调启动.md)。VLA 口径看 [接口说明_操控VLA](docs/接口说明_操控VLA.md)。全部资料见 [文档目录](docs/README.md)。

## 1. 大脑做什么

FQPlanner 是任务编排层，不直接控电机。飞书或网页把任务交给 Master；导航走 DREAM，抓放走 VLA。

```mermaid
flowchart TD
    U[网页 / 飞书 / API] --> M[Master: Planner + Runtime + Runner + Verifier]
    M --> P[统一运行环境配置 + 模块覆盖]
    P --> R[接待: mock / DREAM + VLA]
    P --> E[通用执行: Desk / Slaver + 仿真]
    P --> O[现场观察与网页画面: 指定观察源]
```

所有任务共用 Runtime。运行环境可以全局切换，也可以按接待、通用执行、观察分别覆盖。在管理面板 `:5678` 顶部操作，或编辑 `config/execution.yaml` 后执行：

```bash
.venv/bin/python scripts/execution_mode.py apply
```

配置格式、一键命令、应用失败回退和能力边界见 [统一环境配置](config/README.md#统一切换仿真与真机)。切换不修改真机动作许可，不迁移在途任务，也不启停远端身体服务。真机通用执行和真机观察尚未接入；选择后明确不可用，不回落仿真。

接待固定顺序：导航到茶水间（table2）→ 抓瓶装可乐 → 过门 → 导航回工位（table1）→ 放下。VLA 当前是 `hand_state_only`：终态 `COMPLETED_HAND_STATE_ONLY` 只说明左手开合加至少 3 帧证据，**不等于**图像证实可乐在手上或已放到桌面。

飞书和 8888 网页共用 `robot_api.intent`，只分两类：天气/百科等闲聊不 `publish_task`；「开始接待」「桌上有什么」等交给大脑。认不出、又可能动手的句子按任务处理，避免误聊把真动作吃掉。

通用、看图、接待这三类顶层任务，仿真和真机跑完都会做事后复盘。真机接待仍是固定 DREAM → VLA 顺序，复盘不改规划、不重放动作。DeepSeek 根据执行账本写候选经验，调不通就降模板；默认只落到 `master/memory/reflections/`（git 忽略），不改 SOP。只有 mock「开始接待」还兼容原来的 `write_sop`。飞书任务卡会多一行「复盘：」。

关闭复盘：`master/config.yaml` 中设置 `reflection.enabled: false`，或设置环境变量 `REFLECTION_ENABLED=0`。只用模板、不调用模型：`FQPLANNER_REFLECTION_LLM=off`。

## 2. 冷机怎么开

面板：`http://192.168.5.35:5678`（`web/app.py`，systemd `fqplanner-panel`）。无登录，只能在可信网络使用。冷机只有面板自启，业务进程默认全停。

建议顺序：

1. **Redis** `:6379` → **Master** `:5000`；
2. **Deploy** `:8888`；要发飞书再启动**飞书桥接**，通用任务再启动 **Slaver**；
3. 要看图：先启动 **3DGS** `:5002`，起不来再启动 **MuJoCo** `:5001`；
4. 真机接待另行启动 DREAM / VLA；面板真机层两张卡片同时绿灯后才发任务。

当前 `master/config.yaml` 为 `collaborator.clear: false`，重启 Master **不会主动清空** Redis 协作库；但会中断当前 Master 进程和正在处理的任务，因此面板仍会先确认。只有显式改成 `true` 时，启动 Master 才清空对应协作库。面板启停的是本机 tmux，不会替你发布任务。

| 服务 | 地址 |
|---|---|
| 面板 | `http://192.168.5.35:5678` |
| 任务网页 | `http://192.168.5.35:8888` |
| Master | `http://127.0.0.1:5000` |
| 3DGS | `http://127.0.0.1:5002` |
| MuJoCo | `http://127.0.0.1:5001` |
| Desk | `http://127.0.0.1:5008`（无画面） |
| DREAM | `192.168.5.18:8001`；9882 仅绑定导航机回环地址，由大脑面板转发 |
| VLA | `192.168.5.194:8091` |

首次配置：

```bash
python scripts/bootstrap_local.py
```

本机只维护两套 Python 3.10 环境：`.venv` 运行大脑、网页、飞书和普通工具，`.venv_3dgs` 专用于 CUDA / 3DGS 渲染。不要再创建独立的 `.venv_feishu`；飞书依赖已纳入项目依赖并由 `.venv` 运行。

该命令从 [`config/examples/`](config/examples/) 创建缺失的本地配置，不覆盖已有文件，也不启动服务。飞书、SSH、远端地址从根目录 `.env` 读取。不要提交填写后的 `.env`。未应用统一运行配置时，公开模板中的 `RECEPTION_MODE=mock` 优先于旧接待配置；应用统一运行配置后，接待后端以其生效快照为准，不再被 `RECEPTION_MODE` 覆盖。旧地址 `192.168.5.185` 和 `192.168.0.108` 已停用。配置和依赖清单边界见 [`config/README.md`](config/README.md)。

每个服务一个 tmux session：

```bash
tmux attach -t redis|master|deploy|feishu|slaver|desk|mujoco|gs
```

日志位于 `log/YYYY-MM-DD/<服务>/`。

## 3. 仿真后端和网页四宫格

`:8888` 任务控制台的四宫格不是四个仿真，而是当前一个仿真后端的四路相机。

应用统一配置后，Deploy 只显示观察模块指定的后端，不自动切换来源。Desk `:5008` 不出图；需要画面时，将观察模块显式选为 3DGS 或 MuJoCo。尚未启用统一配置的旧环境保留原来的 3DGS → MuJoCo 选图顺序。

| 后端 | 画面 | 四路相机 |
|---|---|---|
| 3DGS `:5002` | 扫描高斯场景 | 俯视、头、右腕、左腕 |
| MuJoCo `:5001` | 几何厨房 / 轻量台面 | 俯视、正面、腕部、中心视角 |
| Desk `:5008` | 无画面 | — |

直播是实时渲染；回放是任务逐步截取的 JPEG。3DGS 首次出图会执行 CUDA JIT，可能需要一分多钟；期间 `/camera/latest` 返回一两 KB 的黑图属于正常初始化现象。

需要代理时：

```bash
export http_proxy=http://127.0.0.1:7897
export https_proxy=http://127.0.0.1:7897
```

真机接待不经过 3DGS / MuJoCo，机器人执行走 DREAM 和 VLA。

## 4. 网页和飞书怎么发任务

两个入口最终都进入 Deploy `:8888` → Master `:5000`。

- **网页**：在任务框提交。闲聊不会发送给大脑，运动类任务先确认；“开始接待”按钮视为已确认；
- **飞书**：私聊直接发，群聊使用 `@机器人 /task <内容>`；`/help`、`/status` 是桥接命令；
- `LARK_TASK_MODE=dry_run` 只发送卡片，不提交任务；`active` 才会真实发布。

真机接待中途失败并修好现场后，使用网页的**断点继续**，不要重新点击“开始接待”触发空手预检。

飞书配置：

```text
LARK_APP_ID=<应用 ID>
LARK_APP_SECRET=<应用密钥>
LARK_BRAIN_URL=http://127.0.0.1:8888
```

启动飞书桥接：

```bash
.venv/bin/python integrations/feishu/run.py
```

同一应用只能运行一个桥接进程，本机需要能访问 `open.feishu.cn`。

Master 在接待任务进入 Pipeline 前执行只读 `task_preflight`。DREAM `:8001` 或 VLA `:8091` 未就绪时返回 `status=rejected`，不会开始真实动作。

## 5. 发任务前 30 秒

1. 面板 Redis / Master 为绿色；
2. 要看图：3DGS 或 MuJoCo 为绿色，四宫格不是持续一两 KB 的黑图；
3. 要用飞书：飞书卡片为绿色，`LARK_TASK_MODE=active`；
4. 真机接待：DREAM 与 VLA 两张卡片均为绿色；
5. 确认现场支撑、肩带、急停及人工闸门已经按联调说明完成。

查看执行过程：

- 面板日志区；
- `log/YYYY-MM-DD/master/` 中的 `[task]` / `[reception]`；
- DREAM / VLA 探测和 SSH 输出位于当天 `monitor.log`。

## 6. 排障速查

| 现象 | 先看 |
|---|---|
| 网页提交没反应 | Master / Redis 是否监听；Deploy 日志是否调用 Master |
| 网页提示闲聊 | 改用明确任务表达，如“开始接待”“桌上有什么” |
| 四宫格持续黑图 | 3DGS 是否仍在首次 JIT；渲染日志是否异常 |
| 四宫格是几何块面 | 3DGS `:5002` 未启动，已回退 MuJoCo `:5001` |
| 飞书能聊天但不发任务 | `LARK_TASK_MODE` 及运动任务确认状态 |
| 接待立即 `rejected` | 检查 `task_preflight` 中 DREAM 8001 / VLA 8091 |
| 预检提示仍持有 `cola_can_1` | 抓取后失败，应使用“断点继续” |
| 导航失败但原因不清楚 | Master 日志中的 `未满足:`、`code` 和 `reason` |
| 飞书任务卡没有复盘 | 任务是否结束、复盘开关和模型配置 |
| CUDA / 3DGS 无法启动 | `nvidia-smi`；使用 `.venv_3dgs`，不要使用 nouveau |

## 7. 测试

在仓库根目录运行全部离线测试：

```bash
python3 scripts/run_tests.py
```

该入口汇总 Master、Web、飞书、Robot API 和 DREAM 测试。需要在线服务或真实机器人
的验收测试不会被自动执行。

## 8. 深入阅读

| 文档 / 文件 | 用途 |
|---|---|
| [文档目录](docs/README.md) | 架构、接口、联调、规划和排障索引 |
| [公共模块](common/README.md) | 进程日志和大脑业务流水账等共享基础设施 |
| [仿真目录](simulation/README.md) | Desk、MuJoCo、3DGS 及后续仿真工具的归类入口 |
| [非主链扩展](extensions/README.md) | PBD、NX 语音和旧式真机直连能力 |
| [架构说明：三端架构](docs/架构说明_三端架构.md) | 当前系统全局架构 |
| [联调说明：三端联调启动](docs/联调说明_三端联调启动.md) | 大脑、导航、VLA/NX 启动、人工闸门、预检和收工 |
| [接口说明：大脑 Brain](docs/接口说明_大脑%20Brain.md) | Master 编排、状态、断点继续和证据策略 |
| [接口说明：导航 NAV](docs/接口说明_导航NAV.md) | DREAM 世界、状态和四段导航合同 |
| [接口说明_操控VLA](docs/接口说明_操控VLA.md) | 抓放接口和证据等级 |
| [Linux 控制面板](web/README.md) | 面板启停、一键脚本和站立 Enter |
| `master/sop/reception_real.py` | 真机接待阶段 |
| `master/sop/episode.py`、`master/sop/reflect.py` | 任务账本和事后复盘 |
| `master/run.py`、`deploy/run.py` | 网页如何转到 Master |
| `robot_api/intent.py` | 闲聊和任务如何分流 |
| `robot_api/look.py` | 飞书看图 |
| `web/app.py` | 面板进程管理 |
