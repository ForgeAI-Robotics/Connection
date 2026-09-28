"""Run one existing SIMPLE pipeline in one episode, outside the upstream tree."""
import argparse
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .server import scene
from .store import Journal

SCRIPTS = (
    "prepare_g1_coke_scene.py", "prepare_simfoundry_sonic_scene.py",
    "check_simfoundry_grasp_reach.py", "plan_simfoundry_grasp.py",
    "execute_g1_coke_sonic.py", "execute_simfoundry_sonic.py", "view_simfoundry_g1_o7.py",
)


class StopUnconfirmed(RuntimeError):
    pass


def source_fingerprint(source):
    paths = [Path(source) / "scripts" / name for name in SCRIPTS]
    paths += list((Path(source) / "src/simple").rglob("*.py"))
    paths += list((Path(source) / "data/sonic_v1_1_policy").glob("*.onnx"))
    return {str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def terminate(process):
    """Only the process group created for this episode, never an upstream service."""
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
    except ProcessLookupError:
        process.wait(timeout=10)
    except Exception as exc:
        raise StopUnconfirmed(str(exc)) from exc


def run_stage(journal, command_id, command, log, *, timeout, env=None):
    if journal.get(command_id)["cancel_requested"]:
        return "cancelled"
    with Path(log).open("ab") as output:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                   start_new_session=True, env=env)
        try:
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                if journal.get(command_id)["cancel_requested"]:
                    terminate(process)
                    return "cancelled"
                if time.monotonic() >= deadline:
                    terminate(process)
                    return "stage_timeout"
                time.sleep(.2)
            return "ok" if process.returncode == 0 else f"process_exit_{process.returncode}"
        finally:
            terminate(process)


def execute(config, command_id):
    journal = Journal(config["runtime"])
    record = journal.get(command_id)
    if record["state"] not in {"queued", "cancelling"}:
        return
    with (journal.root / "executor.lock").open("a") as lock:
        # A lock collision must not wait and later replay a command with an uncertain owner.
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            journal.update(command_id, state="unknown", error="executor_lease_unavailable")
            return
        folder = journal.root / "episodes" / record["episode_id"]
        folder.mkdir(parents=True, exist_ok=False)
        journal.update(command_id, state="running", worker_pid=os.getpid())
        started = False
        try:
            if record["scene_revision"] != scene(config["source"], config)["scene_revision"]:
                raise ValueError("scene_revision_changed_before_execution")
            if config.get("robot_variant") == "o6":
                started = True
                execute_o6(config, journal, command_id, folder)
                return
            fingerprint = source_fingerprint(config["source"])
            (folder / "source_fingerprint.json").write_text(json.dumps(fingerprint, indent=2))
            prepared, plan, output = (folder / name for name in ("prepared", "plan", "execution"))
            scripts = Path(config["source"]) / "scripts"
            python = config.get("python", sys.executable)
            stages = [
                ("prepare", [python, str(scripts / "prepare_g1_coke_scene.py"), str(prepared)]),
                ("reachability", [python, str(scripts / "check_simfoundry_grasp_reach.py"), str(prepared)]),
                ("plan", [python, str(scripts / "plan_simfoundry_grasp.py"), str(prepared), str(plan)]),
                ("execute", [python, str(scripts / "execute_g1_coke_sonic.py"), str(plan), str(output)]),
            ]
            for stage, command in stages:
                if journal.get(command_id)["cancel_requested"]:
                    journal.update(command_id, state="cancelled", stopped=True, resources_released=True)
                    return
                if stage == "execute":
                    planned = json.loads((plan / "report.json").read_text())
                    if planned.get("passed") is not True:
                        raise ValueError("motion_plan_failed")
                    if source_fingerprint(config["source"]) != fingerprint:
                        raise ValueError("source_changed_before_execution")
                    started = True
                journal.update(command_id, stage=stage, started=started)
                outcome = run_stage(journal, command_id, command, folder / (stage + ".log"),
                                    timeout=float(config.get("stage_timeout_sec", 300)))
                if outcome != "ok":
                    journal.update(command_id, state="cancelled" if outcome == "cancelled" else "failed",
                                   stopped=True, resources_released=True, error=stage + ":" + outcome)
                    return
                if stage == "reachability":
                    reach = json.loads((prepared / "reachability.json").read_text())
                    if not any(row.get("endpoint_eligible") is True for row in reach.get("candidates", [])):
                        journal.update(command_id, diagnostics={"reachability": reach})
                        raise ValueError("no_eligible_grasp_endpoint")
            result = json.loads((output / "result.json").read_text())
            telemetry = json.loads((output / "telemetry.json").read_text())
            if not telemetry or not (output / "terminal_state.npz").is_file():
                raise ValueError("physical_terminal_evidence_missing")
            if source_fingerprint(config["source"]) != fingerprint:
                raise ValueError("source_changed_during_execution")
            # Missing success evidence stays unknown; a reported physical failure is explicit.
            passed = result.get("pick_hold_passed")
            state = "succeeded" if passed is True else "failed" if passed is False else "unknown"
            journal.update(command_id, state=state, stage="ended", result=result, observation=telemetry[-1],
                           stopped=True, resources_released=True,
                           error="" if passed is True else str(result.get("stop_reason") or "physical_evidence_missing"))
        except StopUnconfirmed as exc:
            journal.update(command_id, state="unknown", stopped=False,
                           resources_released=False, error="stop_unconfirmed:" + str(exc))
        except Exception as exc:
            journal.update(command_id, state="unknown" if started else "failed", stopped=True,
                           resources_released=True, error=str(exc))


def execute_o6(config, journal, command_id, folder):
    from . import o6
    fingerprint = o6.fingerprint(config)
    (folder / "source_fingerprint.json").write_text(json.dumps(fingerprint, indent=2))
    output = folder / "execution"
    command = [config.get("python", sys.executable), str(Path(config["source"]) / "scripts/validate_g1_o6_cycle.py"),
               "--data-root", str(Path(config["source"]) / "data"), "--task", "grasp",
               "--seed", str(int(config.get("seed", 101))), "--world-tracking", "--torso-feedback",
               "--tracking-iterations", "10", "--plan-attempts", "1", "--max-frames", "1800",
               "--output", str(output)]
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(o6.overlay(config)), str(Path(config["source"]) / "src"), config["source"]])
    journal.update(command_id, stage="o6_grasp", started=True)
    outcome = run_stage(journal, command_id, command, folder / "o6_grasp.log",
                        timeout=float(config.get("stage_timeout_sec", 600)), env=env)
    if outcome == "cancelled" or outcome == "stage_timeout":
        journal.update(command_id, state="cancelled" if outcome == "cancelled" else "failed",
                       stopped=True, resources_released=True, error=outcome)
        return
    # Native validation returns exit 1 for physical/planning failure but still writes diagnostics.
    if not (output / "report.json").is_file():
        raise ValueError("o6_grasp:" + outcome + ":report_missing")
    if o6.fingerprint(config) != fingerprint:
        raise ValueError("source_changed_during_execution")
    report = json.loads((output / "report.json").read_text())
    journal.update(command_id, diagnostics={k: report.get(k) for k in ("seed", "frames", "error", "phase_events")})
    result, observation, diagnostics = o6.collect(output)
    passed = outcome == "ok" and result["pick_hold_passed"]
    journal.update(command_id, state="succeeded" if passed else "failed", stage="ended",
                   result=result, observation=observation, diagnostics=diagnostics,
                   stopped=True, resources_released=True, error="" if passed else result["stop_reason"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--command", required=True)
    args = parser.parse_args()
    execute(json.loads(Path(args.config).read_text()), args.command)
