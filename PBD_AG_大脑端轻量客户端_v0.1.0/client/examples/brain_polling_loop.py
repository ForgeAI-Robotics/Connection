#!/usr/bin/env python3
"""Example: poll stable world-model changes without ROS or Redis coupling."""

import os
import time

from pbd_ag_client import PBDAGClient


client = PBDAGClient(os.environ.get("PBD_AG_SERVER", "http://10.11.32.63:8088"))
cursor = 0

while True:
    response = client.get_events(after=cursor)
    for event in response["events"]:
        cursor = max(cursor, int(event["event_id"]))
        print(
            event["event_id"],
            event["event_type"],
            event["node_id"],
            event["node"].get("class_name"),
        )
    time.sleep(1.0)
