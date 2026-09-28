"""Persistent logical scene and command journal shared by two HTTP processes.

Only timed state transitions are simulated. No robot SDK, sockets to a robot, or
physical sensor claims exist here. Transactions serialize NAV/VLA ownership.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time
import uuid

from contracts.control_receipt import VERSION, CONTROLLERS
from contracts.tasks import CONTRACT_VERSION, OBJECT_ID

SCENARIOS = {"success", "nav_failure", "pick_failure", "place_failure", "hand_state_only",
             "missing_receipt", "waiting_navigation"}
TERMINAL = {"succeeded", "failed", "cancelled"}


def stamp(value=None):
    return datetime.fromtimestamp(time.time() if value is None else value, timezone.utc).isoformat()


class Conflict(Exception):
    def __init__(self, code, message, status=409):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


class Store:
    def __init__(self, path, clock=time.time):
        self.path, self.clock = str(path), clock
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('instance_id', ?)", (uuid.uuid4().hex,))
            db.execute("CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY, role TEXT NOT NULL, data TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS scenes (task TEXT PRIMARY KEY, data TEXT NOT NULL)")

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @property
    def instance_id(self):
        with self.transaction() as db:
            return db.execute("SELECT value FROM metadata WHERE key='instance_id'").fetchone()[0]

    @staticmethod
    def _commands(db):
        return [json.loads(row[0]) for row in db.execute("SELECT data FROM commands ORDER BY rowid")]

    @staticmethod
    def _save(db, record):
        db.execute("INSERT OR REPLACE INTO commands VALUES (?, ?, ?)",
                   (record["body"]["command_id"], record["role"], json.dumps(record, allow_nan=False)))

    @staticmethod
    def _scene(db, task):
        row = db.execute("SELECT data FROM scenes WHERE task=?", (task,)).fetchone()
        return json.loads(row[0]) if row else {"holding": None, "object_location": "table_2", "leg": 0,
                                             "last_navigation": None, "receipt": None}

    @staticmethod
    def _save_scene(db, task, scene):
        db.execute("INSERT OR REPLACE INTO scenes VALUES (?, ?)", (task, json.dumps(scene)))

    def _tick(self, db):
        now = self.clock()
        for record in self._commands(db):
            if record["state"] in TERMINAL:
                continue
            elapsed = max(0, now - record["accepted"])
            if record["scenario"] == "waiting_navigation" and record["role"] == "nav":
                record["state"] = "waiting_operator_approval"
            elif elapsed >= record["duration"]:
                self._finish(db, record, now)
            elif elapsed >= record["duration"] * .1:
                states = (["planning", "arming", "navigating"] if record["role"] == "nav" else
                          ["verifying_navigation", "waiting_navigation_release", "acquiring_action_port",
                           "running", "evaluating_action_result", "stopping_policy", "restoring_navigation"])
                record["state"] = states[min(len(states) - 1, int(elapsed / record["duration"] * len(states)))]
                record["started"] = record.get("started") or record["accepted"] + record["duration"] * .1
            self._save(db, record)

    def _finish(self, db, record, now, cancelled=False):
        body, role = record["body"], record["role"]
        task = body["task_id"]
        scene = self._scene(db, task)
        operation = "nav" if role == "nav" else body["operation"]
        failed = record["scenario"] == operation + "_failure"
        success = not cancelled and not failed
        record["state"] = "cancelled" if cancelled else "succeeded" if success else "failed"
        record["completed"] = now
        if not cancelled:
            record["started"] = record.get("started") or record["accepted"] + record["duration"] * .1
        result = {"success": success, "action_started": record.get("started") is not None,
                  "message": "协议模拟：" + record["state"]}
        if role == "nav":
            result.update(reached=success, navigation_stopped=True,
                          final_xyt=body["goal_xyt"] if success else None)
            if success:
                scene.update(leg=body["leg_index"], last_navigation=body["command_id"])
        else:
            if success:
                scene.update(holding=OBJECT_ID if operation == "pick" else None,
                             object_location="in_gripper" if operation == "pick" else body["target_area"])
            result.update(object_id=OBJECT_ID, object_grasped=scene["holding"] == OBJECT_ID,
                          holding=scene["holding"], released=success and operation == "place",
                          policy_stopped=True, navigation_port_ready=True,
                          evidence={"source": "protocol_simulator", "object_presence_verified": success})
            if operation == "place":
                result["object_at_target"] = success
                result["evidence"]["object_at_target_verified"] = success
            if record["scenario"] == "hand_state_only":
                result.update(evidence_level="hand_state_only", evidence={"source": "protocol_simulator",
                    "hand": "left", "confirmed_frames": 3, "hand_closed_confirmed": operation == "pick",
                    "hand_open_confirmed": operation == "place", "object_presence_verified": False,
                    "object_at_target_verified": False})
                if operation == "place":
                    result["object_at_target"] = None
        record["result"] = result
        scene["receipt"] = None
        if success and record["scenario"] != "missing_receipt":
            kind = "to_vla" if role == "nav" else "to_nav" if operation == "pick" else "safe_idle"
            scene["receipt"] = {"contract_version": VERSION, "task_id": task,
                "source_command_id": body["command_id"], "kind": kind, "controller": CONTROLLERS[kind],
                "active_command_id": None, "confirmed": True, "observed_at": stamp(now)}
        if failed:
            record["error"] = {"code": "NAVIGATION_EXECUTION_FAILED" if role == "nav" else
                               "EMPTY_GRASP" if operation == "pick" else "PLACE_FAILED",
                               "message": "配置的模拟失败", "retryable": False, "details": {}}
        self._save_scene(db, task, scene)

    def submit(self, role, body, duration, scenario, rejection=None):
        with self.transaction() as db:
            self._tick(db)
            records = self._commands(db)
            existing = next((r for r in records if r["body"]["command_id"] == body["command_id"]), None)
            if existing:
                if existing["role"] != role or existing["body"] != body:
                    raise Conflict("COMMAND_ID_CONFLICT", "相同 command_id 的请求体不同")
                return self.view(existing), 200
            if any(r["state"] not in TERMINAL for r in records):
                raise Conflict("NAVIGATION_BUSY" if role == "nav" else "VLA_BUSY", "动作通路已有活动命令")
            for task, raw in db.execute("SELECT task, data FROM scenes"):
                if task != body["task_id"] and json.loads(raw)["holding"] is not None:
                    raise Conflict("OBJECT_HELD", "其它任务仍持有物体，不能重置场景")
            scene = self._scene(db, body["task_id"])
            if role == "nav":
                if body["leg_index"] != scene["leg"] + 1:
                    raise Conflict("ROUTE_ORDER_INVALID", "必须使用同一任务按既定导航段顺序执行")
                if body["leg_index"] > 1 and scene["holding"] != OBJECT_ID:
                    raise Conflict("OBJECT_NOT_HELD", "搬运前需要已抓取物体")
            else:
                proof = next((r for r in records if r["role"] == "nav" and
                              r["body"]["command_id"] == body["navigation_proof"]["dream_command_id"]), None)
                if (not proof or proof["body"]["task_id"] != body["task_id"] or proof["state"] != "succeeded"
                        or proof["body"]["target_id"] != body["target_area"]
                        or scene["last_navigation"] != proof["body"]["command_id"]):
                    rejection = ("NAVIGATION_PROOF_INVALID", "原导航命令、任务、目标或当前导航位置不匹配")
                expected = None if body["operation"] == "pick" else OBJECT_ID
                if scene["holding"] != expected or (body["operation"] == "pick" and scene["object_location"] != "table_2"):
                    rejection = rejection or ("OBJECT_STATE_INVALID", "持物状态不允许本次操控")
            now = self.clock()
            record = {"body": body, "role": role, "state": "accepted", "accepted": now,
                      "started": None, "completed": None, "result": None,
                      "duration": duration, "scenario": scenario}
            if rejection:
                # Well-formed VLA transactions fail preflight without starting a policy.
                record.update(state="failed", completed=now,
                    error={"code": rejection[0], "message": rejection[1], "retryable": False,
                           "details": {"action_started": False, "action_state_uncertain": False}},
                    result={"success": False, "action_started": False, "object_id": OBJECT_ID,
                            "object_grasped": scene["holding"] == OBJECT_ID, "holding": scene["holding"],
                            "released": False, "policy_stopped": True, "navigation_port_ready": True})
            scene["receipt"] = None
            self._save_scene(db, body["task_id"], scene)
            self._save(db, record)
            return self.view(record), 202

    def command(self, role, command_id, cancel_task=None):
        with self.transaction() as db:
            self._tick(db)
            record = next((r for r in self._commands(db) if r["role"] == role and
                           r["body"]["command_id"] == command_id), None)
            if not record:
                raise Conflict("COMMAND_NOT_FOUND", "未找到原命令", 404)
            if cancel_task is not None:
                if record["body"]["task_id"] != cancel_task:
                    raise Conflict("TASK_ID_MISMATCH", "取消请求不属于原任务")
                if record["state"] not in TERMINAL:
                    self._finish(db, record, self.clock(), cancelled=True)
                    self._save(db, record)
            return self.view(record)

    def _restored_receipt(self, db, latest):
        """Cancellation/failure preserves the logical scene; re-observe its prior owner.

        This is a controller observation only, not success for the cancelled action.
        A real downstream must provide its own equivalent control receipt.
        """
        if latest['scenario'] == 'missing_receipt':
            return None
        task = latest['body']['task_id']
        scene = self._scene(db, task)
        previous = [r for r in self._commands(db) if r['body']['task_id'] == task
                    and r['state'] == 'succeeded' and r['accepted'] < latest['accepted']]
        if not previous:
            return None
        source = max(previous, key=lambda r: r['accepted'])
        if source['scenario'] == 'missing_receipt':
            return None
        body = source['body']
        if source['role'] == 'nav':
            if scene['last_navigation'] != body['command_id'] or scene['leg'] != body['leg_index']:
                return None
            kind = 'to_vla'
        elif body['operation'] == 'pick':
            if scene['holding'] != OBJECT_ID or scene['object_location'] != 'in_gripper':
                return None
            kind = 'to_nav'
        else:
            if scene['holding'] is not None or scene['object_location'] != body['target_area']:
                return None
            kind = 'safe_idle'
        return {'contract_version': VERSION, 'task_id': task,
                'source_command_id': body['command_id'], 'kind': kind,
                'controller': CONTROLLERS[kind], 'active_command_id': None,
                'confirmed': True, 'observed_at': stamp(self.clock())}

    def status(self):
        with self.transaction() as db:
            self._tick(db)
            records = self._commands(db)
            active = next((r for r in records if r["state"] not in TERMINAL), None)
            latest = max(records, key=lambda r: r["accepted"], default=None)
            receipt = self._scene(db, latest["body"]["task_id"])["receipt"] if latest and not active else None
            if not active and latest and latest['state'] in {'cancelled', 'failed'} and not receipt:
                receipt = self._restored_receipt(db, latest)
            if receipt:
                # Fresh observation of the persisted logical controller, not a renewed command.
                receipt["observed_at"] = stamp(self.clock())
            return active, receipt

    @staticmethod
    def view(record):
        body = record["body"]
        keys = ("target_id", "route_phase", "leg_index") if record["role"] == "nav" else ("operation",)
        value = {"contract_version": CONTRACT_VERSION, "task_id": body["task_id"],
                 "command_id": body["command_id"], **{k: body[k] for k in keys},
                 "state": record["state"], "accepted_at": stamp(record["accepted"]),
                 "started_at": stamp(record["started"]) if record["started"] else None,
                 "completed_at": stamp(record["completed"]) if record["completed"] else None,
                 "result": record["result"], "progress": {"phase": record["state"], "message": "协议模拟"}}
        if record.get("error"):
            value["error"] = record["error"]
        return value
