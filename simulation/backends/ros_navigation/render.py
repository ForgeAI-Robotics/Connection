"""Render recorded MuJoCo poses with the actual Brain task/attempt timeline."""
import argparse
import bisect
import json
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('reports', type=Path)
    parser.add_argument('--model', default='/project/office_video_v2/scene/mission.xml')
    parser.add_argument('--ffmpeg', default='/project/http_nav/ffmpeg')
    parser.add_argument('--fps', type=int, choices=[20, 50], default=20)
    parser.add_argument('--manipulation', action='store_true',
                        help='Label the real GR00T experiment and show saved policy RGB inputs')
    args = parser.parse_args()
    import cv2
    import mujoco
    import numpy as np
    out = args.reports
    rows = json.loads((out / 'trajectory.json').read_text())
    events = [json.loads(line) for line in (out / 'brain_bridge.jsonl').read_text().splitlines()]
    submissions = [row for row in events if row['method'] == 'POST' and row['path'] == '/v1/navigation/goals']
    from datetime import datetime
    start = min(datetime.fromisoformat(row['time']).timestamp() for row in submissions) - 2
    rows = [row for row in rows if row['wall_time'] >= start]
    if len(rows) < 2 or not np.allclose(np.diff([r['simulation_time'] for r in rows]),
                                       1 / args.fps, atol=1e-6):
        raise ValueError('Video fps must match the recorded physics sampling rate')
    history = [json.loads(line) for line in (out / 'brain_status_history.jsonl').read_text().splitlines()]
    history.sort(key=lambda row: row['time'])
    times = [row['time'] for row in history]
    final = json.loads((out / 'brain_status.json').read_text())
    policy_rows = json.loads((out / 'inference_timings.json').read_text()) if args.manipulation and (out / 'inference_timings.json').exists() else []
    policy_times = [row['simulation_time'] for row in policy_rows]
    policy_index, policy_views = -1, {}
    model = mujoco.MjModel.from_xml_path(args.model)
    data = mujoco.MjData(model)
    model.vis.global_.offwidth, model.vis.global_.offheight = 1280, 720
    robot = model.body('pelvis').id
    for index in range(model.ngeom):
        name = model.geom(index).name
        if ((model.body_rootid[model.geom_bodyid[index]] == robot and model.geom_group[index] != 1)
                or name.startswith(('ceiling', 'downlight_fixture'))
                or name in ('meeting_panel', 'lobby_luminous_panel')):
            model.geom_rgba[index, 3] = 0
    model.site_rgba[:, 3] = 0
    camera = mujoco.MjvCamera()
    camera.distance, camera.azimuth, camera.elevation = 7., 135., -65.
    output = out / 'brain_ros_navigation.mp4'
    with (out / 'video_encoding.log').open('w') as log:
        encoder = subprocess.Popen([args.ffmpeg, '-n', '-f', 'rawvideo', '-pix_fmt', 'rgb24',
            '-s', '1280x720', '-r', str(args.fps), '-i', '-', '-an', '-c:v', 'libx264', '-preset', 'fast',
            '-crf', '19', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(output)],
            stdin=subprocess.PIPE, stderr=log)
        smooth = np.array(rows[0]['qpos'][:2])
        snapshots = set()
        with mujoco.Renderer(model, height=720, width=1280) as renderer:
            try:
                for index, row in enumerate(rows):
                    data.qpos[:] = row['qpos']
                    if model.nmocap:
                        data.mocap_pos[:] = row['mocap_pos']
                    mujoco.mj_forward(model, data)
                    alpha = .9 ** (20 / args.fps)
                    smooth = alpha * smooth + (1 - alpha) * data.qpos[:2]
                    camera.lookat[:] = [*smooth, .65]
                    renderer.update_scene(data, camera)
                    frame = renderer.render().copy()
                    slot = bisect.bisect_right(times, row['wall_time']) - 1
                    status = history[max(0, slot)]['status']
                    state, phase = status.get('state', ''), status.get('phase', '')
                    failed = state in ('recovery_required', 'failed', 'waiting_human')
                    color = (255, 120, 100) if failed else (180, 235, 220)
                    cv2.rectangle(frame, (0, 0), (1280, 130), (16, 25, 34), -1)
                    texts = [
                        ('Brain > ROS navigation + GR00T > SONIC > MuJoCo' if args.manipulation else 'Brain > 8001 > 9882 > ROS navigation > SONIC > MuJoCo', (245, 245, 245)),
                        (f'{"RECEPTION / GR00T" if args.manipulation else "NAVIGATION ONLY"} | {state} | {phase}', color),
                        (f"sim={row['simulation_time']:.1f}s | mode={row.get('motion_mode')} | ROS={row['nav_state']}", (160, 220, 245)),
                        (f"command={status.get('control_command_id') or '-'} | retries={status.get('attempt_summary', {}).get('retry_count', 0)}", (245, 210, 150))]
                    for n, (text, text_color) in enumerate(texts):
                        cv2.putText(frame, text, (20, 28 + 30*n), cv2.FONT_HERSHEY_SIMPLEX,
                                    .70 if n == 0 else .52, text_color, 2 if n == 0 else 1)
                    if 3.8 < data.qpos[0] < 8.5 and data.qpos[1] > 11.:
                        detail_camera = mujoco.MjvCamera()
                        detail_camera.distance, detail_camera.azimuth, detail_camera.elevation = 3.6, 90, -80
                        detail_camera.lookat[:] = [5.6, 11.8, .7]
                        renderer.update_scene(data, detail_camera)
                        detail = renderer.render().copy()
                        frame[365:695, 930:1260] = cv2.resize(detail[140:580, 420:860], (330, 330))
                        cv2.rectangle(frame, (929, 364), (1261, 696), (240, 220, 140), 2)
                    if policy_times and row['simulation_time'] >= policy_times[0]:
                        selected = bisect.bisect_right(policy_times, row['simulation_time']) - 1
                        if selected != policy_index:
                            sequence = policy_rows[selected]['sequence']
                            with np.load(out / f'input_{sequence:04}.npz') as inputs:
                                policy_views = {key: inputs['video_' + key][0, 0].copy()
                                                for key in ('ego_view', 'left_wrist')}
                            policy_index = selected
                        for key, y in [('ego_view', 155), ('left_wrist', 440)]:
                            frame[y:y + 270, 900:1260] = cv2.resize(policy_views[key], (360, 270))
                            cv2.putText(frame, f'{key}: last actual policy RGB', (900, y - 5),
                                        cv2.FONT_HERSHEY_SIMPLEX, .43, (255, 230, 160), 1)
                    encoder.stdin.write(frame.tobytes())
                    key = (phase, state)
                    if key not in snapshots or index == len(rows) - 1:
                        cv2.imwrite(str(out / f'preview-{index:05d}.jpg'), cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))
                        snapshots.add(key)
                    if index % 400 == 0:
                        print('RENDER', index, len(rows), flush=True)
            finally:
                encoder.stdin.close()
                if encoder.wait() != 0:
                    raise RuntimeError('Video encoder failed')
    capture = cv2.VideoCapture(str(output))
    frames = 0
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        assert frame.shape == (720, 1280, 3)
        frames += 1
    capture.release()
    assert frames == len(rows)
    metadata = dict(video=str(output), frames=frames, fps=args.fps, duration_s=frames/args.fps,
                    task_id=final['task_id'], brain_state=final['state'], attempts=final.get('attempt_summary'),
                    source='recorded physical qpos; no interpolation or hand-authored motion',
                    policy_panels='last actual saved RGB input held until next inference frame' if args.manipulation else None,
                    omitted='only initial idle before first Brain navigation submission minus two seconds',
                    first_sim_time=rows[0]['simulation_time'], last_sim_time=rows[-1]['simulation_time'])
    (out / 'video_validation.json').write_text(json.dumps(metadata, indent=2))
    print(json.dumps(metadata), flush=True)


if __name__ == '__main__':
    main()
