# 项目配置

本目录集中保存项目级公开配置模板和按用途拆分的依赖清单。真实密钥、现场地址及本机运行配置不得提交。

## 目录

- `examples/`：安全的公开模板；`scripts/bootstrap_local.py` 将它们复制到各模块约定的本地路径。
- `dependencies/`：轻量开发、飞书和 3DGS 专用安装清单。
- `business/`、`scene/`：随代码评审的基础业务规则与静态场景；生成的 SOP、经验和现场数据放在 `data/`。

项目完整 Python 依赖集合以根目录 `pyproject.toml` 为声明源，`uv.lock` 为锁定结果。两者必须保留在项目根目录，以兼容 uv 和 Python 构建工具。`dependencies/` 下的清单是按运行边界提取的安装子集，服务于本机环境、组件启动或 CI；修改重叠依赖时必须与 `pyproject.toml` 保持兼容。

Git 忽略规则保留在根目录 `.gitignore`，不另建第二份规则。下表右侧的本机文件均不入 Git；
`config/` 不能整体忽略，公开模板、依赖清单、基础业务规则和静态场景仍需版本管理。

## 初始化本地配置

```bash
python scripts/bootstrap_local.py
```

脚本只创建缺失文件，不覆盖现有配置，也不启动服务。对应关系如下：

```text
config/examples/env.example                 -> .env
config/examples/networks.yaml               -> config/networks.yaml
config/examples/execution.yaml              -> config/execution.yaml
config/examples/brain.yaml                  -> config/brain.yaml
config/examples/slaver.yaml                 -> config/slaver.yaml
config/examples/robot_api.yaml              -> config/robot_api.yaml
config/examples/serve_dream.yaml             -> config/dream.yaml
config/examples/serve_real.yaml              -> config/serve_real.yaml
config/examples/dream_navigation_sop.yaml    -> config/dream_navigation_sop.yaml
```

模板里的 `192.0.2.*`、`*_user`、`/path/to/...` 是占位值，初始化后按机器填写。
`config/examples/execution_reception_protocol.yaml` 是手动选择的协议模拟预设，不在初始化时覆盖运行环境。

## 多服务器代码同步

vla-ai 与 nav-ai 使用同一 Git 分支同步代码和公开模板：修改端先拉取、修改、验证并提交推送，
另一端再 `git pull --ff-only`。可双向进行；遇到本地改动或分叉先保留并审查，不能用强制覆盖解决。
各机的 `.env`、上表中的本机 YAML、`venv/`、`data/`、`logs/` 独立保留，不通过 Git 或整目录覆盖同步。

```bash
git status --short
git pull --ff-only
python3 scripts/bootstrap_local.py
```

初始化只补缺失文件；模板更新不会合并进已有本机配置。新增必需字段需按变更说明补入本机文件，
依赖声明变化需更新对应环境；常驻进程需按运行状态安排重载。拉取代码不等于运行实例已经更新，
不能据此重置任务、自动继续动作或切换环境。

首次升级到取消跟踪 `networks.yaml` 的版本前，先将原文件备份到忽略提交的目录并记录校验值，
保留已有未提交改动后再拉取，最后恢复原文件并核对。Git 中删除已跟踪文件会影响另一端工作树，
不能仅靠新增忽略规则保护首次升级。

历史 `config/business/reception_sop_v2.yaml` 同样先备份，再迁入 `data/business/reception_sop_v2.yaml`；
若目标已有文件则保留目标，将历史副本另行归档，不覆盖新的运行数据。接待 SOP 展示优先读取该
运行数据文件，不存在时使用 `config/business/reception_sop.yaml`；显示生成结果不代表规则已发布。

## 依赖边界

- 通用 `venv/core`：安装 `dependencies/requirements-core.txt`；需要飞书时再安装 `dependencies/requirements-feishu.txt`。
- 飞书组件或 CI：`dependencies/requirements-feishu.txt`。
- CUDA / 3DGS 专用 `venv/gpu`：`dependencies/requirements-3dgs.txt`。

```bash
uv venv --python 3.10 venv/core
uv venv --python 3.10 venv/gpu
uv pip install --python venv/core -r config/dependencies/requirements-core.txt
uv pip install --python venv/core -r config/dependencies/requirements-feishu.txt
uv pip install --python venv/gpu --prerelease allow --index-strategy unsafe-best-match -r config/dependencies/requirements-3dgs.txt
uv pip install --python venv/core -e .
uv pip install --python venv/gpu -e .
```

场景资产、Nav2、ROS、遥操作训练等模块内运行配置继续保留原位，例如 `simulation/assets/`、`simulation/nav2/config.yaml`、`simulation/backends/mujoco/scene/config/` 和 `infra/docker/nav2/**/config/`。这些文件与代码使用相对路径耦合，不属于项目级配置模板。

## 统一切换仿真与真机

