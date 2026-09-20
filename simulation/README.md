# 仿真

本目录集中保存仅用于本地仿真、Mock 世界、仿真资产和仿真研发的代码。

- `backends/desk/`：定点桌面 Mock 后端，端口 `5008`；
- `backends/mujoco/`：MuJoCo / RoboCasa 后端，端口 `5001`；
- `backends/gs/`：MotrixSim + 3DGS 后端，端口 `5002`；
- `assets/`：仿真机器人、场景、3DGS 和场景配置资产；
- `nav2/`：仿真地图、自由点及工作点生成工具；
- `teleop/`：仿真遥操作、数据采集和策略训练；
- `services/`：ACT、PI0.5 仿真策略推理服务。

DREAM、VLA、真机驱动以及 `robot_api` 等共享路由层不属于本目录。
`docker/nav2/` 同时包含仿真和真机桥接，因此继续保留在共享基础设施目录。
