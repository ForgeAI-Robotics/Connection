"""O6 pick/carry/place in the authored office, on one continuous physics world.

Navigation follows explicit office waypoints with SONIC measured-goal control.
This is not the separate ROS demo's automatic mission client.
"""
from copy import deepcopy
import numpy as np
import transforms3d as t3d

from simple.datagen.subtask_spec import MoveEEFToPoseSpec, WalkSpec, TurnSpec, StandSpec, PhaseBreakSpec
from simple.dr.types import Box
from simple.tasks.g1_o6_can_pick_mp import G1O6CanPickMP
from simple.tasks.registry import TaskRegistry
from .office_layout import SPAWN, SIM_GOALS


def segment_of(phase):
    if phase.startswith('office_'):
        return phase.split('__', 1)[0].removeprefix('office_')
    if phase in {'stand', 'open', 'preapproach', 'approach', 'contact_grasp', 'lift_hold', 'transport_clearance'} or phase.startswith('approach_correction_'):
        return 'pick'
    return 'place'


@TaskRegistry.register('g1_o6_office_reception_mp')
class G1O6OfficeReceptionMP(G1O6CanPickMP):
    uid = 'g1_o6_office_reception_mp'
    metadata = deepcopy(G1O6CanPickMP.metadata)
    metadata['max_episode_steps'] = 45000
    dr_cfgs = deepcopy(G1O6CanPickMP.dr_cfgs)
    dr_cfgs['spatial'].robot_region = Box(low=[*SPAWN, .035], high=[*SPAWN, .035])
    # Keep the calibrated near edge at X=-.325 and tabletop Z=0; shorten only
    # the unused far side so the source support fits inside the office wall.
    dr_cfgs['scene'].table_position = Box(low=[.1, 0.], high=[.1, 0.])
    dr_cfgs['scene'].table_size = Box(low=[.85, .79, .1], high=[.85, .79, .1])
    dr_cfgs['scene'].table_height = Box(low=0., high=0.)
    dr_cfgs['scene'].enable_table2 = True
    dr_cfgs['scene'].table2_position = Box(low=[-1.20909, 8.54432], high=[-1.20909, 8.54432])
    dr_cfgs['scene'].table2_size = Box(low=[1.2, 4.01136, .1], high=[1.2, 4.01136, .1])
    dr_cfgs['scene'].table2_height = Box(low=0., high=0.)
    dr_cfgs['scene'].table2_rotation_z = Box(low=0., high=0.)
    place_offset = np.array([-.4, 7.04, 0.])
    placement_support_body = 'table2'
    placement_correction_count = 6
    tracking_correction_limit = .6
    require_upright_hold = True
    keep_ground_at_world_origin = True
    retreat_offset = [.10, 0., 0.]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.robot.locomotion_feedback_enabled = True

    def decompose(self):
        original = super().decompose()
        self.placement_rotation = t3d.euler.euler2mat(0, 0, np.pi) @ self._initial_target_rotation
        original = super().decompose()
        held = bool(self._placement_gate.lifted and self._target_grasp_contact and not self._target_supported)

        def phase(segment, name):
            return 'office_' + segment + '__' + name

        def stand(segment, name, yaw=0.):
            return StandSpec(phase(segment, name), target_yaw=yaw, steps=150)

        def turn(segment, name, yaw):
            return TurnSpec(phase(segment, name), target_yaw=yaw, steps=500, keep_waist_pose=True)

        def walk(segment, name, xy, yaw, speed=.22, lateral=False):
            return WalkSpec(phase(segment, name), vx=0. if lateral else speed, vy=speed if lateral else 0.,
                            target_yaw=yaw, target_position=xy, target_distance=8., steps=6500,
                            keep_waist_pose=True)

        prefix = [stand('nav_table2', 'initial'),
            walk('nav_table2', 'corridor', [-1.15, -.5], 0.),
            turn('nav_table2', 'align_lane', np.pi/2),
            walk('nav_table2', 'pick_lane', [-1.15, 0.], np.pi/2, .12),
            turn('nav_table2', 'face_table', 0.),
            walk('nav_table2', 'approach', [-.645, 0.], 0., .12),
            stand('nav_table2', 'settle')]
        end = next(i for i, s in enumerate(original) if s.phase == 'carry')
        place = next(i for i, s in enumerate(original) if s.phase == 'upright_hold')
        pick = original[:end] + [MoveEEFToPoseSpec('transport_clearance',
            position=self._plan_target + self.grasp_offset + [0., 0., .14],
            orientation=self.grasp_orientation, hand_uid=self.hand_uid, eef_state='close_eef',
            hold_frames=75, execution_precondition=held, allow_target_contact=True)]
        middle = [
            walk('nav_relay2', 'back_from_table', [-1., 0.], 0., -.12),
            turn('nav_relay2', 'face_west', np.pi/2),
            walk('nav_relay2', 'clear_table', [-1., 1.4], np.pi/2),
            turn('nav_relay2', 'face_north', 0.),
            walk('nav_relay2', 'door_lane', [.27727, 1.4], 0.),
            turn('nav_relay2', 'face_door', np.pi/2),
            # Measured stop/turn drift in office-probe-03 overshot Y by 8.1 cm.
            # Aim 4 cm short; the unchanged acceptance target remains Y=4.55.
            walk('nav_relay2', 'door_approach', [.27727, 4.51], np.pi/2),
            turn('nav_relay2', 'lateral_heading', 0.),
            stand('nav_relay2', 'settle'),
            walk('nav_relay3', 'door_lateral', [.27727, 6.15], 0., .12, lateral=True),
            stand('nav_relay3', 'settle'),
            turn('nav_table1', 'face_west', np.pi/2),
            walk('nav_table1', 'meeting_lane', [.27727, 7.20], np.pi/2, .18),
            turn('nav_table1', 'face_table', np.pi),
        ]
        data = self.robot.mjdata
        hand = data.body(self.hand_body)
        hand_pose = np.eye(4)
        hand_pose[:3, :3], hand_pose[:3, 3] = hand.xmat.reshape(3, 3), hand.xpos
        can = self.layout.actors['target'].pose.as_matrix()
        desired = can.copy()
        desired[:3, :3] = self.placement_rotation
        desired[:3, 3] = [data.qpos[0] - .30, data.qpos[1] - .08, .22]
        carry = desired @ np.linalg.inv(can) @ hand_pose
        middle += [MoveEEFToPoseSpec(phase('nav_table1', 'carry_clearance'), position=carry[:3, 3],
                      orientation=t3d.quaternions.mat2quat(carry[:3, :3]), hand_uid=self.hand_uid,
                      eef_state='close_eef', hold_frames=75),
                   walk('nav_table1', 'coarse', [-.20, 7.20], np.pi, .12),
                   stand('nav_table1', 'coarse_settle', np.pi),
                   walk('nav_table1', 'final', [-.39, 7.20], np.pi, .10),
                   stand('nav_table1', 'settle', np.pi)]
        for spec in middle:
            spec.meta.update(allow_target_contact=True, execution_precondition=held,
                             precondition_reason='Transport requires measured unsupported grasp')
        suffix = original[place:]
        goal = SIM_GOALS['table_1']
        yaw = np.arctan2(data.body('pelvis').xmat[3], data.body('pelvis').xmat[0])
        arrived = np.linalg.norm(data.qpos[:2] - goal[:2]) <= .08 and abs(np.arctan2(np.sin(yaw-goal[2]), np.cos(yaw-goal[2]))) <= .15
        suffix[0].meta.update(execution_precondition=bool(arrived and held),
                              precondition_reason='Placement requires measured office arrival and retained grasp')
        for spec in suffix:
            if isinstance(spec, MoveEEFToPoseSpec) and spec.phase.startswith('place'):
                spec.meta['hold_frames'] = 100
        result = []
        for spec in prefix + pick + middle + suffix:
            result.append(spec)
            if not isinstance(spec, PhaseBreakSpec):
                result.append(PhaseBreakSpec('feedback_' + spec.phase))
        return result
