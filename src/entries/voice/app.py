"""Recognized text in, brain publish out. No capture and no filtering."""
import os
import uuid

from flask import Flask, Response, jsonify, request

from clients.brain import BrainClient


def create_app(brain_url=None, *, client=None):
    app = Flask(__name__)
    brain = client or BrainClient(
        brain_url or os.environ.get("MASTER_URL", "http://127.0.0.1:5000"),
        timeout=90,
        source="voice",
    )

    @app.get("/health")
    def health():
        return jsonify({"ready": True, "entry": "voice"})

    @app.post("/publish_task")
    def publish_task():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            payload = {}
        task = payload.get("task")
        if not isinstance(task, str) or not task.strip():
            return jsonify({"error": "Invalid request - 'task' field required"}), 400
        body = dict(payload)
        body["task"] = task.strip()
        body.setdefault("task_id", uuid.uuid4().hex)
        body.setdefault("refresh", True)
        headers = {
            "X-FQ-Source": "voice",
            "X-FQ-Client": request.headers.get("X-FQ-Client") or request.remote_addr or "",
            "X-FQ-Via": "voice",
        }
        operator = request.headers.get("X-FQ-Operator")
        if operator:
            headers["X-FQ-Operator"] = operator
        try:
            response = brain.request("POST", "/publish_task", json=body, headers=headers)
        except Exception as exc:
            return jsonify({"success": False, "error": str(exc), "unavailable": True}), 503
        return Response(
            response.content,
            status=response.status_code,
            content_type=response.headers.get("content-type", "application/json"),
        )

    return app
