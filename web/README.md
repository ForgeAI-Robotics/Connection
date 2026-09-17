# FQPlanner Linux 控制面板

独立进程面板，默认监听 `0.0.0.0:5678`。只启停本机 tmux 进程、查看日志、探测 DREAM / VLA 健康。**不会发布接待任务。**

第一版没有登录。任何能访问 5678 的局域网设备都可以启停 Redis / Master / Deploy / 飞书 / Slaver / Desk 仿真。请只在可信网络使用。

Redis 和 Master 是大脑核心；Deploy 和飞书只是给 Master 下任务的入口。Desk 是通用任务用的本机 mock 后端 `:5008`。MuJoCo 是带画面的厨房仿真 `:5001`（RoboCasa 资产未下完时先起轻量台面），公司任务看图从这里截帧。真机层卡片可探测 DREAM / VLA。导航按导航组一键脚本远程启动：`g1_three_party_oneclick.sh`（会准备 NX SONIC、DREAM 8001/9882、4090 VLA HTTP）。启动后 9882 会自动从导航机的 `127.0.0.1` 转发到本机，浏览器开 `http://<本机IP>:9882` 点初始位并 Approve。DREAM 卡片的「站立 Enter」每点一次只向导航机 tmux 的 `adapter` 窗口发一个 Enter：第一次接管，第二次进 POSE 站立，中间肩带保持挂着。面板不代解肩带、不点 Approve。

一键脚本跑在导航机的 tmux 会话 `g1_panel_oneclick` 里，输出经 `pipe-pane` 落到导航机 `/tmp/fqplanner_dream_oneclick.log`，面板 `tail -F` 它，所以面板重启不会带走编排也不会丢日志。**两次站立 Enter 必须在提示出现后 600 秒内按完**（工作流 `g1_fixed_map_relocalize_navigation_workflow.sh:563`），超时脚本会打 `POSE ready file was not created; navigation remains locked.` 并退出，9882 就不会起。编排已退出但真机还站着时，面板不提供恢复按钮（太少用、且必须先 stop 才能跑）。在导航机上手敲：`bash tools/g1_three_party_oneclick.sh stop` 然后 `bash tools/g1_three_party_oneclick.sh resume`，resume 不重起 SONIC / relay / 相机 / 灵巧手，只重起 DREAM。VLA 卡片仍可单独启停 4090 HTTP/relay。**不会发布接待任务。**

## 开机自启（只起面板）

```bash
sudo apt install tmux
sudo bash web/install_panel.sh
```

日常：

```bash
sudo systemctl enable --now fqplanner-panel
sudo systemctl restart fqplanner-panel
sudo systemctl status fqplanner-panel
sudo journalctl -u fqplanner-panel -f
```

单元没装时 `systemctl restart` 会报 `Unit not found`。装好之前：

```bash
.venv/bin/python web/app.py
```

业务进程开机后默认全停，在面板里用「重启」拉起、「停止」关掉。**每个服务一个独立 tmux session**，名字就是服务 id：

```bash
tmux ls
tmux attach -t redis
tmux attach -t master
tmux attach -t deploy
tmux attach -t feishu
tmux attach -t slaver
tmux attach -t desk
tmux attach -t mujoco
```

所有卡片日志都在 `log/YYYY-MM-DD/<服务>/`。面板只读这个目录，不再把 tmux 窗口当主日志。飞书不再写 `integrations/feishu/runtime/feishu.log`。DREAM / VLA 的探测和 SSH 输出写在当天的 `monitor.log`。

`systemctl restart fqplanner-panel` 只重拉监视进程（单元 `KillMode=process`），不会停止 Redis / Master 等业务。

Redis 的启动日志在 `log/YYYY-MM-DD/redis/*.log`。`tmux attach -t redis` 里若只有一行 `[log] redis -> …`，去看这个文件，或重启 Redis 让窗口同步打出 `Ready to accept connections`。

重启 Master 会清空 Redis 协作库（`collaborator.clear=true`），面板会弹出确认。
