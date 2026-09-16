# FQPlanner Linux 控制面板

独立进程面板，默认监听 `0.0.0.0:5678`。只启停本机 tmux 进程、查看日志、探测 DREAM / VLA 健康。**不会发布接待任务。**

第一版没有登录。任何能访问 5678 的局域网设备都可以启停 Redis / Master / Deploy / 飞书 / Slaver。请只在可信网络使用。

Redis 和 Master 是大脑核心；Deploy 和飞书只是给 Master 下任务的入口。真机层卡片只能看 DREAM / VLA 通不通。

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
```

Redis 的启动日志在 `log/YYYY-MM-DD/redis/*.log`。`tmux attach -t redis` 里若只有一行 `[log] redis -> …`，去看这个文件，或重启 Redis 让窗口同步打出 `Ready to accept connections`。

重启 Master 会清空 Redis 协作库（`collaborator.clear=true`），面板会弹出确认。