管理面板 `:5678` 侧栏的「运行环境」只提供「SIMPLE 仿真／真机」和「应用」。界面与切换保护见[控制面板说明](../docs/接口说明_控制面板.md#运行环境)。两种选择使用同一配置合同：

- `mode` 为 `simulation` 或 `real`，`simulation_backend` 固定为 `simple_o7`。
- 三个模块的 `mode` 均为 `inherit`；接待的 `simulation_backend` 固定为 `reception_simple`，通用执行与观察为 `inherit`。
- SIMPLE 下接待任务为 `reception_simple`，新通用身体任务为 `body_simple`，共用 NAV/VLA；真机下分别为 `reception_real` 和 `body_real`。旧生效快照需重新应用才更新路由，观察不可用时不回落其他仿真。

以下模块覆盖及旧后端配置仍用于命令行维护，不再作为面板选项。协议模拟预设为 `examples/execution_reception_protocol.yaml`，用 `--file` 预览并应用。

本地配置在 `config/execution.yaml`，模板为 `config/examples/execution.yaml`。例如接待选择真机，通用执行选择 Desk，观察和网页画面选择 3DGS：

```yaml
mode: real
simulation_backend: desk
modules:
  reception:
    mode: inherit
  execution:
    mode: simulation
  observation:
    mode: simulation
    simulation_backend: mujoco_3dgs
```

模块 `mode` 可选 `inherit / simulation / real / disabled`。执行与观察的 `simulation_backend` 可选 `inherit / desk / mujoco / mujoco_3dgs / simple_o7`。接待可选 `inherit / reception_mock / reception_protocol / reception_simple`；`inherit` 保留旧演示，`reception_protocol` 使用独立导航与操控协议模拟进程。旧演示与协议模拟均不代表物理仿真器已支持整套接待；`reception_simple` 使用远端 SIMPLE 接待端点。观察覆盖只影响现场描述和网页画面；动作核验仍使用执行后端自己的证据。

命令行和面板使用同一个应用过程：

```bash
# 查看草稿、生效版本、最近一次应用结果及真机动作许可
venv/core/bin/python scripts/execution_mode.py status

# 改完 config/execution.yaml 后预览并应用
venv/core/bin/python scripts/execution_mode.py preview
venv/core/bin/python scripts/execution_mode.py apply

# 一键切到全仿真（清除模块覆盖）
venv/core/bin/python scripts/execution_mode.py apply --mode simulation --sim mujoco_3dgs --reset-overrides

# 接待真机、通用执行 Desk、观察 3DGS
venv/core/bin/python scripts/execution_mode.py apply --mode real --reset-overrides --execution simulation --execution-sim desk --observation simulation --observation-sim mujoco_3dgs

# 全真机：未接入的能力会明确停用，不回落仿真
venv/core/bin/python scripts/execution_mode.py apply --mode real --reset-overrides
```

应用会检查唯一的 Runtime 任务账本和大脑状态。有运行、暂停、人工等待、取消中、待恢复任务，或命令／资源未核清时拒绝切换。事务持有准入互斥，停止大脑及需要重载的 Slaver 后再次核对账本；启动所需本机仿真服务，重启大脑及 Slaver，验证目标和配置版本。网页、飞书保留运行，每次通过大脑读取生效配置。Desk 直连且未运行 Slaver 时无需 Redis。**不重启或清空 Redis，不启停远端 DREAM／VLA，不发任何任务。** 接待路由是 `reception_real` 时把 `reception_real.kernel_enabled` 写成 true，否则写成 false；失败时连同运行配置一起恢复。回退未完成时保留阻断标记。

已应用的快照保存在 `data/system/execution.json`。业务进程启动时读取一次，修改草稿不热切换运行中的后端；需要点击应用或运行 `apply`。这份快照优先于旧的 `RECEPTION_MODE`、`ROBOT_API_BACKEND`、`ROBOT_BACKEND`、`ROBOT_API_URL` 和独立导航覆盖。后端基础地址仍在 `config/robot_api.yaml`；修改地址后重新应用。`data/system` 中的文件由程序维护，不手动改写或删除。

**能力边界：**应用真机接待或真机通用身体执行会打开共用 `kernel_enabled`。派发仍要过下游闸门（导航通道就绪）。两者均切回仿真或停用时才关闭许可。通用身体任务已共用真机 NAV/VLA，支持登记正向路线及单罐抓放；任意物体／地点与桌面整理尚未接入。真机观察仍不可用。Desk 不提供画面；选择 Desk 作为观察后端时会明确不可用，不自动寻找另一环境的相机。各模块的实际选择可在管理面板及 `:8888` 任务网页查看。

一键离线测试仍使用 `venv/core/bin/python scripts/run_tests.py`。测试脚本隔离现场生效配置及切换互斥文件，不受当前全局／模块环境影响，也不改现场生效版本。


工程入口：`python -m brain`、`python -m entries.web`、`python -m entries.feishu`、`python -m entries.voice`、`python -m ops`。大脑访问导航、VLA 的地址及各入口访问大脑的默认地址，都由 `networks.yaml` 的当前 Wi-Fi 决定；大脑角色的 `http_port`、`web_port`、`ops_port` 分别为 API、任务网页、面板端口。`MASTER_URL`、`LARK_BRAIN_URL`、`CONNECTION_OPS_URL` 仅用于独立部署覆盖，现场 `.env` 留空。修改网络配置后重启相关大脑和入口进程；不需要变更任务账本或执行环境。启动前安装本项目；不再提供旧脚本或导入转发。

大脑工作器配置位于 `config/brain.yaml` 的 `brain`：`queue_limit` 默认 32、`io_workers` 默认 4（最少 2）、`shutdown_timeout_sec` 默认 10、`capture_timeline` 默认 true。规划 1 个工作线程，Runtime 1 个所有者线程，控制 I/O 2 个，反思 1 个；媒体及示教工作器按需启动。关闭超时不会假写“已停止”，原命令保留在账本。`brain.scheduler=legacy` 不再由新启动器动态切回旧执行；回退须停准入、核清任务后切回上一代码版本。
