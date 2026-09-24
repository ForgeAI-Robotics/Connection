#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
UNIT_SRC="$ROOT/infra/systemd/fqplanner-panel.service"
UNIT_DST="/etc/systemd/system/fqplanner-panel.service"
PYTHON="${PANEL_PYTHON:-$ROOT/venv/core/bin/python}"
if [[ -n "${PANEL_USER:-}" ]]; then
  USER_NAME="$PANEL_USER"
else
  USER_NAME="${SUDO_USER:-$(id -un)}"
fi
if [[ -n "${PANEL_GROUP:-}" ]]; then
  GROUP_NAME="$PANEL_GROUP"
else
  GROUP_NAME="$(id -gn "$USER_NAME")"
fi

if [[ ! -x "$PYTHON" ]]; then
  echo "找不到面板 Python: $PYTHON" >&2
  exit 1
fi

if ! command -v tmux >/dev/null 2>&1; then
  echo "未安装 tmux。请先执行: sudo apt install tmux" >&2
  exit 1
fi

if [[ $EUID -ne 0 ]]; then
  echo "需要 root 才能安装 systemd 开机自启。请执行: sudo $0" >&2
  exit 1
fi

tmp="$(mktemp)"
sed \
  -e "s|@ROOT@|$ROOT|g" \
  -e "s|@PYTHON@|$PYTHON|g" \
  -e "s|@USER@|$USER_NAME|g" \
  -e "s|@GROUP@|$GROUP_NAME|g" \
  "$UNIT_SRC" > "$tmp"
install -m 644 "$tmp" "$UNIT_DST"
rm -f "$tmp"

systemctl daemon-reload
systemctl enable --now fqplanner-panel.service
systemctl --no-pager --full status fqplanner-panel.service || true
echo
echo "面板已开机自启: http://0.0.0.0:5678"
echo "业务进程不会随开机启动，请在面板里按需点启动。"
echo "systemctl restart fqplanner-panel 只重拉监视进程，不会停止 Redis/Master 等业务。"
echo "本机查看日志: logs/YYYY-MM-DD/<服务>/"
