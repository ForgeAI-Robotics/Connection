"""
FQPlanner 任务控制台
启动后访问 http://127.0.0.1:8888

功能：任务发布、配置校验、工具查看
"""

import ast
import io
import json
import os
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import redis
import requests
import yaml
from flask import Flask, jsonify, render_template, request, send_file, send_from_directory

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from robot_api.config import load_robot_api_config

app = Flask(__name__)
# 每次请求都重新读模板:改了 index.html 不用重启 deploy 也不会拿到旧缓存页面(debug=False 默认会缓存)。
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True

MASTER_URL = os.getenv("MASTER_URL", "http://127.0.0.1:5000")
SIM_URL = os.getenv("ROBOT_API_URL", load_robot_api_config().server_url)
REDIS_CFG = {"host": "127.0.0.1", "port": 6379, "db": 0, "password": None}

# 四宫格任务时间线:capture_quad_timeline.py 把每个时间点的四宫格拼图(overhead+head+左右腕)
# 和 timeline.json 存到这里,网站按时间点回放。
TIMELINE_DIR = PROJECT_ROOT / "deploy" / "task_timeline"


def extract_tools_from_ast(source, filename):
    """从 skill.py 源码中解析工具函数"""
    tree = ast.parse(source, filename=filename)
    tools = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.decorator_list:
                continue
            for deco in node.decorator_list:
                if isinstance(deco, ast.Call) and getattr(deco.func, "attr", None) == "tool":
                    parameters = []
                    total_args = len(node.args.args)
                    defaults = [None] * (total_args - len(node.args.defaults)) + node.args.defaults
                    for arg, default in zip(node.args.args, defaults):
                        if arg.arg == "self":
                            continue
                        parameters.append({
                            "name": arg.arg,
                            "type": ast.unparse(arg.annotation) if arg.annotation else "Any",
                            "default": ast.unparse(default) if default else None,
                        })
                    tools.append({
                        "name": node.name,
                        "description": ast.get_docstring(node) or "",
                        "parameters": parameters,
                    })
    return tools


@app.route("/")
def index():
    return render_template("index.html")


_QUAD_CAM_LABELS = ["Top", "Front", "Wrist", "Agent"]
_capture_lock = threading.Lock()


