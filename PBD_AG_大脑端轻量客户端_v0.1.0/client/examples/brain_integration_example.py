#!/usr/bin/env python3
"""Minimal integration example for the external brain."""

import os

from pbd_ag_client import PBDAGClient


SERVER = os.environ.get("PBD_AG_SERVER", "http://10.11.32.63:8088")
client = PBDAGClient(SERVER)

health = client.health()
print("PBD-AG health:", health)

client.set_task_context(
    task_id="fetch_cola_001",
    semantic_target="cola bottle",
    instruction="去茶水间取一瓶可乐",
    target_classes=["cola", "bottle", "table", "chair"],
)

snapshot = client.get_world_snapshot()
print("map session:", snapshot["map_session_id"])
print("robot pose:", snapshot["robot_pose"].get("robot_xyt"))

for class_name in ("bottle", "cola", "chair", "table"):
    print(class_name, client.get_objects(class_name))

# Navigation is deliberately not invoked in this example.  The brain should
# call client.navigate(x, y, yaw) only after checking health.navigation.mode.
