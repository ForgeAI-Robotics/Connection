# 项目配置

本目录集中保存项目级公开配置模板和按用途拆分的依赖清单。真实密钥、现场地址及本机运行配置不得提交。

## 目录

- `examples/`：安全的公开模板；`scripts/bootstrap_local.py` 将它们复制到各模块约定的本地路径。
- `dependencies/`：轻量开发、飞书和 3DGS 专用安装清单。

项目完整 Python 依赖集合以根目录 `pyproject.toml` 为声明源，`uv.lock` 为锁定结果。两者必须保留在项目根目录，以兼容 uv 和 Python 构建工具。`dependencies/` 下的清单是按运行边界提取的安装子集，服务于本机环境、组件启动或 CI；修改重叠依赖时必须与 `pyproject.toml` 保持兼容。

Git 忽略规则保留在根目录 `.gitignore`。Git 不支持从 `config/` 引入一份全局忽略文件，因此不要复制第二份规则。公开模板集中在这里，实际生成的 `.env`、`*/config.yaml` 和 `serve_dream/dream_navigation_sop.yaml` 继续由根目录 `.gitignore` 排除。

## 初始化本地配置

```bash
python scripts/bootstrap_local.py
```

脚本只创建缺失文件，不覆盖现有配置，也不启动服务。对应关系如下：

```text
config/examples/env.example                 -> .env
config/examples/brain.yaml                 -> config/brain.yaml
config/examples/slaver.yaml                 -> config/slaver.yaml
config/examples/robot_api.yaml              -> config/robot_api.yaml
config/examples/serve_dream.yaml             -> config/dream.yaml
config/examples/serve_real.yaml              -> extensions/serve_real/config.yaml
config/examples/dream_navigation_sop.yaml    -> serve_dream/dream_navigation_sop.yaml
```

## 依赖边界

- 通用 `.venv`：安装 `dependencies/requirements-core.txt`；需要飞书时再安装 `dependencies/requirements-feishu.txt`。
- 飞书组件或 CI：`dependencies/requirements-feishu.txt`。
- CUDA / 3DGS 专用 `.venv_3dgs`：`dependencies/requirements-3dgs.txt`。

```bash
uv pip install --python .venv -r config/dependencies/requirements-core.txt
uv pip install --python .venv -r config/dependencies/requirements-feishu.txt
uv pip install --python .venv_3dgs --prerelease allow --index-strategy unsafe-best-match -r config/dependencies/requirements-3dgs.txt
```

场景资产、Nav2、ROS、遥操作训练等模块内运行配置继续保留原位，例如 `simulation/assets/`、`simulation/nav2/config.yaml`、`simulation/backends/mujoco/scene/config/` 和 `docker/nav2/**/config/`。这些文件与代码使用相对路径耦合，不属于项目级配置模板。

## 统一切换仿真与真机

管理面板 `:5678` 顶部的「运行环境」支持：

- 「一键全仿真／一键全真机」：清除模块覆盖，所有模块跟随全局环境。
- 「应用当前选择」：保留各模块的独立设置。
- 「预览选择」：只展示将使用的后端，不停止服务。

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

模块 `mode` 可选 `inherit / simulation / real / disabled`。执行与观察的 `simulation_backend` 可选 `inherit / desk / mujoco / mujoco_3dgs`。接待的仿真是已有 `reception_mock`，不代表物理仿真器已支持整套接待。观察覆盖只影响现场描述和网页画面；动作核验仍使用执行后端自己的证据。

命令行和面板使用同一个应用过程：

```bash
# 查看草稿、生效版本、最近一次应用结果及真机动作许可
.venv/bin/python scripts/execution_mode.py status

# 改完 config/execution.yaml 后预览并应用
.venv/bin/python scripts/execution_mode.py preview
.venv/bin/python scripts/execution_mode.py apply

# 一键切到全仿真（清除模块覆盖）
.venv/bin/python scripts/execution_mode.py apply --mode simulation --sim mujoco_3dgs --reset-overrides

# 接待真机、通用执行 Desk、观察 3DGS
.venv/bin/python scripts/execution_mode.py apply --mode real --reset-overrides --execution simulation --execution-sim desk --observation simulation --observation-sim mujoco_3dgs

# 全真机：未接入的能力会明确停用，不回落仿真
.venv/bin/python scripts/execution_mode.py apply --mode real --reset-overrides
```

应用会检查唯一的 Runtime 任务账本和大脑状态。有运行、暂停、人工等待、取消中、待恢复任务，或命令／资源未核清时拒绝切换。事务持有准入互斥，停止大脑及需要重载的 Slaver 后再次核对账本；启动所需本机仿真服务，重启大脑及 Slaver，验证目标和配置版本。网页、飞书保留运行，每次通过大脑读取生效配置。Desk 直连且未运行 Slaver 时无需 Redis。**不重启或清空 Redis，不启停远端 DREAM／VLA，不发任何任务，也不改变 `kernel_enabled`。** 失败恢复原配置；回退未完成时保留阻断标记。

已应用的快照保存在 `data/system/execution.json`。业务进程启动时读取一次，修改草稿不热切换运行中的后端；需要点击应用或运行 `apply`。这份快照优先于旧的 `RECEPTION_MODE`、`ROBOT_API_BACKEND`、`ROBOT_BACKEND`、`ROBOT_API_URL` 和独立导航覆盖。后端基础地址仍在 `config/robot_api.yaml`；修改地址后重新应用。`data/system` 中的文件由程序维护，不手动改写或删除。

**能力边界：**真机接待仍受原 `kernel_enabled` 和下游闸门约束，切环境不会放行身体动作。通用执行与观察尚无接好的完整真机适配，选择真机时显示不可用并拒绝对应任务。Desk 不提供画面；选择 Desk 作为观察后端时会明确不可用，不自动寻找另一环境的相机。各模块的实际选择可在管理面板及 `:8888` 任务网页查看。

一键离线测试仍使用 `.venv/bin/python scripts/run_tests.py`。测试脚本隔离现场生效配置及切换互斥文件，不受当前全局／模块环境影响，也不改现场生效版本。


工程入口：`python -m brain`、`python -m entries.web`、`python -m entries.feishu`、`python -m ops`。`LARK_BRAIN_URL` 应直接指向大脑 `:5000`。启动前安装本项目；不再提供旧脚本或导入转发。

大脑工作器配置位于 `config/brain.yaml` 的 `brain`：`queue_limit` 默认 32、`io_workers` 默认 4（最少 2）、`shutdown_timeout_sec` 默认 10、`capture_timeline` 默认 true。规划 1 个工作线程，Runtime 1 个所有者线程，控制 I/O 2 个，反思 1 个；媒体及示教工作器按需启动。关闭超时不会假写“已停止”，原命令保留在账本。`brain.scheduler=legacy` 不再由新启动器动态切回旧执行；回退须停准入、核清任务后切回上一代码版本。
