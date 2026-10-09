"""Semantic layout of the SIMPLE reception episode; no simulator imports (used by the HTTP facade)."""
import math

SPAWN = (-0.90, 0.90)
# Semantic contract target -> simulated base goal (x, y, yaw) in the formal room frame.
SIM_GOALS = {
    # Effective O6 grasp stance: the release run spawns at x=-0.68 and SONIC's startup
    # settle carries the base to x~-0.615 before the arm moves (multileg_carry, frames 50-150).
    "table_2": (-0.62, 0.00, 0.0),
    "relay2": (-0.70, 0.75, math.pi / 2),
    # Lateral step to the left with the facing kept; it ends on the x=-0.90 lane of the
    # verified cross_table carry, so the last leg repeats that task's 0.4 m final approach.
    "relay3": (-0.90, 0.75, math.pi / 2),
    "table_1": (-0.50, 2.00, 0.0),          # cross-table placement stance
}
# SONIC brakes once within 3 cm of a walk goal and coasts on: runs reception-1 and -5
# stopped 4.3 cm past the pick stance and 5.8 cm past the placement stance. Both final
# straight approaches aim short by this distance; arrival is still checked against the
# true stance.
SONIC_STOP_OVERSHOOT = 0.045
SEGMENTS = ("nav_table2", "pick", "nav_relay2", "nav_relay3", "nav_table1", "place")
# Contract objects and where they are in the simulated room (formal_room frame, metres).
SCENE = {
    "table_2": {"type": "table", "sim": "source table (upstream 'table', centre [0.30, 0.00])",
                "stance_xyt": list(SIM_GOALS["table_2"])},
    "door_1": {"type": "door", "sim": "relay legs relay2 -> relay3 (lateral)",
               "stance_xyt": [list(SIM_GOALS["relay2"]), list(SIM_GOALS["relay3"])]},
    "table_1": {"type": "table", "sim": "destination table (upstream 'table2', centre [0.30, 2.00])",
                "stance_xyt": list(SIM_GOALS["table_1"])},
    "cola_can_1": {"type": "can", "sim": "graspnet1b:2 soup can (calibrated O6 grasp object), start [-0.30, 0.08]"},
}
