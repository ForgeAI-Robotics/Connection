"""serve_dream 离线自测 — 不依赖对方在线服务,验证三个适配器 + HTTP 端点。

  python serve_dream/selftest.py

覆盖:
  1. rosmap: 合成一张小 trinary PGM(与真实 map.yaml 同 origin/resolution)→ /map_data 结构
  2. scene_graph: 样例场景图 → /objects /fixtures(坐标/父子关系断言)
  3. nav_handoff: 写任务 → 模拟导航侧写回 SUCCEEDED → 轮询映射 {success:true}
  4. flask test_client 全端点走一遍(含 /nav 的失败与成功路径、501 端点)
"""

import json
import math
import os
import shutil
import sys
import tempfile
import threading
import time

_THIS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS)

import yaml

from adapters import nav_handoff, rosmap, scene_graph

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PASS = 0


def check(name, cond, detail=""):
    global PASS
    status = "✓" if cond else "✗"
    print(f"  {status} {name}" + (f"  {detail}" if detail else ""))
    if cond:
        PASS += 1
    else:
        raise SystemExit(f"selftest 失败于: {name} {detail}")


def make_pgm(path, width=40, height=30):
    """合成 trinary 图:四周障碍(0)、右上角未知(205)、其余可通行(254)。"""
    data = bytearray([254]) * 0
    data = bytearray()
    for y in range(height):
        for x in range(width):
            if x < 2 or y < 2 or x >= width - 2 or y >= height - 2:
                data.append(0)
            elif x > width - 8 and y < 8:
                data.append(205)
            else:
                data.append(254)
    with open(path, "wb") as f:
        f.write(b"P5\n%d %d\n255\n" % (width, height))
        f.write(bytes(data))


