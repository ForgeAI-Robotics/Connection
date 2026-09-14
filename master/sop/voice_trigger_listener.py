"""语音触发监听器 —— 把「小达小达,开始接待」接到大脑接待程序(不碰 HARIX 平台)。

原理(见 build/DEPLOY.md):harix-openrcu 唤醒后云端 ASR,识别文本会打进日志(`asr: recv ... text="..."`)。
本脚本 tail 那个日志,抓到 ASR 文本里含【接待关键词】就 POST 触发大脑接待。绕开 HARIX 平台配意图。

部署拓扑:
  G1(Linux):跑 build(语音唤醒+ASR)  +  本脚本(tail 日志 → POST)
  Mac(大脑):跑 deploy(:8888,收 /api/reception/run)
  → 说"小达小达,开始接待" → build 日志出 text="开始接待" → 本脚本命中 → POST 到 Mac 触发接待

用法(在 G1 上,build 已 start.sh 起来后):
  python3 voice_trigger_listener.py --log <build目录>/logs/rcu.log --brain http://<Mac的IP>:8888
  (Mac IP 现在是 10.11.32.178;日志路径按 build 部署位置,DEPLOY.md 默认 logs/rcu.log 或 logs/start.log)
"""

import argparse
import json
import re
import time
import urllib.request

# 命中这些词就触发接待(按现场语音习惯加)
TRIGGER_WORDS = ["接待", "开始接待", "迎宾", "会议接待", "准备会议"]
# 防抖:两次触发至少间隔这么多秒(避免一句话被多行日志重复触发)
COOLDOWN_S = 20.0


def tail_lines(path):
    """像 tail -f:从文件末尾开始,持续吐新行(文件被轮转/重建也重开)。"""
    while True:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                f.seek(0, 2)
                while True:
                    line = f.readline()
                    if line:
                        yield line
                    else:
                        time.sleep(0.3)
        except FileNotFoundError:
            print(f"[等待日志出现] {path}", flush=True)
            time.sleep(1.0)


def trigger_reception(brain_url):
    req = urllib.request.Request(
        brain_url.rstrip("/") + "/api/reception/run",
        data=json.dumps({"scenario": "normal", "reflect": False}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", required=True, help="build 的日志文件(logs/rcu.log 或 logs/start.log)")
    ap.add_argument("--brain", default="http://10.11.32.178:8888", help="大脑 deploy 地址")
    args = ap.parse_args()

    # 抓 ASR 识别文本;build 日志里 ASR 行形如 `asr: recv ... text="..."`
    asr_re = re.compile(r'text="([^"]+)"')
    print(f"监听 {args.log}\n触发词 {TRIGGER_WORDS} → POST {args.brain}/api/reception/run", flush=True)

    last_fire = 0.0
    for line in tail_lines(args.log):
        m = asr_re.search(line)
        if not m:
            continue
        text = m.group(1)
        print(f"  识别到: {text}", flush=True)
        if any(w in text for w in TRIGGER_WORDS):
            now = time.time()
            if now - last_fire < COOLDOWN_S:
                print("  (冷却中,跳过)", flush=True)
                continue
            last_fire = now
            print("  ★ 命中触发词 → 触发接待", flush=True)
            try:
                r = trigger_reception(args.brain)
                print(f"  接待已触发: success={r.get('success')}", flush=True)
            except Exception as exc:
                print(f"  触发失败: {exc}", flush=True)


if __name__ == "__main__":
    main()
