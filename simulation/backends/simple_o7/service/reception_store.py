"""Reception command journal shared by the HTTP facade and one episode worker per task.

Same transaction rules as Connection's protocol simulator: original command IDs are
idempotent, a different body under the same ID conflicts, one action owns the
transport at a time, and reads never create or replay commands. Unlike the protocol
simulator, terminal results come from a physical MuJoCo episode (service.reception_episode).
"""
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .reception_contract import CONTROLLERS, OBJECT_ID, RECEIPT_VERSION
from .reception_task_info import SEGMENTS

TERMINAL = {"succeeded", "failed", "cancelled"}
NAV_SEGMENT = {1: "nav_table2", 2: "nav_relay2", 3: "nav_relay3", 4: "nav_table1"}
LIVE_SESSION = {"starting", "ready", "running"}


def stamp(value=None):
    return datetime.fromtimestamp(time.time() if value is None else value, timezone.utc).isoformat()


def segment_for(role, body):
    return NAV_SEGMENT[body["leg_index"]] if role == "nav" else body["operation"]


class Conflict(Exception):
    def __init__(self, code, message, status=409):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def _alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:  # a reaped-later child shows as a zombie; it is not running physics
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except OSError:
        return False


class ReceptionStore:
    def __init__(self, root, clock=time.time, alive=_alive):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = str(self.root / "reception.sqlite3")
        self.clock, self.alive = clock, alive
        with self.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('instance_id', ?)", (uuid.uuid4().hex,))
            db.execute("CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY, task TEXT NOT NULL, data TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS sessions (task TEXT PRIMARY KEY, data TEXT NOT NULL)")

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=20)
        try:
            db.execute("PRAGMA synchronous=FULL")
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

    # ------------------------------------------------------------------ rows
    @staticmethod
    def _commands(db, task=None):
        sql, args = "SELECT data FROM commands", ()
        if task is not None:
            sql, args = sql + " WHERE task=?", (task,)
        return [json.loads(r[0]) for r in db.execute(sql + " ORDER BY rowid", args)]

    @staticmethod
    def _save(db, record):
        db.execute("INSERT OR REPLACE INTO commands VALUES (?, ?, ?)",
                   (record["body"]["command_id"], record["body"]["task_id"], json.dumps(record, allow_nan=False)))

    @staticmethod
    def _session(db, task):
        row = db.execute("SELECT data FROM sessions WHERE task=?", (task,)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _save_session(db, session):
        db.execute("INSERT OR REPLACE INTO sessions VALUES (?, ?)", (session["task_id"], json.dumps(session)))

    def _reap(self, db):
        """A worker that exited cannot finish its command: record the loss, never a success."""
        now = self.clock()
        for row in db.execute("SELECT data FROM sessions").fetchall():
            session = json.loads(row[0])
            if session["state"] not in LIVE_SESSION or self.alive(session.get("worker_pid")):
                continue
            if session["state"] == "starting" and now - session["created"] < 30 and not session.get("worker_pid"):
                continue
            session.update(state="failed", failure=session.get("failure") or "episode_worker_exited")
            self._save_session(db, session)
            for record in self._commands(db, session["task_id"]):
                if record["state"] not in TERMINAL:
                    started = record["started"] is not None
                    self._terminal(record, "failed", now, error=("SIMULATION_WORKER_EXITED",
                                   "仿真进程已退出，原命令无法完成；物理状态未知时不报告成功"),
                                   result=self._stopped_result(record, session, started))
                    self._save(db, record)

    # ------------------------------------------------------------------ results
    @staticmethod
    def _stopped_result(record, session, started):
        result = {"success": False, "action_started": started, "message": "SIMPLE 物理仿真：未完成"}
        if record["role"] == "nav":
            result.update(reached=False, navigation_stopped=True, final_xyt=None)
        else:
            holding = session.get("holding") if session else None
            result.update(object_id=OBJECT_ID, object_grasped=holding == OBJECT_ID, holding=holding,
                          released=False, policy_stopped=True, navigation_port_ready=True)
        return result

    def _terminal(self, record, state, now, result=None, error=None):
        record.update(state=state, completed=now, result=result)
        if error:
            record["error"] = {"code": error[0], "message": error[1], "retryable": False,
                               "details": {"action_started": bool(record.get("started")),
                                           "action_state_uncertain": False}}

    # ------------------------------------------------------------------ HTTP side
    def submit(self, role, body, launch):
        """Accept or reject one validated contract request. launch(task_id, episode_dir) -> pid."""
        with self.transaction() as db:
            self._reap(db)
            records = self._commands(db)
            existing = next((r for r in records if r["body"]["command_id"] == body["command_id"]), None)
            if existing:
                if existing["role"] != role or existing["body"] != body:
                    raise Conflict("COMMAND_ID_CONFLICT", "相同 command_id 的请求体不同")
                return self.view(existing), 200
            if (self.root / "reception.closed").exists():
                raise Conflict("SERVICE_STOPPING", "仿真服务正在停止，不接受新命令", 503)
            if any(r["state"] not in TERMINAL for r in records):
                raise Conflict("NAVIGATION_BUSY" if role == "nav" else "VLA_BUSY", "动作通路已有活动命令")
            task, segment = body["task_id"], segment_for(role, body)
            session = self._session(db, task)
            now = self.clock()
            rejection = None
            if session is None and role == "nav" and segment == "nav_table2":
                for row in db.execute("SELECT data FROM sessions").fetchall():
                    other = json.loads(row[0])
                    if other["state"] in LIVE_SESSION:
                        # One physical episode at a time; an idle earlier episode is closed, not reused.
                        other.update(state="superseded", failure="superseded_by_new_task")
                        self._save_session(db, other)
                episode = self.root / "reception_episodes" / f"{task}-{uuid.uuid4().hex[:8]}"
                session = {"task_id": task, "episode": str(episode), "worker_pid": None, "state": "starting",
                           "next": 0, "holding": None, "object_location": "table_2", "last_navigation": None,
                           "receipt": None, "failure": None, "created": now, "heartbeat": now}
                self._save_session(db, session)
            if session is None:
                if role == "nav":
                    raise Conflict("ROUTE_ORDER_INVALID", "本任务还没有仿真回合；第一段必须是 table_2 导航")
                rejection = ("NAVIGATION_PROOF_INVALID", "本任务没有对应的仿真回合与导航记录")
            elif session["state"] not in LIVE_SESSION:
                if role == "nav":
                    raise Conflict("EPISODE_ENDED", f"本任务的仿真回合已结束（{session['state']}）；新尝试需要新的任务")
                rejection = ("EPISODE_ENDED", f"本任务的仿真回合已结束（{session['state']}）")
            elif SEGMENTS[session["next"]] != segment:
                if role == "nav":
                    raise Conflict("ROUTE_ORDER_INVALID", "必须按单罐接待顺序执行；当前应为 " + SEGMENTS[session["next"]])
                rejection = ("OBJECT_STATE_INVALID", "当前仿真回合不处于该操控步骤：应为 " + SEGMENTS[session["next"]])
            if role == "vla" and rejection is None:
                proof = next((r for r in records if r["role"] == "nav"
                              and r["body"]["command_id"] == body["navigation_proof"]["dream_command_id"]), None)
                if (not proof or proof["body"]["task_id"] != task or proof["state"] != "succeeded"
                        or proof["body"]["target_id"] != body["target_area"]
                        or session["last_navigation"] != proof["body"]["command_id"]):
                    rejection = ("NAVIGATION_PROOF_INVALID", "原导航命令、任务、目标或当前仿真位置不匹配")
            record = {"body": body, "role": role, "segment": segment, "state": "accepted", "accepted": now,
                      "started": None, "completed": None, "result": None, "cancel_requested": False,
                      "progress": "等待仿真进程"}
            if rejection:
                self._terminal(record, "failed", now, error=rejection,
                               result=self._stopped_result(record, session, False))
            self._save(db, record)
            if session is not None and not rejection:
                session["receipt"] = None
                self._save_session(db, session)
        if session is not None and not rejection and session["worker_pid"] is None:
            pid = launch(task, session["episode"])
            with self.transaction() as db:
                current = self._session(db, task)
                current["worker_pid"] = pid
                self._save_session(db, current)
        return self.view(record), 202

    def command(self, role, command_id, cancel_task=None):
        with self.transaction() as db:
            self._reap(db)
            record = next((r for r in self._commands(db) if r["role"] == role
                           and r["body"]["command_id"] == command_id), None)
            if not record:
                raise Conflict("COMMAND_NOT_FOUND", "未找到原命令", 404)
            if cancel_task is not None:
                if record["body"]["task_id"] != cancel_task:
                    raise Conflict("TASK_ID_MISMATCH", "取消请求不属于原任务")
                if record["state"] not in TERMINAL:
                    record["cancel_requested"] = True
                    if record["started"] is None:
                        # Not yet picked up by the episode worker: nothing moved, the episode stays
                        # live and the same step may be issued again under a new command_id.
                        session = self._session(db, cancel_task)
                        self._terminal(record, "cancelled", self.clock(),
                                       result=self._stopped_result(record, session, False))
                    self._save(db, record)
            return self.view(record)

    def status(self):
        """(active command, receipt). The receipt is a fresh observation of an idle live episode."""
        with self.transaction() as db:
            self._reap(db)
            records = self._commands(db)
            active = next((r for r in records if r["state"] not in TERMINAL), None)
            if active or not records:
                return active, None
            latest = max(records, key=lambda r: r["accepted"])
            session = self._session(db, latest["body"]["task_id"])
            receipt = (session or {}).get("receipt")
            if receipt and session["state"] in LIVE_SESSION | {"completed"}:
                return None, dict(receipt, observed_at=stamp(self.clock()))
            return None, None

    def sessions(self):
        with self.transaction() as db:
            self._reap(db)
            return [json.loads(r[0]) for r in db.execute("SELECT data FROM sessions ORDER BY rowid")]

    def close_admission(self):
        with self.transaction() as db:
            self._reap(db)
            if any(r["state"] not in TERMINAL for r in self._commands(db)):
                raise Conflict("COMMANDS_UNRESOLVED", "接待原命令尚未结束，拒绝停止仿真服务")
            (self.root / "reception.closed").touch()

    # ------------------------------------------------------------------ worker side
    def worker_session(self, task):
        with self.transaction() as db:
            return self._session(db, task)

    def update_session(self, task, **changes):
        with self.transaction() as db:
            session = self._session(db, task)
            session.update(changes)
            self._save_session(db, session)
            return session

    def claim(self, task):
        """Move the next accepted command of this task to running, or report a cancel before start."""
        with self.transaction() as db:
            session = self._session(db, task)
            session["heartbeat"] = self.clock()
            self._save_session(db, session)
            for record in self._commands(db, task):
                if record["state"] == "accepted":
                    record.update(state="navigating" if record["role"] == "nav" else "running",
                                  started=self.clock(), progress="仿真执行中")
                    self._save(db, record)
                    return record
            return None

    def poll(self, command_id, progress=None):
        """Worker heartbeat; returns True once a cancel was requested for the running command."""
        with self.transaction() as db:
            row = db.execute("SELECT data FROM commands WHERE id=?", (command_id,)).fetchone()
            record = json.loads(row[0])
            session = self._session(db, record["body"]["task_id"])
            session["heartbeat"] = self.clock()
            self._save_session(db, session)
            if progress and progress != record.get("progress"):
                record["progress"] = progress
                self._save(db, record)
            return bool(record["cancel_requested"])

    def finish(self, command_id, state, result, error=None, session_changes=None):
        with self.transaction() as db:
            row = db.execute("SELECT data FROM commands WHERE id=?", (command_id,)).fetchone()
            record = json.loads(row[0])
            if record["state"] in TERMINAL:
                return record
            self._terminal(record, state, self.clock(), result=result, error=error)
            self._save(db, record)
            session = self._session(db, record["body"]["task_id"])
            session.update(session_changes or {})
            if state == "succeeded":
                kind = ("to_vla" if record["role"] == "nav" else
                        "to_nav" if record["body"]["operation"] == "pick" else "safe_idle")
                session["receipt"] = {"contract_version": RECEIPT_VERSION, "task_id": record["body"]["task_id"],
                                      "source_command_id": command_id, "kind": kind,
                                      "controller": CONTROLLERS[kind], "active_command_id": None,
                                      "confirmed": True, "observed_at": stamp(self.clock())}
            self._save_session(db, session)
            return record

    # ------------------------------------------------------------------ views
    @staticmethod
    def view(record):
        body = record["body"]
        keys = ("target_id", "route_phase", "leg_index") if record["role"] == "nav" else ("operation",)
        value = {"contract_version": body["contract_version"], "task_id": body["task_id"],
                 "command_id": body["command_id"], **{k: body[k] for k in keys},
                 "state": record["state"], "accepted_at": stamp(record["accepted"]),
                 "started_at": stamp(record["started"]) if record["started"] else None,
                 "completed_at": stamp(record["completed"]) if record["completed"] else None,
                 "result": record["result"],
                 "progress": {"phase": record.get("progress") or record["state"],
                              "message": "SIMPLE O6 + SONIC MuJoCo 物理仿真"}}
        if record.get("error"):
            value["error"] = record["error"]
        return value
