"""NX 语音触发接待 —— M260 USB 麦克风 → 云 ASR → 关键词"开始接待" → 触发大脑接待。

在 G1 的 NX(Jetson Orin)上常驻运行,循环:录一段音 → 音量门限(VAD)滤掉静音/底噪 →
qwen-omni 云 ASR 转文字 → 文字含"开始接待"就 POST 给大脑 deploy 的 /publish_task 触发接待。
部署 / 依赖 / 运维 / 为什么不用板载麦,见同目录 README.md。

可选环境变量:
  DEPLOY_URL    大脑 deploy 的触发地址(默认见下;交接到别的机器要改成对应 deploy 机的 IP:8888)
  VLM_KEY_FILE  key 文件路径(默认 ~/.vlm_key,内容 = dashscope 国际版兼容模式的 key)
运行: python3 reception_voice.py    (常驻建议: nohup python3 -u reception_voice.py >voice.log 2>&1 </dev/null &)
"""
import os, json, base64, subprocess, urllib.request, time, wave, audioop

# ---------------- 配置(换机器 / 调参改这里) ----------------
DEPLOY = os.environ.get('DEPLOY_URL', 'http://192.168.0.229:8888/publish_task')  # 大脑 deploy 触发地址
KEY = open(os.path.expanduser(os.environ.get('VLM_KEY_FILE', '~/.vlm_key'))).read().strip()  # dashscope 兼容模式 key
ASR_URL = 'https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions'
ASR_MODEL = 'qwen3-omni-flash'   # sk-ws- 型 key 只能走兼容模式,音频只能用 omni 系列(不支持 paraformer 原生)
SEG = 5                # 每段录音秒数
SILENCE_RMS = 800      # 音量 RMS 门限:低于=静音/底噪,跳过 ASR(省 API + 防 qwen 对底噪幻觉)。实测说话 rms 1200~1400
TRIGGER = '开始接待'   # 触发词(去标点空格后精确匹配,避免"不要接待"等误触发)
HALLUC = ['提升自己的幸福感', '通过一些简单的方法', '我今天要讲的是', '[空]', '没有清晰']  # qwen 对噪音的高频幻觉句,命中即丢
# ----------------------------------------------------------


def _find_card():
    """动态认 M260(XFM)的声卡号:NX 重启后卡号会变(见过 3→1),不能写死。"""
    import re
    out = subprocess.run(['arecord', '-l'], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if 'XFM' in line:
            m = re.search(r'card (\d+)', line)
            if m:
                return m.group(1)
    return '1'


CARD = _find_card()


def _clean(s):
    for c in ' ，。,.!！?？、':
        s = s.replace(c, '')
    return s


def rms_of(p):
    w = wave.open(p, 'rb')
    fr = w.readframes(w.getnframes())
    w.close()
    return audioop.rms(fr, 2)


def record(p, s):
    subprocess.run(['arecord', '-D', 'plughw:' + CARD + ',0', '-f', 'S16_LE',
                    '-r', '16000', '-c', '1', '-d', str(s), p], stderr=subprocess.DEVNULL)


def asr(p):
    a = base64.b64encode(open(p, 'rb').read()).decode()
    body = {'model': ASR_MODEL, 'messages': [{'role': 'user', 'content': [
        {'type': 'input_audio', 'input_audio': {'data': 'data:;base64,' + a, 'format': 'wav'}},
        {'type': 'text', 'text': '转写这段音频里真实听到的中文。若没有清晰的人在说话(只有噪音或静音),只回复[空]二字,禁止编造或补全。'}]}],
        'stream': True, 'modalities': ['text']}
    req = urllib.request.Request(ASR_URL, data=json.dumps(body).encode(),
                                 headers={'Authorization': 'Bearer ' + KEY, 'Content-Type': 'application/json'})
    t = ''
    for ln in urllib.request.urlopen(req, timeout=40):
        ln = ln.decode().strip()
        if ln.startswith('data:'):
            d = ln[5:].strip()
            if d == '[DONE]':
                break
            try:
                t += json.loads(d)['choices'][0]['delta'].get('content', '')
            except Exception:
                pass
    return t


def trigger():
    req = urllib.request.Request(DEPLOY, data=json.dumps({'task': '开始接待'}).encode('utf-8'),
                                 headers={'Content-Type': 'application/json'})
    return urllib.request.urlopen(req, timeout=15).status


def main():
    print('[voice] listening...', flush=True)
    while True:
        try:
            record('/tmp/seg.wav', SEG)
            level = rms_of('/tmp/seg.wav')
            if level < SILENCE_RMS:   # 静音/底噪,跳过 ASR(省 API + 避免幻觉)
                continue
            txt = asr('/tmp/seg.wav')
            if any(h in txt for h in HALLUC):   # 丢掉 qwen 对噪音的幻觉句
                continue
            if txt.strip():
                print('  heard(rms=%d):' % level, txt, flush=True)
            if TRIGGER in _clean(txt):
                print('  >> hit! trigger reception', flush=True)
                print('  trigger HTTP', trigger(), flush=True)
                time.sleep(8)   # 触发后冷却,防连发
        except Exception as e:
            print('  err:', str(e)[:70], flush=True)
            time.sleep(2)


if __name__ == '__main__':
    main()
