"""The existing reception mock world, with Runtime owning every action and verification."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from connection.brain.adapters.execution import SimAdapter
from connection.contracts.steps import StepSpec


def _world():
    # A private instance avoids other CLI demos resetting the active task's world.
    spec = importlib.util.spec_from_file_location("kernel_reception_world", Path(__file__).with_name("reception_world.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def plan(port, options):
    headcount = int(options.get("headcount", 4))
    if not 1 <= headcount <= 50:
        raise ValueError("参会人数必须在 1 到 50 之间")
    scenario = options.get("scenario", "normal")
    if scenario not in {"normal", "grasp_fail", "walk_blocked", "place_miss", "label_wrong"}:
        raise ValueError("未知接待演示场景")
    port.world.reset_world(headcount=headcount, tea_cola=max(10, headcount))
    if scenario != "normal":
        port.world.inject_fault(scenario)
    actions = [("light", "会议室", 0)]
    for i in range(max(0, headcount - 1)):
        actions.extend([("walk", "茶水间", 0), ("pick", "茶水间", 0),
                        ("walk", "会议室", 0), ("place", "会议室", i + 2)])
    actions.append(("check", "会议室", headcount))
    return [StepSpec(f"MOCK_{i}", "mock", prefix=f"mock-{i}", key=json.dumps(action, ensure_ascii=False),
                     evidence="mock_world", deadline_sec=30)
            for i, action in enumerate(actions, 1)]


class MockAdapter(SimAdapter):
    def __init__(self):
        super().__init__()
        self.world = _world()

    def _execute_view(self, command_id, request):
        kind, room, count = json.loads(request["body"]["subtask"])
        w = self.world
        if kind == "light":
            claimed = w.w_set_light(room)
            ok = w.w_light_state(room) == "on"
        elif kind == "walk":
            claimed = w.w_walk(room)
            ok = w.w_robot_at() == room
        elif kind == "pick":
            claimed = w.w_pick_cola(room)
            ok = w.w_robot_holding() == "可乐"
        elif kind == "place":
            claimed = w.w_place_cola(room)
            slot = w.w_last_slot(room) or {}
            ok = (w.w_robot_holding() is None and w.w_count_cola(room) >= count
                  and slot.get("spaced") is True and slot.get("label_aligned") is True)
        elif kind == "check":
            claimed = True
            ok = (w.w_robot_holding() is None and w.w_count_cola(room) >= count
                  and all(s["label_aligned"] and s["spaced"] for s in w.w_all_slots(room)))
        else:
            raise ValueError("未知模拟接待动作")
        return {"command_id": command_id, "terminal": "succeeded" if ok else "failed",
                "started": True, "stopped": True, "resources_released": True,
                "evidence": {"supports": ok, "contradicts": not ok, "identity_ok": True,
                             "time_ok": True, "identity": command_id, "grade": "object",
                             "claimed_ok": claimed, "detail": f"{kind} {room}: {ok}",
                             "world": json.loads(json.dumps(w.WORLD))}}