def _capture_task_timeline(task_text, task_id, poll=2.0, timeout=600.0):
    """为一个任务采集四宫格时间线:开始 1 张 + 每个子任务完成的边界各 1 张 + 全部完成 1 张。

    不再实时/定时截图(实时渲染已移除)。改为盯 Master task_status 的 completed 计数,每当有一个
    子任务完成(计数+1),就在那个边界截一帧——此刻 serve 空闲,/camera/latest 现渲很快很清晰。
    这样时间线天然对齐"每步做完的样子",也不拖慢机器人执行。网页发任务时后台自动调。"""
    if not _capture_lock.acquire(blocking=False):
        return  # 已有采集在跑,跳过
    try:
        TIMELINE_DIR.mkdir(parents=True, exist_ok=True)
        for f in os.listdir(TIMELINE_DIR):
            if f.startswith("frame_") and f.endswith(".jpg"):
                os.remove(os.path.join(TIMELINE_DIR, f))
        frames, t0 = [], time.time()

        def snap(label):
            try:
                r = requests.get(f"{SIM_URL}/camera/latest", timeout=90)
                if r.status_code == 200 and r.content:
                    fn = f"frame_{len(frames):02d}.jpg"
                    with open(os.path.join(TIMELINE_DIR, fn), "wb") as fp:
                        fp.write(r.content)
                    frames.append({"file": fn, "label": label, "t": round(time.time() - t0, 1)})
            except Exception:
                pass

        def write(won=None):
            with open(os.path.join(TIMELINE_DIR, "timeline.json"), "w", encoding="utf-8") as fp:
                json.dump({"task": task_text, "won": won,
                           "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                           "cameras": _QUAD_CAM_LABELS, "frames": frames}, fp, ensure_ascii=False, indent=2)

        snap("开始"); write()
        seen_done, all_done = 0, False
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(poll)
            try:
                st = requests.get(f"{MASTER_URL}/api/task_status", timeout=10).json()
            except Exception:
                continue
            if st.get("task_id") != task_id:
                continue
            # 每完成一个子任务就在边界补一帧(serve 此刻空闲,现渲快)
            done = int(st.get("completed", 0) or 0)
            total = int(st.get("total", 0) or 0)
            subs = st.get("subtask_list") or []
            while seen_done < done:
                label = f"第{seen_done + 1}步"
                if seen_done < len(subs):
                    sub = subs[seen_done]
                    ok = (sub.get("status") == "success")
                    label = f"第{seen_done + 1}步 {'✓' if ok else '✕'} {sub.get('subtask', '')}"
                seen_done += 1
                snap(label); write()
            if st.get("all_done"):
                all_done = True
                break
        snap("完成");
        won = None
        try:
            won = requests.get(f"{SIM_URL}/success", timeout=10).json().get("won")
        except Exception:
            pass
        write(won=won)
    finally:
        _capture_lock.release()


@app.route("/publish_task", methods=["POST"])
def publish_task():
    """转发任务到 Master,并后台为该任务采集四宫格时间线(网页时间线随即变成这个任务)。"""
    try:
        data = request.get_json()
        if not data or "task" not in data:
            return jsonify({"error": "缺少 task 字段"}), 400
        task_id = data.get("task_id") or uuid.uuid4().hex
        data["task_id"] = task_id
        data.setdefault("refresh", True)
        task_text = data["task"]
        resp = requests.post(f"{MASTER_URL}/publish_task", json=data, timeout=120)
        result = resp.json()
        if (
            resp.status_code == 200
            and result.get("status") == "success"
            and result.get("accepted") is not False
        ):
            threading.Thread(target=_capture_task_timeline,
                             args=(task_text, task_id), daemon=True).start()
        return jsonify(result), resp.status_code
    except requests.exceptions.ConnectionError:
        return jsonify({"error": "Master 服务未启动"}), 503
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/validate-config", methods=["POST"])
def validate_config():
    """校验配置文件和 Redis/MCP 连通性"""
    project_root = Path(__file__).parent.parent
    data = request.json or {}
    master_cfg = data.get("master_config") or str(project_root / "master" / "config.yaml")
    slaver_cfg = data.get("slaver_config") or str(project_root / "slaver" / "config.yaml")

    for path in [master_cfg, slaver_cfg]:
        if not path or not os.path.exists(path):
            return jsonify({"success": False, "message": f"配置文件不存在: {path}"}), 400

    with open(master_cfg, "r", encoding="utf-8") as f:
        master_data = yaml.safe_load(f)
    with open(slaver_cfg, "r", encoding="utf-8") as f:
        slaver_data = yaml.safe_load(f)

    master_col = master_data.get("collaborator", {})
    slaver_col = slaver_data.get("collaborator", {})
    required_keys = {"host", "port", "password", "db"}

    if not required_keys.issubset(master_col.keys()) or not required_keys.issubset(slaver_col.keys()):
        return jsonify({"success": False, "message": "collaborator 配置字段不完整"})

    if (master_col["host"], master_col["port"]) != (slaver_col["host"], slaver_col["port"]):
        return jsonify({"success": False, "message": "Master 和 Slaver collaborator 配置不匹配"})

    try:
        r = redis.StrictRedis(
            host=master_col["host"], port=master_col["port"],
            password=master_col["password"], db=master_col["db"],
            socket_connect_timeout=5,
        )
        r.ping()
    except Exception as e:
        return jsonify({"success": False, "message": f"Redis 连接失败: {e}"})

    robot = slaver_data.get("robot", {})
    if robot.get("call_type") == "remote":
        try:
            requests.post(robot["path"].rstrip("/") + "/mcp", timeout=5)
        except requests.exceptions.RequestException:
            return jsonify({"success": False, "message": "MCP 远程服务不可达"})

    return jsonify({"success": True, "message": "配置校验通过"})


@app.route("/api/task_status", methods=["GET"])
def task_status():
    """转发 Master 的任务状态查询"""
    try:
        resp = requests.get(f"{MASTER_URL}/api/task_status", timeout=5)
        return jsonify(resp.json()), resp.status_code
    except requests.exceptions.ConnectionError:
        return jsonify({"active": False, "error": "Master 服务未启动"}), 503
    except Exception as e:
        return jsonify({"active": False, "error": str(e)}), 500


@app.route("/api/task_preflight", methods=["POST"])
def task_preflight():
    """转发 Master 的只读任务预检，不创建任务。"""
    try:
        data = request.get_json() or {}
        if not isinstance(data.get("task"), str) or not data["task"].strip():
            return jsonify({"ready": False, "error": "缺少有效 task 字段"}), 400
        resp = requests.post(
            f"{MASTER_URL}/api/task_preflight", json=data, timeout=45
        )
        return jsonify(resp.json()), resp.status_code
    except requests.exceptions.ConnectionError:
        return jsonify({"ready": False, "error": "Master 服务未启动"}), 503
    except Exception as e:
        return jsonify({"ready": False, "error": str(e)}), 500


@app.route("/api/failure_pending", methods=["GET"])
def failure_pending():
    try:
        resp = requests.get(f"{MASTER_URL}/api/failure_pending", timeout=5)
        return jsonify(resp.json()), resp.status_code
    except Exception:
        return jsonify({"pending": False}), 200


@app.route("/api/save_failure_experience", methods=["POST"])
def save_failure_experience():
    try:
        resp = requests.post(f"{MASTER_URL}/api/save_failure_experience",
                             json=request.get_json(), timeout=30)
        return jsonify(resp.json()), resp.status_code
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/success_pending", methods=["GET"])
def success_pending():
    try:
        resp = requests.get(f"{MASTER_URL}/api/success_pending", timeout=5)
        return jsonify(resp.json()), resp.status_code
    except Exception:
        return jsonify({"pending": False}), 200


@app.route("/api/save_success_experience", methods=["POST"])
def save_success_experience():
    try:
        resp = requests.post(f"{MASTER_URL}/api/save_success_experience",
                             json=request.get_json(), timeout=30)
        return jsonify(resp.json()), resp.status_code
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/save_experience", methods=["POST"])
def save_experience():
    """转发经验保存到 Master"""
    try:
        data = request.get_json()
        resp = requests.post(f"{MASTER_URL}/api/save_experience", json=data, timeout=5)
        return jsonify(resp.json()), resp.status_code
    except requests.exceptions.ConnectionError:
        return jsonify({"success": False, "message": "Master 服务未启动"}), 503
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/experiences", methods=["GET"])
def experiences():
    """转发经验库查询"""
    try:
        resp = requests.get(f"{MASTER_URL}/api/experiences", timeout=5)
        return jsonify(resp.json()), resp.status_code
    except requests.exceptions.ConnectionError:
        return jsonify({"success": True, "data": ""}), 200
    except Exception as e:
        return jsonify({"success": True, "data": ""}), 200


@app.route("/api/auto_tools", methods=["GET"])
def auto_tools():
    """从 Redis 自动读取已注册机器人的工具列表"""
    try:
        r = redis.StrictRedis(**REDIS_CFG, socket_connect_timeout=3, decode_responses=True)
        r.ping()
        agents = r.hgetall("AGENT_INFO")
        if not agents:
            return jsonify({"success": True, "data": []})
        result = []
        for name, info_str in agents.items():
            try:
                info = json.loads(info_str)
                tools = info.get("robot_tool", [])
                for t in tools:
                    func = t.get("function", {})
                    params = func.get("parameters", {})
                    props = params.get("properties", {})
                    param_list = [
                        {"name": k, "type": v.get("type", "any")}
                        for k, v in props.items()
                    ]
                    result.append({
                        "robot": name,
                        "name": func.get("name", ""),
                        "description": func.get("description", ""),
                        "parameters": param_list,
                    })
            except (json.JSONDecodeError, AttributeError):
                continue
        return jsonify({"success": True, "data": result})
    except Exception as e:
        return jsonify({"success": False, "message": f"Redis 连接失败: {e}", "data": []})


@app.route("/api/scene_state", methods=["GET"])
def scene_state():
    """读取当前场景状态"""
    try:
        r = redis.StrictRedis(**REDIS_CFG, socket_connect_timeout=3, decode_responses=True)
        r.ping()
        raw = r.hgetall("ENVIRONMENT_INFO")
        if not raw:
            return jsonify({"success": True, "data": {}})
        result = {}
        for name, val in raw.items():
            try:
                result[name] = json.loads(val)
            except (json.JSONDecodeError, TypeError):
                result[name] = val
        return jsonify({"success": True, "data": result})
    except Exception as e:
        return jsonify({"success": False, "message": f"Redis 连接失败: {e}", "data": {}})


@app.route("/api/belief", methods=["GET"])
def belief():
    """代理仿真后端的 belief 物体视角 {objects:{obj:{location}}, holding}。前端物体状态表用。"""
    try:
        resp = requests.get(f"{SIM_URL}/belief", timeout=5)
        return jsonify(resp.json()), resp.status_code
    except requests.exceptions.ConnectionError:
        return jsonify({"objects": {}, "error": "仿真服务未启动"}), 503
    except Exception as e:
        return jsonify({"objects": {}, "error": str(e)}), 500


@app.route("/api/sop", methods=["GET"])
def api_sop():
    """当前 SOP(master/sop/demo_sop.yaml)。前端最底部展示,生成新 SOP 时在此基础上 update。"""
    p = PROJECT_ROOT / "master" / "sop" / "demo_sop.yaml"
    try:
        with open(p, encoding="utf-8") as f:
            return jsonify({"success": True, "sop": yaml.safe_load(f)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ===== 人类示范学习(/teach):上传视频 → 动作拆解 + 分层提炼 → 落盘(demo 准备第4步)=====

def _sop_dir():
    return PROJECT_ROOT / "master" / "sop"


@app.route("/teach")
def teach_page():
    """人类示范学习页:上传示范视频 → 展示动作拆解 + 三层提炼 → 确认落盘。换任务只改页面上的任务描述。"""
    return render_template("teach.html")


@app.route("/api/demo/parse", methods=["POST"])
def api_demo_parse():
    """接上传视频 + 任务描述 → 后台跑 video_to_actions + learn_from_demo(dry-run) → 进度/结果写文件。"""
    import threading
    sop_dir = _sop_dir()
    f = request.files.get("video")
    if not f:
        return jsonify({"success": False, "error": "没收到视频文件"}), 400
    task = (request.form.get("task") or "会议接待补货").strip()
    up_dir = sop_dir / "uploads"
    up_dir.mkdir(exist_ok=True)
    vpath = up_dir / f"demo_{datetime.now():%Y%m%d_%H%M%S}_{f.filename}"
    f.save(str(vpath))
    status_path = sop_dir / "demo_teach_status.json"
    result_path = sop_dir / "demo_teach_result.json"
    if result_path.exists():
        result_path.unlink()

    def _st(phase, detail="", done=False, **kw):
        status_path.write_text(json.dumps(
            {"running": not done, "done": done, "phase": phase, "detail": detail, "task": task, **kw},
            ensure_ascii=False), encoding="utf-8")
    _st("已上传", f.filename)

    def _worker():
        import sys as _sys
        if str(sop_dir) not in _sys.path:
            _sys.path.insert(0, str(sop_dir))
        try:
            _st("抽帧 + VLM 解析动作", "qwen3-vl-plus 看视频…")
            import video_to_actions as v2a
            demo = v2a.video_to_actions(str(vpath), hint=task)
            _st("提炼分层", "deepseek 分 Task Specific / Global…")
            import learn_from_demo as lfd
            ts, gl = lfd.learn_from_demo(demo, write=False, context=task)
            result_path.write_text(json.dumps(
                {"video": vpath.name, "task": task,
                 "task_summary": demo.get("task_summary", ""),
                 "segments": demo.get("segments", []),
                 "task_specific": ts, "global_rules": gl},
                ensure_ascii=False), encoding="utf-8")
            _st("完成", "", done=True)
        except Exception as exc:
            _st("出错", str(exc), done=True, error=True)

    threading.Thread(target=_worker, daemon=True).start()
    return jsonify({"success": True, "started": True, "video": vpath.name, "task": task})


@app.route("/api/demo/status", methods=["GET"])
def api_demo_status():
    """解析进度(上传→抽帧+VLM→提炼→完成)。前端轮询。"""
    p = _sop_dir() / "demo_teach_status.json"
    if not p.exists():
        return jsonify({"running": False, "done": False, "phase": "空闲"})
    return jsonify(json.loads(p.read_text(encoding="utf-8")))


@app.route("/api/demo/result", methods=["GET"])
def api_demo_result():
    """解析结果:动作序列 + 分层提炼(Task Specific / Global)。前端在 status.done 后拉。"""
    p = _sop_dir() / "demo_teach_result.json"
    if not p.exists():
        return jsonify({"success": False, "error": "尚无结果"}), 404
    return jsonify({"success": True, **json.loads(p.read_text(encoding="utf-8"))})


@app.route("/api/demo/save", methods=["POST"])
def api_demo_save():
    """确认落盘:把【页面展示、你确认的那份】结果直接写盘(所见即所得,不重新提炼)。"""
    import sys as _sys
    sop_dir = _sop_dir()
    if str(sop_dir) not in _sys.path:
        _sys.path.insert(0, str(sop_dir))
    rp = sop_dir / "demo_teach_result.json"
    if not rp.exists():
        return jsonify({"success": False, "error": "没有可落盘的结果"}), 404
    r = json.loads(rp.read_text(encoding="utf-8"))
    try:
        import learn_from_demo as lfd
        demo = {"task_summary": r.get("task_summary", ""), "segments": r.get("segments", [])}
        ts, gl = r.get("task_specific", []), r.get("global_rules", [])
        lfd.persist(ts, gl, demo)      # 直接落盘解析时那份(所见即所得),不再调 LLM 重算
        return jsonify({"success": True, "task_specific": ts, "global_rules": gl})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/reception")
def reception_page():
    """G1 会议接待补货闭环展示页(复用桌面配色)。"""
    return render_template("reception.html")


@app.route("/api/reception/run", methods=["POST"])
def api_reception_run():
    """跑一次接待补货闭环(reception_loop),返回结构化 trace(含每步 verify + 反思)。"""
    import subprocess
    body = request.get_json(force=True, silent=True) or {}
    scenario = body.get("scenario", "normal")
    if scenario not in ("normal", "grasp_fail", "walk_blocked", "place_miss", "label_wrong"):
        scenario = "normal"
    headcount = int(body.get("headcount", 4))
    sop_dir = PROJECT_ROOT / "master" / "sop"
    cmd = [sys.executable, str(sop_dir / "reception_loop.py"),
           "--scenario", scenario, "--headcount", str(headcount)]
    if not body.get("reflect", True):
        cmd.append("--no-reflect")
    try:
        proc = subprocess.run(cmd, cwd=str(sop_dir), capture_output=True,
                              text=True, timeout=160)
        trace = json.loads((sop_dir / "last_reception_trace.json").read_text(encoding="utf-8"))
        return jsonify({"success": True, "trace": trace, "stdout": proc.stdout[-4000:]})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/reception/report", methods=["GET"])
def api_reception_report():
    """接待完成确认报告(reception_loop 生成的 report_card)。主控台在接待 done 后拉它展示。"""
    p = _sop_dir() / "last_reception_report.json"
    if not p.exists():
        return jsonify({"success": False, "error": "尚无报告"}), 404
    try:
        return jsonify({"success": True, "report": json.loads(p.read_text(encoding="utf-8"))})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/reception/sop", methods=["GET"])
def api_reception_sop():
    """接待 SOP(优先 v2,展示反思沉淀后的版本)。"""
    sop_dir = PROJECT_ROOT / "master" / "sop"
    v2 = sop_dir / "reception_sop_v2.yaml"
    p = v2 if v2.exists() else sop_dir / "reception_sop.yaml"
    try:
        with open(p, encoding="utf-8") as f:
            return jsonify({"success": True, "sop": yaml.safe_load(f),
                            "version": "v2" if p == v2 else "v1"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/update_scene", methods=["POST"])
def update_scene():
    """手动更新场景（外部变化）"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "message": "缺少 JSON 数据"}), 400

        location = data.get("location")
        action = data.get("action")  # add_object / remove_object
        obj = data.get("object")

        if not location or not action or not obj:
            return jsonify({"success": False, "message": "需要 location, action, object 三个字段"}), 400

        r = redis.StrictRedis(**REDIS_CFG, socket_connect_timeout=3, decode_responses=True)
        r.ping()

        raw = r.hget("ENVIRONMENT_INFO", location)
        if not raw:
            return jsonify({"success": False, "message": f"位置 '{location}' 不存在"}), 404

        scene_obj = json.loads(raw)
        contains = scene_obj.get("contains", [])

        if action == "add_object":
            if obj not in contains:
                contains.append(obj)
        elif action == "remove_object":
            if obj in contains:
                contains.remove(obj)
        else:
            return jsonify({"success": False, "message": f"不支持的 action: {action}"}), 400

        scene_obj["contains"] = contains
        r.hset("ENVIRONMENT_INFO", location, json.dumps(scene_obj, ensure_ascii=False))

        return jsonify({"success": True, "message": f"{action} '{obj}' at '{location}'", "data": scene_obj})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/get_tool_config", methods=["POST"])
def get_tool_config():
    """获取工具列表（解析 skill.py 中的 @mcp.tool() 函数）"""
    data = request.json or {}
    slaver_cfg_path = data.get("slaver_config")

    if not slaver_cfg_path or not os.path.exists(slaver_cfg_path):
        return jsonify({"success": False, "message": "配置文件不存在", "data": []}), 400

    with open(slaver_cfg_path, "r", encoding="utf-8") as f:
        slaver_data = yaml.safe_load(f)

    call_type = slaver_data.get("robot", {}).get("call_type")
    path = slaver_data.get("robot", {}).get("path")

    if call_type == "local":
        base_dir = Path(slaver_cfg_path).parent
        tool_path = base_dir / path / "skill.py"
        if not tool_path.exists():
            return jsonify({"success": False, "message": f"{tool_path} 不存在", "data": []}), 400
        with open(tool_path, "r", encoding="utf-8") as f:
            source = f.read()
        results = extract_tools_from_ast(source, str(tool_path))
        return jsonify({"success": True, "data": results})
    else:
        try:
            url = path.rstrip("/") + "/mcp"
            requests.post(url, timeout=5)
            return jsonify({"success": True, "data": []})
        except Exception as e:
            return jsonify({"success": False, "message": f"MCP 服务不可达: {e}", "data": []}), 400


@app.route("/api/robot_status", methods=["GET"])
def robot_status():
    """代理仿真后端的场景信息（机器人坐标、物体、家具）"""
    try:
        resp = requests.get(f"{SIM_URL}/scene", timeout=5)
        return jsonify(resp.json()), resp.status_code
    except requests.exceptions.ConnectionError:
        return jsonify({"error": "仿真服务未启动"}), 503
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/record/start", methods=["POST"])
def record_start():
    """代理仿真后端：开始录制"""
    try:
        resp = requests.post(f"{SIM_URL}/record/start", json=request.json or {}, timeout=5)
        return jsonify(resp.json()), resp.status_code
    except requests.exceptions.ConnectionError:
        return jsonify({"success": False, "message": "仿真服务未启动"}), 503


@app.route("/api/record/stop", methods=["POST"])
def record_stop():
    """代理仿真后端：停止录制"""
    try:
        resp = requests.post(f"{SIM_URL}/record/stop", timeout=30)
        return jsonify(resp.json()), resp.status_code
    except requests.exceptions.ConnectionError:
        return jsonify({"success": False, "message": "仿真服务未启动"}), 503


@app.route("/api/record/status", methods=["GET"])
def record_status():
    """代理仿真后端：录制状态"""
    try:
        resp = requests.get(f"{SIM_URL}/record/status", timeout=5)
        return jsonify(resp.json()), resp.status_code
    except requests.exceptions.ConnectionError:
        return jsonify({"active": False}), 503


@app.route("/api/record/download/<filename>", methods=["GET"])
def record_download(filename):
    """代理仿真后端：下载视频"""
    try:
        resp = requests.get(f"{SIM_URL}/record/download/{filename}", timeout=30, stream=True)
        if resp.status_code == 200:
            return send_file(io.BytesIO(resp.content), mimetype="video/mp4", as_attachment=True, download_name=filename)
        return jsonify({"error": "下载失败"}), resp.status_code
    except requests.exceptions.ConnectionError:
        return jsonify({"error": "仿真服务未启动"}), 503


# ============================================================
# 四宫格相机(真机3相机 head+左右腕 + overhead 拼图)
# ============================================================

@app.route("/api/quad_latest", methods=["GET"])
def quad_latest():
    """实时四宫格:代理仿真后端 /camera/latest(2x2 拼图 overhead+head+右腕+左腕,带标签)。"""
    try:
        resp = requests.get(f"{SIM_URL}/camera/latest", timeout=30, stream=True)
        if resp.status_code == 200:
            return send_file(io.BytesIO(resp.content), mimetype="image/jpeg")
        return jsonify({"error": "渲染失败"}), resp.status_code
    except requests.exceptions.ConnectionError:
        return jsonify({"error": "仿真服务未启动"}), 503
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/timeline", methods=["GET"])
def timeline_manifest():
    """返回已采集的四宫格任务时间线清单(deploy/task_timeline/timeline.json)。"""
    manifest = TIMELINE_DIR / "timeline.json"
    if not manifest.exists():
        return jsonify({"exists": False, "frames": []})
    try:
        with open(manifest, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["exists"] = True
        return jsonify(data)
    except Exception as e:
        return jsonify({"exists": False, "error": str(e), "frames": []}), 500


@app.route("/task_timeline/<path:filename>", methods=["GET"])
def timeline_frame(filename):
    """按文件名返回时间线里某一帧四宫格图。"""
    if not TIMELINE_DIR.exists():
        return jsonify({"error": "无时间线目录"}), 404
    return send_from_directory(str(TIMELINE_DIR), filename)


if __name__ == "__main__":
    print("任务控制台已启动: http://127.0.0.1:8888")
    app.run(host="0.0.0.0", port=8888, debug=False)
