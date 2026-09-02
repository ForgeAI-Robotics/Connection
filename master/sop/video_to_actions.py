"""人类示教视频 → 动作序列 —— 自己实现(照徐鑫《短时机器人第一人称视频开放词汇动作解析》方案的简化版)。

徐鑫离职,但他方案文档把方法和输出格式都写清了 → 照着用你现成的 qwen-vl(vlm_judge 那套)自己做:
  全局抽帧(2fps)→ qwen-vl 整体理解 → 输出带时间的【开放词汇】动作序列(不预设固定动作类别)。
接待动作少、粗粒度,做他的【简化版】(不搞光流切分 / 多轮自校正,一次 VLM 全局输出)就够。

输出格式对齐徐鑫方案(直接喂 learn_from_demo):
  {"task_summary": "...",
   "segments": [{"start_time","end_time","action","object","confidence"}, ...]}

依赖:opencv-python(抽帧,`pip install opencv-python`,macOS 12 也秒装)或系统 ffmpeg(退路)
     + 现成 qwen-vl(VLM_API_KEY/dashscope,见 vlm_judge)。

用法:
  python video_to_actions.py 示范视频.mp4                    # → demo_actions.json
  python video_to_actions.py 示范视频.mp4 -o out.json --fps 2
  之后: python learn_from_demo.py demo_actions.json          # 动作序列 → 更新 SOP
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import vlm_judge                                   # noqa: E402  复用 _endpoint_cfg / _api_key / _to_jpg_b64

_TASK_HINT = "会议接待补货:人从茶水间取可乐、搬运到会议室、在桌上间隔摆放"

_PROMPT = """你是机器人的「第一人称视频动作解析」模块。下面是一段人类示范【{hint}】的第一人称视频,
按时间顺序均匀抽的帧(每帧前标了时间秒)。请从整体理解这段示范,输出【开放词汇】动作序列——
不预设固定动作类别,根据视频内容自己命名动作(如"走到茶水间冰箱前""取出可乐""搬运可乐""放置可乐""调整间距")。

要求:
- 每个动作段给:起止时间(秒,参考帧上标的时间)、动作名(动词开头、简短)、交互对象、置信度(0-1);
- 动作按时间先后、不重叠;粗粒度即可(整段示范约 5-10 个动作段);合并连续相同的动作。

只输出 JSON,不要其它文字:
{{"task_summary": "一句话概括整段示范",
  "segments": [{{"start_time": 0.0, "end_time": 2.0, "action": "...", "object": "...", "confidence": 0.9}}]}}"""


def _downsample(items, max_frames):
    """均匀降到 max_frames 张(qwen-vl 多图上限)。"""
    if len(items) > max_frames:
        step = len(items) / max_frames
        items = [items[int(i * step)] for i in range(max_frames)]
    return items


def _extract_frames(video, fps, max_frames, tmpdir):
    """抽帧(缩到宽 1024)→ [(路径, 时间秒)]。优先 opencv-python(pip 装、不碰系统 ffmpeg,
    macOS 12 也能用);没装 cv2 才退回系统 ffmpeg。"""
    try:
        import cv2
    except ImportError:
        return _extract_ffmpeg(video, fps, max_frames, tmpdir)

    cap = cv2.VideoCapture(video)
    if not cap.isOpened():
        raise RuntimeError(f"打不开视频: {video}")
    vfps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    step = max(1, round(vfps / fps))
    items, idx = [], 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if idx % step == 0:
            h, w = frame.shape[:2]
            if w > 1024:
                frame = cv2.resize(frame, (1024, int(h * 1024 / w)))
            p = os.path.join(tmpdir, f"f_{len(items):05d}.jpg")
            cv2.imwrite(p, frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            items.append((p, idx / vfps))
        idx += 1
    cap.release()
    if not items:
        raise RuntimeError(f"没抽到帧(视频损坏?): {video}")
    return _downsample(items, max_frames)


def _extract_ffmpeg(video, fps, max_frames, tmpdir):
    """退路:系统 ffmpeg 抽帧(macOS 12 装不上就用上面的 cv2)。"""
    pat = os.path.join(tmpdir, "f_%05d.jpg")
    r = subprocess.run(
        ["ffmpeg", "-i", video, "-vf", f"fps={fps},scale=1024:-1", "-q:v", "3", pat],
        capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("抽帧失败:没装 opencv-python,系统 ffmpeg 也用不了。\n"
                           "→ 直接 `pip install opencv-python`(有预编译轮子、macOS 12 秒装),别折腾 brew ffmpeg。\n"
                           f"ffmpeg stderr: {r.stderr[-300:]}")
    files = sorted(f for f in os.listdir(tmpdir) if f.startswith("f_"))
    items = [(os.path.join(tmpdir, f), i / fps) for i, f in enumerate(files)]
    return _downsample(items, max_frames)


def video_to_actions(video, fps=2.0, max_frames=24, hint=_TASK_HINT):
    """视频 → {task_summary, segments:[{start_time,end_time,action,object,confidence}]}。"""
    ep = vlm_judge._endpoint_cfg()
    key = vlm_judge._api_key(ep["key_names"])
    if not key:
        raise RuntimeError(f"无 VLM API key(需环境变量 {' 或 '.join(ep['key_names'])})")

    with tempfile.TemporaryDirectory() as td:
        frames = _extract_frames(video, fps, max_frames, td)
        print(f"抽帧 {len(frames)} 张(fps={fps}, 缩到宽 1024)", flush=True)
        content = [{"type": "text", "text": _PROMPT.format(hint=hint)}]
        for path, t in frames:
            content.append({"type": "text", "text": f"[t={t:.1f}s]"})
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{vlm_judge._to_jpg_b64(path)}"}})
        body = json.dumps({"model": ep["model"],
                           "messages": [{"role": "user", "content": content}],
                           "temperature": 0}).encode("utf-8")
        req = urllib.request.Request(
            ep["api_base"].rstrip("/") + "/chat/completions", data=body, method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
        print(f"调 {ep['model']} 解析动作序列…", flush=True)
        with urllib.request.urlopen(req, timeout=120) as r:
            out = json.loads(r.read())["choices"][0]["message"]["content"]

    m = re.search(r"\{.*\}", out, re.S)
    return json.loads(m.group(0)) if m else {"task_summary": "", "segments": []}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video", help="人类示范第一人称视频(mp4/mov)")
    ap.add_argument("-o", "--out", default=os.path.join(_HERE, "demo_actions.json"))
    ap.add_argument("--fps", type=float, default=2.0)
    ap.add_argument("--max-frames", type=int, default=24)
    args = ap.parse_args()

    data = video_to_actions(args.video, fps=args.fps, max_frames=args.max_frames)
    print(f"\n示范摘要: {data.get('task_summary','')}")
    print(f"动作序列({len(data.get('segments', []))} 段):")
    for s in data.get("segments", []):
        print(f"  · {s.get('start_time','?')}~{s.get('end_time','?')}s "
              f"{s.get('action','')} [{s.get('object','')}] (conf {s.get('confidence','?')})")
    json.dump(data, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n→ {os.path.relpath(args.out, os.path.dirname(os.path.dirname(_HERE)))}")
    print(f"下一步: python learn_from_demo.py {args.out}")


if __name__ == "__main__":
    main()