def main():
    tmp = tempfile.mkdtemp(prefix="serve_dream_selftest_")
    try:
        # -- 布置交换目录:map.yaml(真实参数) + 合成 pgm + 样例场景图/位姿 --
        shutil.copy(os.path.join(_THIS, "sample", "map.yaml"), tmp)
        shutil.copy(os.path.join(_THIS, "sample", "total_scene_graph_latest.json"), tmp)
        shutil.copy(os.path.join(_THIS, "sample", "nav_map_latest.json"), tmp)
        make_pgm(os.path.join(tmp, "map.pgm"))

        print("[1] rosmap 适配")
        md = rosmap.load_map_data(os.path.join(tmp, "map.yaml"))
        check("resolution=0.025", md["resolution"] == 0.025)
        check("origin=(-3.1,-5.1)", abs(md["origin"][0] + 3.1) < 1e-6 and abs(md["origin"][1] + 5.1) < 1e-6)
        check("grid 尺寸一致", len(md["grid"]) == md["width"] * md["height"], f"{md['width']}x{md['height']}")
        check("trinary 值透传", {0, 205, 254} == set(md["grid"]))

        print("[2] scene_graph 适配")
        g = scene_graph.load_graph(os.path.join(tmp, "total_scene_graph_latest.json"))
        fx = scene_graph.graph_fixtures(g)
        ob = scene_graph.graph_objects(g)
        check("fixtures= table_1+fridge_1", set(fx) == {"table_1", "fridge_1"})
        check("table_1 pos", abs(fx["table_1"]["pos"][0] - 0.695676) < 1e-6)
        check("fridge_1 size=bbox 全尺寸", abs(fx["fridge_1"]["size"][0] - (5.076632 - 3.927049)) < 1e-4)
        check("关键 objects 存在", {"cola_can_1", "bottled_water_1", "mobile_phone_1"}.issubset(ob))
        check("cola_can_1 pos+parent", abs(ob["cola_can_1"]["pos"][0] - 0.801077) < 1e-4
              and ob["cola_can_1"]["parent"] == "table_1")
        check("grasped 恒 False", not any(o["grasped"] for o in ob.values()))

        print("[3] nav_handoff 文件契约")
        task = nav_handoff.build_task(0.95, -1.70, yaw_deg=-134.565)
        q = task["nav2_navigate_to_pose"]["goal"]["pose"]["pose"]["orientation"]
        check("yaw deg→rad→quat", abs(q["z"] - (-0.92242)) < 1e-3 and abs(q["w"] - 0.38619) < 1e-3)
        task_dir = nav_handoff.write_task(tmp, task)
        check("任务目录+latest 清单", os.path.isfile(os.path.join(task_dir, "navigation_task.yaml"))
              and os.path.isfile(os.path.join(tmp, "tasks", "latest_task.yaml")))

        def nav_side_writes_result():
            time.sleep(0.5)
            with open(os.path.join(task_dir, "navigation_result.yaml"), "w") as f:
                yaml.safe_dump({
                    "schema_version": "g1_navigation_result/v1",
                    "task_id": task["task"]["task_id"], "revision": 1,
                    "status": "SUCCEEDED",
                    "final_pose_xyt_rad": [0.96, -1.69, -2.35],
                    "message": "",
                }, f)

        threading.Thread(target=nav_side_writes_result, daemon=True).start()
        res = nav_handoff.poll_result(task_dir, timeout_sec=10, interval_sec=0.2)
        resp = nav_handoff.result_to_response(res)
        check("SUCCEEDED→success", resp["success"] is True, resp["result"])
        check("ABORTED→failure", nav_handoff.result_to_response({"status": "ABORTED"})["success"] is False)

        print("[4] HTTP 通道(模拟对方:静态文件服务 + nav2_goal_bridge 的 /nav)")
        import functools
        from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer

        static_srv = ThreadingHTTPServer(
            ("127.0.0.1", 0), functools.partial(SimpleHTTPRequestHandler, directory=tmp))
        threading.Thread(target=static_srv.serve_forever, daemon=True).start()

        class FakeNavBridge(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0) or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                ok = body.get("x") is not None and body.get("y") is not None
                data = json.dumps({"success": ok,
                                   "result": "Nav2 导航完成(SUCCEEDED)" if ok else "缺 x/y",
                                   "echo": body}).encode()
                self.send_response(200 if ok else 500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)

        nav_srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeNavBridge)
        threading.Thread(target=nav_srv.serve_forever, daemon=True).start()

        from adapters import exchange
        http_cfg = {"exchange": {
            "mode": "http",
            "base_url": f"http://127.0.0.1:{static_srv.server_port}",
            "nav_url": f"http://127.0.0.1:{nav_srv.server_port}",
            "map_yaml": "map.yaml",
            "scene_graph": "total_scene_graph_latest.json",
            "pose_file": "nav_map_latest.json",
        }}
        g2, src = exchange.load_json_file(http_cfg, _THIS, "scene_graph", "total_scene_graph_latest.json")
        check("HTTP 拉场景图", g2 is not None and "table_1" in g2["nodes"])
        pose2, _ = exchange.load_json_file(http_cfg, _THIS, "pose_file", "nav_map_latest.json")
        check("HTTP 拉位姿", abs(pose2["robot_xyt"][0] - 1.64645) < 1e-4)
        md2 = rosmap.load_map_data(exchange.map_yaml_path(http_cfg, _THIS))
        check("HTTP 拉地图(yaml+pgm→缓存→解析)", md2["width"] == 40)
        resp, code = exchange.nav_forward(http_cfg, 0.95, -1.70, -134.6, timeout=5)
        check("HTTP /nav 转发(度数透传+同步返回)", code == 200 and resp["success"]
              and abs(resp["echo"]["target_yaw"] + 134.6) < 1e-6)
        resp2, _c2 = exchange.nav_forward({"exchange": {"nav_url": "http://127.0.0.1:1"}}, 1, 1, timeout=1)
        check("导航服务不可达→报错不崩", resp2["success"] is False)
        static_srv.shutdown()
        nav_srv.shutdown()

        print("[5] HTTP 端点(flask test_client,交换目录指向临时目录)")
        try:
            import flask  # noqa: F401
        except ImportError:
            print("  - 跳过:当前 Python 环境无 flask(适配器 [1]-[3] 已全绿;"
                  "在跑 serve 的环境里执行本脚本可补测 HTTP 层)")
            print(f"\n通过 {PASS} 项 ✓(HTTP 层未测)")
            return
        cfg_path = os.path.join(_THIS, "config.yaml")
        with open(cfg_path, encoding="utf-8") as f:
            cfg_backup = f.read()
        cfg = yaml.safe_load(cfg_backup)
        cfg["exchange"]["dir"] = tmp
        cfg["nav"]["result_timeout_sec"] = 5
        cfg["nav"]["result_poll_interval_sec"] = 0.2
        with open(cfg_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
        try:
            from service.server import app
            c = app.test_client()
            check("GET /health", c.get("/health").get_json()["scene_graph"]["found"])
            check("GET /objects", "cola_can_1" in c.get("/objects").get_json())
            check("GET /fixtures", "table_1" in c.get("/fixtures").get_json())
            bs = c.get("/base_status").get_json()
            check("GET /base_status(位姿+yaw_deg)", abs(bs["pos"][0] - 1.64645) < 1e-4
                  and abs(bs["yaw_deg"] - (math.degrees(-2.054726) % 360)) < 0.1)
            check("GET /map_data", c.get("/map_data").get_json()["width"] == 40)
            check("POST /nav 缺参→400", c.post("/nav", json={}).status_code == 400)
            check("POST /grasp→501(VLA 职责)", c.post("/grasp", json={"obj_name": "x"}).status_code == 501)

            blocked = c.post(
                "/nav", json={"x": 0.95, "y": -1.70, "target_yaw": -134.6})
            check("POST /nav 默认安全门→403", blocked.status_code == 403
                  and blocked.get_json()["real_control_authorized"] is False)
        finally:
            with open(cfg_path, "w", encoding="utf-8") as f:
                f.write(cfg_backup)

        print(f"\n全部通过: {PASS} 项 ✓")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
