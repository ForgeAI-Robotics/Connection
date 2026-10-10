"""One persistent O6 SONIC reception episode, advanced one contract segment at a time.

The configuration mirrors the frozen sonic_release_v1 validation of the left-hand
cross-table task. Between brain commands the simulation clock is paused: the
physical state stays in memory and is never reset or re-created inside a task.
Evidence is measured from MuJoCo state at the end of each segment.
"""
import hashlib
import inspect
import json
import time
from pathlib import Path

import numpy as np

RELEASE = "/mnt/simple/generated-data/o6-planner-production-20260923/sonic_release_v1"


class SegmentFailed(RuntimeError):
    pass


class Episode:
    def __init__(self, output, *, seed=601, data_root="/mnt/simple/data", room=RELEASE + "/formal_room/room.json",
                 max_frames=12000, log=print, scene='formal_room', office_assets=None):
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=True)
        self.log = log
        self.seed = seed
        import simple.utils
        simple.utils.get_data_dir = lambda: Path(data_root).resolve()
        import simple.envs  # noqa: F401  (registers environments)
        import gymnasium as gym
        import mujoco
        import torch
        from simple.robots.g1_o6_mp import G1O6MPWholebody
        from simple.tasks.g1_o6_can_pick_mp import G1O6CanPickMP
        from simple.tasks.g1_o6_cross_table_mp import G1O6CrossTableMP
        from simple.dr.types import Box
        from simple.agents.mp import MotionPlannerAgent
        from simple.mp.curobo import CuRoboPlanner
        from service.reception_task import G1O6ReceptionMP, SIM_GOALS, SPAWN, SONIC_STOP_OVERSHOOT, segment_of
        self.scene = scene
        if scene == 'office_v2':
            if not office_assets:
                raise ValueError('office_v2 requires a snapshot of office scene assets')
            from service.office_layout import prepare_room, install_visuals, SIM_GOALS, SPAWN
            from service.office_task import G1O6OfficeReceptionMP, segment_of
            room = prepare_room(office_assets, self.output)
            install_visuals()
            G1O6ReceptionMP = G1O6OfficeReceptionMP
            max_frames = max(max_frames, 45000)
        elif scene != 'formal_room':
            raise ValueError('Unknown reception scene: ' + scene)
        self.segment_of = segment_of
        self.mujoco, self.goals = mujoco, SIM_GOALS
        torch.manual_seed(seed)
        # Same switches as validate_o6_sonic_suite.py for the left-hand cross_table case.
        G1O6CanPickMP.state_driven_phases = True
        G1O6CanPickMP.formal_room_manifest = Path(room)
        for cls in (G1O6CanPickMP, G1O6CrossTableMP, G1O6ReceptionMP):
            cls.sensor_cfgs = {}
            cls.dr_cfgs["spatial"].target_region = Box(low=[-.30, .08], high=[-.30, .08])
        G1O6MPWholebody.additional_sensor_configs = staticmethod(lambda: {})
        G1O6MPWholebody.world_tracking_enabled = True
        G1O6MPWholebody.move_plan_attempts = 8
        G1O6MPWholebody.torso_feedback_enabled = True
        G1O6MPWholebody.base_hold_enabled = False
        G1O6MPWholebody.torso_collision_padding = 0.
        G1O6MPWholebody.torso_avoidance_enabled = False
        G1O6MPWholebody.interhand_avoidance_enabled = True
        G1O6MPWholebody.hand_target_speed_rad_s = 1.
        G1O6MPWholebody.staged_hand_opening = True
        G1O6MPWholebody.world_tracking_iterations = 10
        env_id = "simple/G1O6ReceptionMP-v0" if scene == 'formal_room' else "simple/G1O6OfficeReceptionMP-v0"
        if env_id not in gym.envs.registry:
            gym.register(env_id, entry_point="simple.envs.loco_manipulation:LocoManipulationEnv",
                         kwargs={"task": G1O6ReceptionMP.uid})
        self.env = gym.make(env_id, robot_uid="g1_o6_mp_wholebody", sim_mode="mujoco", headless=True,
                            webrtc=False, render_hz=50, dr_level=0, max_episode_steps=max_frames)
        self.obs, self.info = self.env.reset(seed=seed)
        self.task, self.engine = self.env.unwrapped.task, self.env.unwrapped.mujoco
        self.model, self.data = self.engine.mjModel, self.engine.mjData
        self.planner = CuRoboPlanner(robot=self.task.robot, plan_dt=.02, plan_batch_size=1,
                                     plan_per_traj=1, easy_motion_gen=False)
        self.agent = MotionPlannerAgent(self.task, self.planner, debug=False)
        self.terminated = self.truncated = False
        self.reward = 0.
        self.failure = None
        self.frames = dict(full_qpos=[], time=[], segment=[], phase=[], target=[])
        self.segment_log = []
        self.initial_target = np.asarray(self.info["target"][:3], dtype=float)
        mujoco.mj_saveModel(self.model, str(self.output / "execution_model.mjb"), None)
        sources = {Path(inspect.getfile(c)) for c in (G1O6MPWholebody, G1O6CanPickMP, G1O6CrossTableMP,
                                                      G1O6ReceptionMP, MotionPlannerAgent)}
        if scene == 'office_v2':
            sources.update([Path(__file__), Path(__file__).with_name('office_layout.py')])
        self.provenance = dict(seed=seed, release=RELEASE, scene=scene, room=str(room), spawn=list(SPAWN),
                               room_sha256=hashlib.sha256(Path(room).read_bytes()).hexdigest(),
                               pick_approach_overshoot_compensation_m=.025 if scene == 'office_v2' else SONIC_STOP_OVERSHOOT,
                               sim_goals={k: list(v) for k, v in SIM_GOALS.items()},
                               source_sha256={str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},
                               body_controller=self.task.robot.body_controller,
                               clock_between_commands="paused; state held in memory, never reset")
        if scene == 'office_v2':
            self.provenance.update(object_asset='graspnet1b:2',
                navigation='authored office waypoints; O6 SONIC measured-goal controller; not ROS g1pilot',
                scene_adaptations=['calibrated dynamic soup can replaces static coke decoration',
                                   'source support: 0.85 x 0.79 m, calibrated 0.75 m top and near edge',
                                   'meeting support: authored footprint and 0.75 m top, supported tabletop'])
        (self.output / "provenance.json").write_text(json.dumps(self.provenance, indent=2))

    # ---------------------------------------------------------------- measurement
    def base(self):
        mat = self.data.body("pelvis").xmat
        return float(self.data.qpos[0]), float(self.data.qpos[1]), float(np.arctan2(mat[3], mat[0]))

    def can(self):
        return np.asarray(self.info["target"][:3], dtype=float)

    def snapshot(self):
        x, y, yaw = self.base()
        task, data = self.task, self.data
        hand = data.body(task.hand_body).xpos
        gate = task._placement_gate
        # Planar speed over the last 0.5 s of recorded motion; a single-step velocity includes gait sway.
        times, qpos = self.frames["time"], self.frames["full_qpos"]
        back = next((i for i in range(len(times) - 1, -1, -1) if times[-1] - times[i] >= .5), None)
        speed = (float(np.linalg.norm(qpos[-1][:2] - qpos[back][:2]) / (times[-1] - times[back]))
                 if back is not None else float(np.linalg.norm(data.qvel[:2])))
        return dict(sim_time=float(data.time), base_xyt=[x, y, yaw], base_speed=speed,
                    tilt_deg=float(np.degrees(np.arccos(np.clip(data.body("pelvis").xmat[8], -1, 1)))),
                    can_xyz=self.can().tolist(), lift_m=float(self.can()[2] - self.initial_target[2]),
                    grasp_contact=bool(task._target_grasp_contact), supported=bool(task._target_supported),
                    destination_table_contact=bool(task._target_table_supported),
                    lifted_gate=bool(gate.lifted) if gate else False,
                    placement_accepted=bool(gate.accepted) if gate else False,
                    destination_xyz=gate.destination.tolist() if gate else None,
                    hand_can_distance_m=float(np.linalg.norm(hand - self.can())),
                    physics_failure=self.failure)

    # ---------------------------------------------------------------- execution
    def _record(self, segment, phase):
        f = self.frames
        f["full_qpos"].append(self.data.qpos.copy())
        f["time"].append(float(self.data.time))
        f["segment"].append(segment)
        f["phase"].append(phase)
        f["target"].append(self.can().tolist())
        if len(f['time']) % 100 == 0:
            (self.output / 'progress.json').write_text(json.dumps(dict(
                segment=segment, phase=phase, frames=len(f['time']), sim_time=self.data.time,
                base_xyt=list(self.base()), can_xyz=self.can().tolist())))
        tilt = np.degrees(np.arccos(np.clip(self.data.body("pelvis").xmat[8], -1, 1)))
        if tilt > 20 or self.info.get("o6_physics_failure"):
            self.failure = self.info.get("o6_physics_failure") or "base_tilt_exceeded_20_degrees"
            raise SegmentFailed(self.failure)

    def _step_queue(self, segment, phase, cancelled):
        count = 0
        while len(self.agent) and not (self.terminated or self.truncated):
            if count % 10 == 0 and cancelled():
                return True
            action = self.agent.get_action(self.obs, self.info)
            self.obs, self.reward, self.terminated, self.truncated, self.info = self.env.step(action)
            self._record(segment, phase)
            count += 1
        return False

    def _stop(self, segment):
        """Cancellation: drop queued motion and command standing until the base is still."""
        self.agent._action_queue.clear()
        yaw = self.base()[2]
        for _ in range(150):
            self.agent.queue_loco_command(command=[0, yaw, 0, 0, 0, 0, 0, 0], motion_type="stand",
                                          keep_waist_pose=True)
        self._step_queue(segment, "cancel_stand", lambda: False)

    def run_segment(self, segment, cancelled=lambda: False, progress=None):
        from service.reception_task_info import SEGMENTS
        segment_of = self.segment_of
        from simple.datagen.subtask_spec import PhaseBreakSpec, WalkSpec
        if segment not in SEGMENTS:
            raise ValueError(segment)
        if self.failure:
            raise SegmentFailed("episode_already_failed: " + self.failure)
        start_events, start_frame, wall = len(self.agent.phase_events), len(self.frames["time"]), time.time()
        before = self.snapshot()
        outcome, error = "succeeded", None
        index = self.agent._subtask_index
        walk_index = None
        try:
            while not (self.terminated or self.truncated):
                specs = self.task.decompose()
                index = self.agent._subtask_index
                upcoming = next((s for s in specs[index:] if not isinstance(s, PhaseBreakSpec)), None)
                if upcoming is None or segment_of(upcoming.phase) != segment:
                    break
                status = self.agent.synthesize()
                phase = "/".join(s.phase for s in specs[index:self.agent._subtask_index]
                                 if not isinstance(s, PhaseBreakSpec))
                if any(isinstance(s, WalkSpec) for s in specs[index:self.agent._subtask_index]):
                    walk_index = index
                if status is False:
                    raise SegmentFailed("planning_or_precondition_failed: " + phase)
                if progress and phase:
                    progress(phase)
                if self._step_queue(segment, phase, cancelled):
                    self._stop(segment)
                    outcome = "cancelled"
                    if segment.startswith("nav_"):
                        self.agent._subtask_index = index  # a paused leg can be re-issued
                    break
                if status is True:
                    break
            if self.truncated:
                raise SegmentFailed("episode_frame_budget_exhausted")
        except SegmentFailed as exc:
            outcome, error = "failed", str(exc)
        except Exception as exc:  # planner and state-completion timeouts
            outcome, error = "failed", repr(exc)
        recoverable = False
        if outcome == "failed" and segment.startswith("nav_") and not self.failure:
            # A walk/turn that missed its goal without a physical failure is stood still and
            # rewound to the start of the failed phase, so a new command can re-issue it.
            try:
                self._stop(segment)
                self.agent._subtask_index = index
                recoverable = True
            except SegmentFailed:
                pass
        after = self.snapshot()
        entry = dict(segment=segment, outcome=outcome, error=error, before=before, after=after,
                     frames=len(self.frames["time"]) - start_frame, wall_seconds=round(time.time() - wall, 1),
                     phase_events=self.agent.phase_events[start_events:],
                     episode_terminated=bool(self.terminated), reward=float(self.reward))
        entry["checks"] = self.checks(segment, after)
        if outcome == "cancelled" and segment.startswith("nav_"):
            recoverable = True
        if (outcome == "succeeded" and segment.startswith("nav_") and not entry["checks"]["reached"]
                and walk_index is not None and not self.failure):
            # Phases finished but the measured stance is outside tolerance: re-issuing the leg
            # repeats only its last walk (and what follows) from the current state.
            self.agent._subtask_index = walk_index
            recoverable = True
        if recoverable and not entry["checks"]["object_retained"]:
            recoverable = False
        if outcome == "failed" and not recoverable:
            self.failure = self.failure or error
        entry["recoverable"] = recoverable
        self.segment_log.append(entry)
        self.save()
        return entry

    def checks(self, segment, s):
        """Physical acceptance for the contract fields, computed from the measured state."""
        if segment.startswith("nav_"):
            target = {"nav_table2": "table_2", "nav_relay2": "relay2", "nav_relay3": "relay3", "nav_table1": "table_1"}[segment]
            gx, gy, gyaw = self.goals[target]
            x, y, yaw = s["base_xyt"]
            error = float(np.hypot(x - gx, y - gy))
            yaw_error = float(abs(np.arctan2(np.sin(yaw - gyaw), np.cos(yaw - gyaw))))
            reached = error <= .08 and yaw_error <= .15 and s["base_speed"] <= .08 and s["tilt_deg"] <= 10
            carrying = segment != "nav_table2"
            held = s["grasp_contact"] and not s["supported"] if carrying else True
            return dict(sim_target=target, sim_goal_xyt=[gx, gy, gyaw], position_error_m=error,
                        yaw_error_rad=yaw_error, reached=bool(reached), object_retained=bool(held),
                        stopped=s["base_speed"] <= .08)
        if segment == "pick":
            grasped = s["lifted_gate"] and s["grasp_contact"] and not s["supported"] and s["lift_m"] >= .05
            return dict(object_grasped=bool(grasped), lift_m=s["lift_m"])
        dest = np.asarray(s["destination_xyz"] or [np.nan] * 3)
        xy_error = float(np.linalg.norm(np.asarray(s["can_xyz"][:2]) - dest[:2]))
        at_target = bool(s["placement_accepted"] and s["destination_table_contact"] and not s["grasp_contact"])
        return dict(object_at_target=at_target, released=bool(not s["grasp_contact"] and s["hand_can_distance_m"] >= .16),
                    placement_xy_error_m=xy_error)

    def save(self):
        f = self.frames
        np.savez_compressed(self.output / "trajectory.npz", full_qpos=np.asarray(f["full_qpos"]),
                            time=np.asarray(f["time"]), segment=np.asarray(f["segment"]),
                            phase=np.asarray(f["phase"]), target=np.asarray(f["target"]))
        (self.output / "segments.json").write_text(json.dumps(dict(provenance=self.provenance,
            segments=self.segment_log, failure=self.failure), indent=2, default=float))

    def close(self):
        self.save()
        self.env.close()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Run all six reception segments without HTTP (physics check).")
    parser.add_argument("output")
    parser.add_argument("--seed", type=int, default=601)
    parser.add_argument("--segments", type=int, default=6, help="stop after this many segments")
    parser.add_argument('--scene', choices=['formal_room', 'office_v2'], default='formal_room')
    parser.add_argument('--office-assets')
    args = parser.parse_args()
    from service.reception_task import SEGMENTS
    episode = Episode(args.output, seed=args.seed, scene=args.scene, office_assets=args.office_assets)
    try:
        for name in SEGMENTS[:args.segments]:
            entry = episode.run_segment(name, progress=lambda phase: print('PHASE', phase, flush=True))
            print(json.dumps({k: entry[k] for k in ("segment", "outcome", "error", "frames", "wall_seconds", "checks")},
                             default=float), flush=True)
            if entry["outcome"] != "succeeded" or any(entry['checks'].get(k) is False for k in
                    ('reached', 'object_retained', 'object_grasped', 'object_at_target', 'released')):
                return 1
    finally:
        episode.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
