"""Single-can reception episode on the frozen O6 SONIC cross-table task.

The brain's fq/reception-lan/v1 route (table_2 -> pick -> relay2 -> lateral
relay3 -> table_1 -> place) is mapped to semantic poses in the formal room.
Real-map coordinates are never reused; each brain command advances one
segment of the same physical episode. The upstream task code is imported
read-only; this subclass only adds the approach walk and splits the carry.
"""
from copy import deepcopy

import numpy as np

from simple.datagen.subtask_spec import PhaseBreakSpec, StandSpec, TurnSpec, WalkSpec
from simple.dr.types import Box
from simple.tasks.g1_o6_can_pick_mp import G1O6CanPickMP
from simple.tasks.g1_o6_cross_table_mp import G1O6CrossTableMP
from simple.tasks.registry import TaskRegistry

from .reception_task_info import SEGMENTS, SIM_GOALS, SONIC_STOP_OVERSHOOT, SPAWN  # noqa: F401

# Final approach to the placement stance: stop COARSE_STANDOFF short, stand, then step to the
# stance minus FINAL_SHORT. Upstream completes a walk once within 6 cm of its target, so the
# corrective step must start well outside that radius (run brain-run-03 started 3.8 cm away and
# never moved); braking then lands within about +/-3 cm of the target.
COARSE_STANDOFF = 0.19
FINAL_SHORT = 0.01

_PHASES = {
    "nav_table2": {"reception_initial_stand", "nav_turn_south", "nav_walk_to_pick_lane",
                   "nav_face_pick_table", "nav_approach_pick_table", "stand"},
    "pick": {"open", "preapproach", "approach", "approach_correction_0", "approach_correction_1",
             "contact_grasp", "lift_hold", "transport_clearance"},
    "nav_relay2": {"back_from_source", "turn_to_destination", "carry_to_relay2", "settle_at_relay2"},
    "nav_relay3": {"lateral_to_relay3", "settle_at_relay3"},
    "nav_table1": {"carry_to_table1_lane", "face_destination", "destination_carry_clearance",
                   "approach_destination_coarse", "settle_before_final_step", "approach_destination",
                   "settle_at_destination"},
}


def segment_of(phase):
    for name, phases in _PHASES.items():
        if phase in phases:
            return name
    # Placement, its corrections, release and retreat.
    return "place"


def _with_breaks(specs):
    result = []
    for spec in specs:
        result.extend([spec, PhaseBreakSpec("feedback_after_" + spec.phase)])
    return result


@TaskRegistry.register("g1_o6_reception_mp")
class G1O6ReceptionMP(G1O6CrossTableMP):
    uid = "g1_o6_reception_mp"
    label = "G1 O6 Single-can Reception"
    description = "Walk to the source table, pick the can, carry it through two relay legs and place it on the second table."
    metadata = deepcopy(G1O6CrossTableMP.metadata)
    metadata["max_episode_steps"] = 12000
    dr_cfgs = deepcopy(G1O6CrossTableMP.dr_cfgs)
    dr_cfgs["spatial"].robot_region = Box(low=[SPAWN[0], SPAWN[1], .035], high=[SPAWN[0], SPAWN[1], .035])
    dr_cfgs["language"].instructions = [description]

    def decompose(self):
        specs = super().decompose()
        prefix = [StandSpec("reception_initial_stand", steps=150)] + _with_breaks([
            TurnSpec("nav_turn_south", target_yaw=-np.pi / 2, steps=250, keep_waist_pose=True),
            WalkSpec("nav_walk_to_pick_lane", vx=.25, target_yaw=-np.pi / 2, target_distance=SPAWN[1],
                     target_position=[SPAWN[0], 0.], steps=900, keep_waist_pose=True),
            TurnSpec("nav_face_pick_table", target_yaw=0., steps=250, keep_waist_pose=True),
            WalkSpec("nav_approach_pick_table", vx=.12, target_yaw=0., target_distance=SIM_GOALS["table_2"][0] - SPAWN[0],
                     target_position=[SIM_GOALS["table_2"][0] - SONIC_STOP_OVERSHOOT, SIM_GOALS["table_2"][1]],
                     steps=400, keep_waist_pose=True),
        ])
        index = next(i for i, s in enumerate(specs) if s.phase == "carry_between_tables")
        carry = specs[index]
        legs = []
        for phase, goal, vx in (("carry_to_relay2", SIM_GOALS["relay2"], .25),
                                ("lateral_to_relay3", SIM_GOALS["relay3"], 0.),
                                ("carry_to_table1_lane", (SIM_GOALS["relay3"][0], SIM_GOALS["table_1"][1]), .25)):
            leg = WalkSpec(phase, vx=vx, vy=.15 if phase.startswith("lateral") else 0., target_yaw=np.pi / 2,
                           target_distance=1., target_position=list(goal[:2]), steps=1000, keep_waist_pose=True)
            # The carry legs inherit the measured-grasp precondition of the upstream transport.
            for key in ("allow_target_contact", "execution_precondition", "precondition_reason"):
                leg.meta[key] = carry.meta[key]
            legs.append(leg)
        # Each relay leg ends standing still, as a contract navigation leg does; the
        # upstream break after carry_between_tables stays after the last leg.
        # StandSpec commands its own heading (default 0); keep the relay heading while settling.
        settle = [StandSpec("settle_at_relay2", target_yaw=SIM_GOALS["relay2"][2], steps=150),
                  StandSpec("settle_at_relay3", target_yaw=SIM_GOALS["relay3"][2], steps=150)]
        for spec in settle:
            spec.meta["allow_target_contact"] = True
        middle = [legs[0], PhaseBreakSpec("feedback_after_carry_to_relay2"), settle[0],
                  PhaseBreakSpec("feedback_after_settle_at_relay2"),
                  legs[1], PhaseBreakSpec("feedback_after_lateral_to_relay3"), settle[1],
                  PhaseBreakSpec("feedback_after_settle_at_relay3"), legs[2]]
        result = prefix + specs[:index] + middle + specs[index + 1:]
        # The upstream 0.4 m final approach drifted 8.6 cm sideways in run brain-run-02 and timed
        # out, and in run reception-5 coasted 5.8 cm past the stance (no room left for the retreat).
        # Walk most of it, stand still, then take a short corrective step whose direction is
        # recomputed from the measured base.
        at = next(i for i, s in enumerate(result) if s.phase == "approach_destination")
        final = result[at]
        x, y = final.meta["target_position"]
        coarse = deepcopy(final)
        coarse.phase = "approach_destination_coarse"
        coarse.meta["target_position"] = [x - FINAL_SHORT - COARSE_STANDOFF, y]
        stop = StandSpec("settle_before_final_step", target_yaw=final.meta["target_yaw"], steps=150,
                         allow_target_contact=True)
        final.meta["target_position"] = [x - FINAL_SHORT, y]
        result[at:at] = [coarse, PhaseBreakSpec("feedback_after_approach_destination_coarse"), stop,
                         PhaseBreakSpec("feedback_after_settle_before_final_step")]
        return result

    def check_success(self, info, *args, **kwargs):
        # The upstream 1.8 m cross-table travel threshold does not apply to this shorter route;
        # success is the native placement gate on the destination table.
        passed = G1O6CanPickMP.check_success(self, info, *args, **kwargs)
        info["o6_reception"] = dict(destination_table_contact=self._target_table_supported)
        return bool(passed and self._target_table_supported)
