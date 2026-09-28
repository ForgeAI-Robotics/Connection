# Connection 具身大脑

生产代码统一在 `src/`。任务网页、飞书分别直连大脑；所有业务任务使用同一个 Runtime。旧调度、旧接待循环及导入兼容层已退出，历史代码可在 Git 提交 `c2a98ab` 查看。

```text
网页 :8888 ─┐
飞书 ──────┼→ Brain API :5000 → Runtime ⇄ Planner / 业务包
外部 API ──┘                     ↓ Runner → 执行适配器 → 身体 / 仿真
                                ↑ Verifier ← 执行证据
                                └→ 账本与记忆 → 异步反思、评测、发布
Ops :5678 → 服务启停、健康、日志、运行环境切换
```

## 目录

| 目录 | 用途 |
|---|---|
| `src/brain/` | 大脑应用、内核、业务包、技能、适配、账本、学习、API |
| `src/entries/` | 独立网页与飞书入口；不持有大脑账本 |
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
| `docs/` | 架构、接口、实施与联调记录 |

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
venv/core/bin/python -m ops
# 使用 Slaver 执行路径时另起 Redis 和 Slaver
venv/core/bin/python -m execution.slaver
```

网页 `:8888` 使用 `MASTER_URL` 连接大脑 `:5000`；飞书使用 `LARK_BRAIN_URL` 直连 `:5000`。运维面板 `:5678` 独立运行。沿用的面板服务 ID `master` / `deploy` 只是既有管理 API 标识，启动的都是新模块。

从仓库外部署时设置 `CONNECTION_WORKSPACE` 指向配置和数据根目录。配置不会从进程工作目录猜测。入口可独立部署，无需挂载大脑的数据。

安装系统级面板：`sudo bash infra/systemd/install_panel.sh`。服务单元直接运行 `python -m ops`，不经过旧脚本。

## 日常配置与数据

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

网页、飞书、外部 API 以及接待演示共用 Runtime。通用任务由 Planner 生成步骤；接待与观察使用业务包；桌面整理通过现有规则展开。Runner 不改路线；Verifier 只输出 PASS / FAIL / UNKNOWN。反思读取任务快照，不改正在执行的任务，也不自动改内核代码。

## 仿真与真机

在面板侧栏「运行环境」选择配置，或修改配置后执行：

```bash
venv/core/bin/python scripts/execution_mode.py preview
venv/core/bin/python scripts/execution_mode.py apply
```

配置支持接待、通用执行、观察分别覆盖。运行环境变化仍检查在途任务，失败回退配置，不重置任务账本。Desk 直连不依赖 Redis/Slaver，MuJoCo/3DGS 执行继续使用现有底座。

`reception_real.kernel_enabled=false` 仍表示真机派发许可关闭，所有任务照常进入新 Runtime。DREAM/VLA HTTP 合同与 command_id 不变，交接能力未核实时不会假装接管成功。本次工程替换不等于真机验收，也不代表原四阶段的剩余范围完成。

## 验证与迁移

```bash
venv/core/bin/python scripts/run_tests.py
```

测试使用临时账本和本机假服务，不连接真实机器人。旧实现专属测试的退出及替代覆盖记录在 `tests/contracts/retired_test_coverage.json`，不以测试数量相同代替行为验收。

一次性迁移工具 `scripts/migrate_layout_data.py` 默认只预览；应用前必须停止写入者、核清旧任务。它拒绝未核清命令和不同内容的目标文件，先备份再复制并验证哈希，不把旧接待账本转换成新任务。之后旧源码目录才可以退出。

工程重构已完成；截至 `a42bdab`（2026-09-28）的后续接入与验证进度见[实施规划第 18 节](docs/规划说明_具身大脑重构.md#18-最新提交与验收索引2026-09-28)，具体通过范围和未完成项见[仿真通用任务验收](docs/联调说明_仿真通用任务验收.md)。

详见 [文档索引](docs/README.md)、[当前架构](docs/架构说明_三端架构.md)、[重构前后对比](docs/架构说明_重构前后对比.md)、[实施规划第 16 节](docs/规划说明_具身大脑重构.md)。
