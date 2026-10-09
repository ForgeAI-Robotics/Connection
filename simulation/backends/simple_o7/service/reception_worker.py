"""One episode worker per reception task: executes accepted commands segment by segment.

Started by the HTTP facade on the first table_2 navigation of a task. The physical
episode lives only in this process; when it ends (place done, failure, cancel,
superseded or idle timeout) the recording is saved and the process exits.
"""
import argparse
import json
import time
import traceback
from pathlib import Path

from .reception_contract import OBJECT_ID
from .reception_store import LIVE_SESSION, ReceptionStore

IDLE_TIMEOUT_SEC = 1800
NAV_TARGET = {"nav_table2": "table_2", "nav_relay2": "relay2", "nav_relay3": "relay3", "nav_table1": "table_1"}


def nav_result(entry):
    checks, after = entry["checks"], entry["after"]
    ok = entry["outcome"] == "succeeded" and checks["reached"] and checks["object_retained"]
    result = {"success": ok, "action_started": True, "reached": bool(checks["reached"] and entry["outcome"] == "succeeded"),
              # The episode clock is paused once the segment ends: no motion command is being issued.
              "navigation_stopped": True,
              # Measured pose is in the simulated room frame, not the real map; see simulation.measured_xyt.
              "final_xyt": None,
              "message": "SIMPLE 物理仿真：" + ("到达" if ok else entry["error"] or "未满足到达核验"),
              "simulation": {"frame": "simple_formal_room", "semantic_target": checks["sim_target"],
                             "goal_xyt": checks["sim_goal_xyt"], "measured_xyt": after["base_xyt"],
                             "position_error_m": checks["position_error_m"], "yaw_error_rad": checks["yaw_error_rad"],
                             "object_retained": checks["object_retained"], "sim_time_s": after["sim_time"],
                             "frames": entry["frames"], "phases": [e["phase"] for e in entry["phase_events"]],
                             "motion_mode_executed": "lateral_facing_kept" if entry["segment"] == "nav_relay3" else "forward_path"}}
    error = None
    if not ok:
        error = ("NAVIGATION_EXECUTION_FAILED", entry["error"] or
                 ("搬运中物体脱手" if not checks["object_retained"] else "未到达仿真目标位姿"))
    return ok, result, error


def vla_result(entry, operation):
    checks, after = entry["checks"], entry["after"]
    common = {"action_started": True, "object_id": OBJECT_ID, "policy_stopped": True, "navigation_port_ready": True}
    physics = {"source": "simple_o6_physics", "can_xyz": after["can_xyz"], "lift_m": after["lift_m"],
               "grasp_contact": after["grasp_contact"], "supported": after["supported"],
               "destination_table_contact": after["destination_table_contact"],
               "hand_can_distance_m": after["hand_can_distance_m"], "sim_time_s": after["sim_time"],
               "frames": entry["frames"], "phases": [e["phase"] for e in entry["phase_events"]]}
    if operation == "pick":
        ok = entry["outcome"] == "succeeded" and checks["object_grasped"]
        result = dict(common, success=ok, object_grasped=ok, holding=OBJECT_ID if ok else None, released=False,
                      evidence=dict(physics, object_presence_verified=ok, lifted_gate=after["lifted_gate"]),
                      message="SIMPLE 物理仿真：" + ("抓取并稳定持有" if ok else entry["error"] or "未形成稳定抓持"))
        return ok, result, None if ok else ("EMPTY_GRASP", entry["error"] or "未形成稳定抓持")
    ok = entry["outcome"] == "succeeded" and checks["object_at_target"] and checks["released"]
    result = dict(common, success=ok, object_grasped=not checks["released"],
                  holding=None if checks["released"] else OBJECT_ID, released=checks["released"],
                  object_at_target=checks["object_at_target"],
                  evidence=dict(physics, object_at_target_verified=checks["object_at_target"],
                                placement_xy_error_m=checks["placement_xy_error_m"],
                                placement_gate_accepted=after["placement_accepted"]),
                  message="SIMPLE 物理仿真：" + ("放置于目标桌并撤手" if ok else entry["error"] or "放置未通过核验"))
    return ok, result, None if ok else ("PLACE_FAILED", entry["error"] or "放置未通过核验")


def run(store, task, episode_dir, seed):
    from .reception_episode import Episode
    log = Path(episode_dir) / "worker.log"
    Path(episode_dir).mkdir(parents=True, exist_ok=True)
    try:
        episode = Episode(episode_dir, seed=seed)
    except Exception:
        log.write_text(traceback.format_exc())
        store.update_session(task, state="failed", failure="simulation_init_failed")
        raise
    store.update_session(task, state="ready")
    idle_since = time.time()
    try:
        while True:
            session = store.worker_session(task)
            if session["state"] not in LIVE_SESSION:
                return
            record = store.claim(task)
            if record is None:
                if time.time() - idle_since > IDLE_TIMEOUT_SEC:
                    store.update_session(task, state="ended", failure="idle_timeout")
                    return
                time.sleep(.2)
                continue
            command_id, segment = record["body"]["command_id"], record["segment"]
            store.update_session(task, state="running")
            last = [0.]

            def cancelled():
                now = time.time()
                if now - last[0] < .5:
                    return False
                last[0] = now
                return store.poll(command_id)

            entry = episode.run_segment(segment, cancelled=cancelled,
                                        progress=lambda phase: store.poll(command_id, progress=phase))
            with log.open("a") as handle:
                handle.write(json.dumps({k: entry[k] for k in ("segment", "outcome", "error", "frames",
                                                              "wall_seconds", "checks")}, default=float) + "\n")
            if entry["outcome"] == "cancelled":
                role_result = (nav_result(entry) if segment.startswith("nav_") else vla_result(entry, segment))[1]
                role_result.update(success=False, message="SIMPLE 物理仿真：已取消并站稳")
                if segment.startswith("nav_"):
                    role_result["reached"] = False
                if entry.get("recoverable"):
                    # Paused navigation: robot stood still; the same leg may be re-issued.
                    role_result.setdefault("simulation", {})["retry_allowed"] = True
                    store.finish(command_id, "cancelled", role_result, session_changes={"state": "ready"})
                    idle_since = time.time()
                    continue
                store.finish(command_id, "cancelled", role_result,
                             session_changes={"state": "cancelled", "failure": "cancelled_in_" + segment})
                return
            if segment.startswith("nav_"):
                ok, result, error = nav_result(entry)
                changes = {"last_navigation": command_id} if ok else {}
                if not ok and entry.get("recoverable"):
                    # Robot stood still, object retained: the episode stays live for a new attempt
                    # of this same leg (the brain's 继续 issues a new command_id).
                    result["simulation"]["retry_allowed"] = True
                    store.finish(command_id, "failed", result, error=error, session_changes={"state": "ready"})
                    idle_since = time.time()
                    continue
            else:
                ok, result, error = vla_result(entry, segment)
                changes = {"holding": result["holding"],
                           "object_location": ("in_gripper" if segment == "pick" else NAV_TARGET["nav_table1"])
                           if ok else "unknown"}
            if ok:
                changes.update(next=session["next"] + 1, state="completed" if segment == "place" else "ready")
            else:
                changes.update(state="failed", failure=error[1])
            store.finish(command_id, "succeeded" if ok else "failed", result, error=error, session_changes=changes)
            idle_since = time.time()
            if not ok or segment == "place":
                return
    finally:
        episode.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--episode", required=True)
    parser.add_argument("--seed", type=int, default=601)
    args = parser.parse_args()
    run(ReceptionStore(args.runtime), args.task, args.episode, args.seed)


if __name__ == "__main__":
    main()
