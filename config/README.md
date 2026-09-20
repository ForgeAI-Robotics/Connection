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
config/examples/master.yaml                 -> master/config.yaml
config/examples/slaver.yaml                 -> slaver/config.yaml
config/examples/robot_api.yaml              -> robot_api/config.yaml
config/examples/serve_dream.yaml             -> serve_dream/config.yaml
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
