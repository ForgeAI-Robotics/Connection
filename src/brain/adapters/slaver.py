"""Optional Slaver execution transport. It never owns business task state."""
import json
import threading


class SlaverTransport:
    def __init__(self, config):
        from execution.collaboration import Collaborator
        settings = dict(config.get("collaborator") or {})
        settings["clear"] = False
        self.collaborator = Collaborator.from_config(settings)
        self._result_lock = threading.RLock()
        self._inflight_by_robot = {}
        self._stop = threading.Event()
        self._thread = None
        self._subscriptions = {}
        self._start_lock = threading.Lock()

    def start(self):
        with self._start_lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._listen, name="brain-slaver", daemon=True)
                self._thread.start()

    def _listen(self):
        conn = self.collaborator._get_conn()
        pubsub = conn.pubsub()
        subscribed = set()
        try:
            while not self._stop.is_set():
                names = set(self.collaborator.read_all_agents_name() or [])
                for name in names - subscribed:
                    pubsub.subscribe(f"{name}_to_FQPlanner")
                    subscribed.add(name)
                if not subscribed:
                    self._stop.wait(.5)
                    continue
                message = pubsub.get_message(timeout=.5, ignore_subscribe_messages=False)
                if message:
                    if message["type"] == "subscribe":
                        name = str(message["channel"]).removesuffix("_to_FQPlanner")
                        with self._result_lock:
                            self._subscriptions.setdefault(name, threading.Event()).set()
                    elif message["type"] == "message":
                        self._handle_result(message["data"])
        finally:
            pubsub.close()
            conn.close()

    def _begin_inflight(self, robot_name, task_id):
        # The adapter registers before send. Ensure subscription also precedes send.
        self.start()
        with self._result_lock:
            ready = self._subscriptions.setdefault(robot_name, threading.Event())
        if not ready.wait(5):
            raise RuntimeError("Slaver 结果订阅未就绪；没有派发命令")
        slot = {"task_id": str(task_id), "got_result": False, "status": None, "result": None}
        with self._result_lock:
            self._inflight_by_robot[robot_name] = slot
        return slot

    def _consume_inflight(self, robot_name, slot):
        with self._result_lock:
            if self._inflight_by_robot.get(robot_name) is slot:
                self._inflight_by_robot.pop(robot_name)
            return dict(slot)

    def _handle_result(self, payload):
        try:
            value = json.loads(payload)
        except (TypeError, ValueError):
            return
        name = value.get("robot_name")
        with self._result_lock:
            slot = self._inflight_by_robot.get(name)
            if not slot or str(value.get("task_id")) != slot["task_id"]:
                return
            if not value.get("subtask_handle") or not value.get("subtask_result"):
                return
            slot.update(status=value.get("status"), result=value["subtask_result"], got_result=True)
        self.collaborator.update_agent_busy(name, False)

    def close(self, timeout=2):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)
        self.collaborator.pool.disconnect()
