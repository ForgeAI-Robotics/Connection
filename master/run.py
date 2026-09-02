import json
import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import psutil
from agents.agent import GlobalAgent
from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_socketio import SocketIO

# The documented entry point is ``python master/run.py`` from the repository
# root, while the master configuration contains paths relative to this module.
# Normalize the working directory so config.yaml, scene/profile.yaml, and log
# paths resolve consistently regardless of the caller's current directory.
os.chdir(os.path.dirname(os.path.abspath(__file__)))

app = Flask(__name__, static_folder="assets")
CORS(app, resources={r"/*": {"origins": "*"}})
socketio = SocketIO(app, cors_allowed_origins="*")


master_agent = GlobalAgent(config_path="config.yaml")

_TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _validated_task_id(value):
    if value is None:
        return None
    if not isinstance(value, str) or not _TASK_ID_PATTERN.fullmatch(value):
        raise ValueError(
            "task_id 必须是 1-128 位 ASCII 标识符，"
            "仅允许字母、数字、点、下划线、冒号和连字符"
        )
    return value


def send_text_to_forntend(text):
    socketio.emit("text_update", {"data": text}, namespace="/")


@app.route("/system_status", methods=["GET"])
def system_status():
    """
    Get the system status.

    Returns:
        JSON response with system status
    """
    cpu_load = psutil.cpu_percent(interval=1)

    memory = psutil.virtual_memory()
    memory_usage = memory.percent

    return jsonify(
        {
            "cpu_load": round(cpu_load, 1),
            "memory_usage": round(memory_usage, 1),
        }
    )


@app.route("/robot_status", methods=["GET"])
def robot_status():
    """
    Get the status of all robots.

    Returns:
        JSON response with robot status
    """
    try:
        registered_robots = master_agent.collaborator.read_all_agents_info()
        registered_robots_status = []
        for robot_name, robot_info in registered_robots.items():
            registered_robots_status.append(
                {
                    "robot_name": robot_name,
                    "robot_state": json.loads(robot_info).get("robot_state"),
                }
            )
        return jsonify(registered_robots_status), 200
    except Exception as e:
        return jsonify({"error": "Internal server error", "details": str(e)}), 500


@app.route("/api/task_status", methods=["GET"])
def task_status():
    """返回当前任务的执行进度。"""
    return jsonify(master_agent.get_task_status()), 200


@app.route("/api/task_preflight", methods=["POST"])
def task_preflight():
    """提交前只读预检；不创建任务、不发送 DREAM/VLA 命令。"""
    try:
        data = request.get_json() or {}
        task = data.get("task")
        if not isinstance(task, str) or not task.strip():
            return jsonify({"ready": False, "error": "缺少有效 task 字段"}), 400
        return jsonify(master_agent.get_task_preflight(task.strip())), 200
    except Exception as exc:
        return jsonify({
            "ready": False,
            "required": True,
            "blockers": [str(exc)],
            "error_type": type(exc).__name__,
        }), 200


@app.route("/api/exploration_rate", methods=["GET"])
def exploration_rate():
    """当前 exploration_rate;benchmark preflight 用它确认经验没被高探索率忽略。"""
    return jsonify({"exploration_rate": master_agent._exploration_rate}), 200


@app.route("/api/success_pending", methods=["GET"])
def success_pending():
    """返回是否有等待人工决定的成功经验。"""
    info = master_agent._pending_success
    if info:
        return jsonify({"pending": True, **info}), 200
    return jsonify({"pending": False}), 200


@app.route("/api/save_success_experience", methods=["POST"])
def save_success_experience():
    """接收人工输入的成功经验（可选），LLM 归类后写入 skill 文件。"""
    try:
        data = request.get_json() or {}
        raw_input = (data.get("note") or "").strip()
        if not raw_input:
            master_agent._pending_success = None
            return jsonify({"success": True, "message": "已跳过"}), 200
        result = master_agent.classify_and_save_success_experience(raw_input)
        return jsonify(result), 200
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/failure_pending", methods=["GET"])
def failure_pending():
    """返回是否有等待人工录入的失败经验。"""
    info = master_agent._pending_failure
    if info:
        return jsonify({"pending": True, **info}), 200
    return jsonify({"pending": False}), 200


@app.route("/api/save_failure_experience", methods=["POST"])
def save_failure_experience():
    """接收人工录入的失败经验，LLM 自动归类后写入对应 skill 文件。"""
    try:
        data = request.get_json() or {}
        raw_input = (data.get("note") or "").strip()
        if not raw_input:
            master_agent._pending_failure = None
            return jsonify({"success": True, "message": "已跳过"}), 200
        result = master_agent.classify_and_save_failure_experience(raw_input)
        return jsonify(result), 200
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/save_experience", methods=["POST"])
def save_experience():
    """兼容旧前端的经验保存接口。"""
    try:
        data = request.get_json() or {}
        task_id = data.get("task_id", "")
        exp_type = data.get("type")
        note = data.get("note", "")

        if exp_type not in ("positive", "negative"):
            return jsonify({"success": False, "message": "type 必须是 positive 或 negative"}), 400

        result = master_agent.save_experience(task_id, exp_type, note)
        return jsonify(result), 200
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/experiences", methods=["GET"])
def experiences():
    """查看经验库全文。"""
    return jsonify({"success": True, "data": master_agent.get_experiences()}), 200


@app.route("/publish_task", methods=["POST", "GET"])
def publish_task():
    """
    Publish a task to the Redis channel.

    Request JSON format:
    {
        "task": "task_content"  # The task to be published
        "refresh": "true" # Boolean value, default is true, indicating whether to refresh the cached robot memory
    }

    Returns:
        JSON response with status or error message
    """
    if request.method == "GET":
        return jsonify({"statis": "success"}), 200
    try:
        data = request.get_json()
        if not data or "task" not in data:
            return jsonify({"error": "Invalid request - 'task' field required"}), 400
        tasks = data["task"] if isinstance(data["task"], list) else [data["task"]]
        if len(tasks) != 1:
            return jsonify({"error": "每次只能发布一个顶层任务"}), 400
        if "refresh" not in data:
            data["refresh"] = False

        task_id = _validated_task_id(data.get("task_id"))
        for task in tasks:
            if not isinstance(task, str):
                return jsonify({"error": "Invalid task format - must be a string"}), 400
            subtask_list = master_agent.publish_global_task(
                task, data["refresh"], task_id
            )

        accepted = not (
            isinstance(subtask_list, dict) and subtask_list.get("ignored") is True
        )
        actual_task_id = master_agent.current_task_id

        return (
            jsonify(
                {
                    "status": "success",
                    "accepted": accepted,
                    "task_id": actual_task_id,
                    "requested_task_id": task_id,
                    "message": (
                        "Task published successfully"
                        if accepted else "Task was not accepted"
                    ),
                    "data": subtask_list,
                }
            ),
            200,
        )

    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": "Internal server error", "details": str(e)}), 500


if __name__ == "__main__":
    # Run the Flask app
    app.run(host="0.0.0.0", port=5000)
