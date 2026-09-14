"""大脑侧 PBD-AG 语义地图适配 —— 轮询 PBD 世界模型,喂接待 demo(数物体/位姿/工作点导航)。

PBD 组(定位建图语义地图)在 PBD 主机跑重服务(模型/CUDA/ROS2),大脑只装纯 Python 客户端
`pbd_ag_client`,HTTP 轮询拉:占据地图 / 机器人位姿 / 语义物体(类别+中心+bbox+父子关系+稳定 id)/
RGB(按需);回传:任务上下文 / 工作点导航目标(map 系坐标)。客户端与文档见
`PBD_AG_大脑端轻量客户端_v0.1.0/`。轮询 = 大脑按频率主动 GET 拉最新(不是 PBD 推送)。

用法:
  export PBD_AG_SERVER=http://10.11.32.63:8088
  python master/sop/pbd_world.py           # 拉一次,按接待 demo 视角打印当前场景
  python master/sop/pbd_world.py --watch    # 之后持续轮询增量事件(物体增删改)
"""

import argparse
import os
import time

from pbd_ag_client import PBDAGClient

SERVER = os.environ.get("PBD_AG_SERVER", "http://10.11.32.63:8088")

# 接待 demo 关心的类别(联调时按 PBD 实际类名微调)
DRINK_CLASSES = ["bottle", "cola", "thermos"]     # 饮料:可乐/瓶/保温杯
OBSTACLE_CLASSES = ["chair"]                       # 可移动障碍:办公椅
FIXTURE_CLASSES = ["table", "fridge"]              # 桌子 / 冰箱(茶水间标志)


def _as_list(r):
    if isinstance(r, dict):
        return r.get("objects") or r.get("items") or []
    return r or []


class PBDWorld:
    """大脑侧对 PBD 世界模型的只读观测 + 任务上下文/导航下发。"""

    def __init__(self, server=SERVER):
        self.client = PBDAGClient(server)

    def health(self):
        return self.client.health()

    def objects(self, class_name):
        return _as_list(self.client.get_objects(class_name))

    def count(self, class_name):
        return len(self.objects(class_name))

    def robot_xyt(self):
        snap = self.client.get_world_snapshot()
        return (snap.get("robot_pose") or {}).get("robot_xyt")

    def set_task(self, task_id, target, instruction, classes):
        return self.client.set_task_context(
            task_id=task_id, semantic_target=target,
            instruction=instruction, target_classes=classes)

    def navigate(self, x, y, yaw):
        """发工作点坐标给 PBD;只有 navigation_mode=forward 时才真驱动,否则只记录。"""
        h = self.health()
        mode = (h.get("navigation") or {}).get("mode") or h.get("navigation_mode")
        return {"nav_mode": mode, "result": self.client.navigate(x, y, yaw)}

    def poll_events(self, after=0):
        return self.client.get_events(after=after)


def summarize(w: PBDWorld):
    """接待 demo 视角:数饮料/障碍/固定物 + 机器人位姿,打印当前场景。"""
    h = w.health()
    print(f"PBD 状态: world_ready={h.get('world_ready')} session={h.get('map_session_id')} "
          f"rev={h.get('world_revision')} nav_mode={h.get('navigation_mode')} stale={h.get('source_stale')}")
    print(f"机器人位姿(map x,y,yaw): {w.robot_xyt()}")

    def show(label, classes):
        found = []
        for c in classes:
            for o in w.objects(c):
                ctr = o.get("center") or [0, 0, 0]
                found.append(f"{o.get('class_name')}@[{ctr[0]:.2f},{ctr[1]:.2f}]")
        print(f"  {label}: {len(found)} —— {', '.join(found) or '无'}")

    show("饮料(可乐/瓶/保温杯)", DRINK_CLASSES)
    show("障碍(办公椅)", OBSTACLE_CLASSES)
    show("固定物(桌/冰箱)", FIXTURE_CLASSES)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default=SERVER)
    ap.add_argument("--watch", action="store_true", help="持续轮询增量事件")
    args = ap.parse_args()

    w = PBDWorld(args.server)
    print("=" * 62)
    print(f"大脑 ↔ PBD-AG 语义地图 · {args.server}")
    print("=" * 62)
    summarize(w)

    if args.watch:
        print("\n--- 持续轮询增量事件(1Hz,Ctrl+C 停)---")
        cursor = 0
        while True:
            for e in w.poll_events(after=cursor).get("events", []):
                cursor = max(cursor, int(e["event_id"]))
                node = e.get("node") or {}
                print(f"  事件#{e['event_id']} {e.get('event_type')} "
                      f"{e.get('node_id')} {node.get('class_name')}")
            time.sleep(1.0)


if __name__ == "__main__":
    main()
