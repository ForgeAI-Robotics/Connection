"""Brain HTTP application factory. No web templates or import-time services."""
import json
import re
import psutil
from flask import Flask, jsonify, request
from flask_cors import CORS
from shared.log_setup import note_task_request
from shared.brain_journal import emit as journal_emit, inbound_from_flask
from contracts.intent import Intent, classify_task
from brain.api.facade import HttpFacade
_TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def create_app(application, *, facade=None):
    app = Flask(__name__)
    CORS(app, resources={r"/*": {"origins": "*"}})
    master_agent = facade if facade is not None else HttpFacade(application)
    app.extensions['brain'] = application

    def _validated_task_id(value):
        if value is None:
            return None
        if not isinstance(value, str) or not _TASK_ID_PATTERN.fullmatch(value):
            raise ValueError(
                "task_id 必须是 1-128 位 ASCII 标识符，"
                "仅允许字母、数字、点、下划线、冒号和连字符"
            )
        return value

    def _inbound():
        return inbound_from_flask(request)

    @app.get("/api/execution_profile")
    def execution_profile_status():
        from shared.execution_profile import applied_profile
        profile = applied_profile()
        return jsonify(profile or {"revision": None, "mode": "legacy_config"})

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

    @app.route("/api/task_status", methods=["GET"])
    def task_status():
        """返回当前任务的执行进度。"""
        return jsonify(master_agent.get_task_status()), 200

    @app.route("/api/task_pause", methods=["POST"])
    def task_pause():
        """暂停当前 Runtime 任务。"""
        return jsonify(master_agent.kernel_pause()), 200

    @app.route("/api/task_continue", methods=["POST"])
    def task_continue():
        """从暂停或人工等待继续。开关关闭时不写旧账本。"""
        return jsonify(master_agent.kernel_continue()), 200

    @app.route("/api/task_cancel", methods=["POST"])
    def task_cancel():
        """请求取消当前 Runtime 任务。受理不等于已经取消完成。"""
        return jsonify(master_agent.kernel_cancel()), 200

    @app.route("/api/task_preflight", methods=["POST"])
    def task_preflight():
        """提交前只读预检；不创建任务、不发送 DREAM/VLA 命令。"""
        data = {}
        try:
            data = request.get_json() or {}
            task = data.get("task")
            if not isinstance(task, str) or not task.strip():
                return jsonify({"ready": False, "error": "缺少有效 task 字段"}), 400
            task = task.strip()
            report = master_agent.get_task_preflight(task)
            journal_emit(
                "INBOUND",
                event="preflight",
                text=task,
                inherit_task=False,
                **_inbound(),
            )
            journal_emit(
                "PREFLIGHT",
                event="http",
                text=task,
                ready=report.get("ready"),
                required=report.get("required"),
                blockers=report.get("blockers"),
                inherit_task=False,
            )
            note_task_request(
                "preflight",
                task,
                ready=report.get("ready"),
                required=report.get("required"),
                blockers=report.get("blockers"),
            )
            return jsonify(report), 200
        except Exception as exc:
            note_task_request("preflight", data.get("task") if isinstance(data, dict) else "", error=str(exc))
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
        Publish a task through the configured brain scheduler.

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
                task = task.strip()
                entry = classify_task(task)
                resume = bool(data.get("resume"))
                from brain.config_flags import scheduler_runtime
                if resume and not scheduler_runtime(master_agent.config) and not master_agent._is_reception_task(task):
                    return jsonify(
                        {
                            "status": "rejected",
                            "accepted": False,
                            "error": "断点继续只支持开始接待",
                            "task": task,
                        }
                    ), 200
                if entry.intent is Intent.CHAT:
                    journal_emit(
                        "INBOUND",
                        event="chat",
                        text=task,
                        intent="chat",
                        risk=entry.risk.value,
                        inherit_task=False,
                        **_inbound(),
                    )
                    journal_emit(
                        "REJECTED",
                        event="chat",
                        text=task,
                        error="闲聊不会发给大脑",
                        ok=False,
                        inherit_task=False,
                    )
                    note_task_request("intent", task, intent="chat", risk=entry.risk.value)
                    return jsonify(
                        {
                            "status": "rejected",
                            "accepted": False,
                            "intent": "chat",
                            "error": "闲聊不会发给大脑",
                            "task": task,
                        }
                    ), 200
                journal_emit(
                    "INBOUND",
                    event="publish",
                    text=task,
                    intent=entry.intent.value,
                    risk=entry.risk.value,
                    force_new=bool(data.get("force_new_task")),
                    resume=resume,
                    id=task_id,
                    inherit_task=False,
                    **_inbound(),
                )
                if not resume:
                    report = master_agent.get_task_preflight(task)
                    journal_emit(
                        "PREFLIGHT",
                        event="http",
                        text=task,
                        ready=report.get("ready"),
                        required=report.get("required"),
                        blockers=report.get("blockers"),
                        id=task_id,
                        inherit_task=False,
                    )
                    note_task_request(
                        "preflight",
                        task,
                        ready=report.get("ready"),
                        required=report.get("required"),
                        blockers=report.get("blockers"),
                    )
                    if report.get("required") and not report.get("ready"):
                        blockers = report.get("blockers") or []
                        journal_emit(
                            "REJECTED",
                            event="preflight",
                            text=task,
                            blockers=blockers,
                            error="；".join(str(item) for item in blockers) or "下游服务未就绪",
                            ok=False,
                            id=task_id,
                            inherit_task=False,
                        )
                        return jsonify(
                            {
                                "status": "rejected",
                                "accepted": False,
                                "ready": False,
                                "required": True,
                                "blockers": blockers,
                                "error": "；".join(str(item) for item in blockers)
                                or "下游服务未就绪",
                                "task": task,
                            }
                        ), 200
                note_task_request("publish", task, task_id=task_id, resume=resume)
                subtask_list = master_agent.publish_global_task(
                    task, data["refresh"], task_id,
                    force_new_task=bool(data.get("force_new_task")),
                    resume=resume, options={"entry": request.headers.get("X-FQ-Source") or "api"},
                )

            accepted = not (
                isinstance(subtask_list, dict) and subtask_list.get("ignored") is True
            )
            actual_task_id = (subtask_list.get("task_id") if isinstance(subtask_list, dict) else None) or master_agent.current_task_id
            error = (
                subtask_list.get("error") or subtask_list.get("reasoning_explanation")
                if isinstance(subtask_list, dict) else None
            )

            return (
                jsonify(
                    {
                        "status": "success",
                        "accepted": accepted,
                        "task_id": actual_task_id,
                        "requested_task_id": task_id,
                        "message": (
                            "Task published successfully"
                            if accepted
                            else (error or "Task was not accepted")
                        ),
                        "error": None if accepted else error,
                        "blocks_new_motion": bool(
                            isinstance(subtask_list, dict)
                            and subtask_list.get("blocks_new_motion")
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

    @app.route("/api/reception/run", methods=["POST"])
    def reception_demo():
        """Same admission, status and controls as every other task; mock execution only."""
        from brain.config_flags import scheduler_runtime
        import uuid
        if not scheduler_runtime(master_agent.config):
            return jsonify({"success": False, "error": "接待演示需要统一 Runtime"}), 409
        body = request.get_json(silent=True) or {}
        options = {"mock": True, "headcount": body.get("headcount", 4),
                   "scenario": body.get("scenario", "normal"), "reflect": body.get("reflect", True)}
        task_id = "task-" + uuid.uuid4().hex
        result = master_agent.publish_global_task("开始接待", False, task_id, options=options)
        accepted = result.get("ignored") is not True
        return jsonify({"success": accepted, "accepted": accepted, "task_id": task_id,
                        "error": result.get("error"), "status": master_agent.get_task_status()}), 200

    @app.route("/api/reception/report", methods=["GET"])
    def reception_report():
        status = master_agent.get_task_status()
        if status.get("task_type") != "reception":
            return jsonify({"success": False, "error": "当前没有接待任务"}), 404
        return jsonify({"success": True, "report": {
            "task_id": status.get("task_id"), "verdict": status.get("state") == "succeeded",
            "state": status.get("state"), "checks": [
                {"name": item["subtask"], "pass": item["status"] == "success", "detail": item["result"]}
                for item in status.get("subtask_list", [])],
            "advice": status.get("blocked_reason") or "", "spoken": status.get("state"),
        }})

    @app.get('/health')
    def health():
        return jsonify(application.health())

    from brain.api.views import register_views
    register_views(app, application)
    return app
