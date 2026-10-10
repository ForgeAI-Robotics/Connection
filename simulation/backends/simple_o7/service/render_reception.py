"""Render a reception episode video by replaying the recorded MuJoCo states.

Every frame is the recorded full qpos of the physical run (no interpolation or
generated motion). Captions name the brain command each segment executed and the
measured outcome from segments.json. Paused simulation time between commands is
shown as a short hold on the last frame of the segment.
"""
import argparse
import json
import os
import subprocess
from pathlib import Path

STEPS = {
    "nav_table2": "① 导航到 table_2（取物桌）",
    "pick": "② 抓取 cola_can_1",
    "nav_relay2": "③ 搬运导航到 relay2（门前）",
    "nav_relay3": "④ 横移到 relay3（过门）",
    "nav_table1": "⑤ 搬运导航到 table_1（目标桌）",
    "place": "⑥ 放置 cola_can_1",
}


def attempt_ranges(report, commands):
    """Bind each contiguous recorded attempt to its own result and command."""
    offset, counts, result = 0, {}, []
    for entry in report['segments']:
        segment = entry['segment']
        attempt = counts.get(segment, 0)
        counts[segment] = attempt + 1
        info = commands.get(segment) or {}
        attempts = info.get('attempts') or []
        # A legacy last-command mapping cannot identify earlier retries.
        if attempt < len(attempts):
            info = attempts[attempt]
        elif attempt or info.get('attempt_count', 1) > 1:
            info = {}
        end = offset + entry['frames']
        if end > offset:
            result.append((offset, end, entry, info, attempt + 1))
        offset = end
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("episode", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--commands", type=Path, help="JSON {segment: {command_id, verdict}} from the brain ledger")
    parser.add_argument("--font", default="/opt/connection-simple-o7/runtime/fonts/NotoSansCJK-Regular.ttc")
    parser.add_argument("--title", default="开始接待 · Connection 大脑 → SIMPLE 物理仿真（G1 + O6 灵巧手 · SONIC · MuJoCo）")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=25)
    args = parser.parse_args()
    os.environ.setdefault("MUJOCO_GL", "egl")
    import numpy as np
    import mujoco
    from PIL import Image, ImageDraw, ImageFont

    model = mujoco.MjModel.from_binary_path(str(args.episode / "execution_model.mjb"))
    model.vis.global_.offwidth = max(model.vis.global_.offwidth, args.width)
    model.vis.global_.offheight = max(model.vis.global_.offheight, args.height)
    # Brighter headlight only; geometry, materials and recorded states are unchanged.
    model.vis.headlight.ambient[:] = [.45, .45, .45]
    model.vis.headlight.diffuse[:] = [.65, .65, .65]
    data = mujoco.MjData(model)
    trace = np.load(args.episode / "trajectory.npz")
    qpos, times, segments, phases = trace["full_qpos"], trace["time"], trace["segment"], trace["phase"]
    report = json.loads((args.episode / "segments.json").read_text())
    if (report.get('provenance') or {}).get('scene') == 'office_v2':
        # The authored office floor supplies the visual surface; physics still
        # used the original ground collider at exactly the same elevation.
        model.geom_rgba[model.geom('ground').id, 3] = 0.
    commands = json.loads(args.commands.read_text()) if args.commands else {}
    attempts = attempt_ranges(report, commands)
    if not attempts or attempts[-1][1] != len(qpos):
        raise ValueError('Recorded attempt frame counts do not match trajectory')
    renderer = mujoco.Renderer(model, height=args.height, width=args.width)
    render_options = mujoco.MjvOption()
    render_options.geomgroup[5] = 0  # office ceiling omitted only from the camera view
    inset_size = (args.width // 3, args.height // 3)
    inset = mujoco.Renderer(model, height=inset_size[1], width=inset_size[0])
    hand_camera = mujoco.MjvCamera()
    hand_camera.distance, hand_camera.azimuth, hand_camera.elevation = .75, 250., -30.
    hand_body = "left_o6_hand_base_link"
    big, small = ImageFont.truetype(args.font, 30), ImageFont.truetype(args.font, 22)
    stride = max(1, round(50 / args.fps))
    output = args.output or args.episode / "reception.mp4"
    encoder = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
                                "-s", f"{args.width}x{args.height}", "-r", str(args.fps), "-i", "-",
                                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", str(output)],
                               stdin=subprocess.PIPE)
    # Left-rear, high tracking view: keeps the carried can (left hand) and both tables in frame
    # without placing the camera outside the room walls.
    camera = mujoco.MjvCamera()
    camera.distance, camera.azimuth, camera.elevation = 2.6, 285., -40.
    lookat = None

    def caption(image, index, attempt):
        segment = str(segments[index])
        draw = ImageDraw.Draw(image, "RGBA")
        draw.rectangle([0, 0, args.width, 96], fill=(10, 14, 24, 200))
        draw.text((20, 10), args.title, font=small, fill=(220, 228, 240))
        line = STEPS.get(segment, segment)
        _, _, entry, info, attempt_number = attempt
        if attempt_number > 1:
            line += f'（第 {attempt_number} 次尝试）'
        if info.get("command_id"):
            line += "   " + info["command_id"]
        draw.text((20, 44), line, font=big, fill=(255, 255, 255))
        verdict = info.get("verdict") or (entry.get("outcome") == "succeeded" and "物理核验通过")
        footer = f"仿真时间 {times[index]:6.2f} s   阶段 {phases[index]}"
        draw.rectangle([0, args.height - 44, args.width, args.height], fill=(10, 14, 24, 190))
        draw.text((20, args.height - 38), footer, font=small, fill=(200, 210, 225))
        return draw, entry, verdict

    def write(frame):
        encoder.stdin.write(np.asarray(frame, dtype=np.uint8).tobytes())

    indices = list(range(0, len(qpos), stride))
    attempt_index = 0
    for n, index in enumerate(indices):
        while index >= attempts[attempt_index][1]:
            attempt_index += 1
        attempt = attempts[attempt_index]
        data.qpos[:] = qpos[index]
        mujoco.mj_forward(model, data)
        target = data.body("pelvis").xpos.copy()
        target[2] += .1
        lookat = target if lookat is None else .92 * lookat + .08 * target
        camera.lookat[:] = lookat
        renderer.update_scene(data, camera=camera, scene_option=render_options)
        image = Image.fromarray(renderer.render())
        hand_camera.lookat[:] = data.body(hand_body).xpos
        inset.update_scene(data, camera=hand_camera, scene_option=render_options)
        close = Image.fromarray(inset.render())
        corner = (args.width - inset_size[0] - 16, args.height - inset_size[1] - 56)
        image.paste(close, corner)
        ImageDraw.Draw(image).rectangle([corner[0] - 2, corner[1] - 2, corner[0] + inset_size[0] + 1,
                                         corner[1] + inset_size[1] + 1], outline=(230, 230, 230), width=2)
        draw, entry, verdict = caption(image, index, attempt)
        last_of_segment = n + 1 == len(indices) or indices[n + 1] >= attempt[1]
        write(image)
        if n == 0 or last_of_segment:
            image.save(args.episode / f'preview-{str(segments[index])}-{attempt[4]}.jpg')
        if last_of_segment and entry:
            checks = entry.get('checks') or {}
            ok = entry.get("outcome") == "succeeded" and not any(checks.get(k) is False for k in
                    ('reached', 'object_retained', 'object_grasped', 'object_at_target', 'released'))
            badge = ("✓ " + str(verdict)) if ok and verdict else ("✕ " + str(entry.get("error") or '物理效果核验未通过'))
            draw.rectangle([20, 110, 20 + 26 * len(badge) + 30, 160], fill=(22, 120, 70, 220) if ok else (160, 40, 40, 220))
            draw.text((36, 116), badge, font=big, fill=(255, 255, 255))
            for _ in range(int(args.fps * 1.2)):
                write(image)
    encoder.stdin.close()
    if encoder.wait() != 0:
        raise RuntimeError('Video encoder failed')
    renderer.close()
    inset.close()
    print(json.dumps({"video": str(output), "frames_rendered": len(indices), "fps": args.fps}))


if __name__ == "__main__":
    main()
