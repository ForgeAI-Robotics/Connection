"""Unified post-task ledger. Sim and real share one episode schema."""

from __future__ import annotations


SCHEMA = "fq/reflection-episode/v1"


def stringify(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        message = value.get("message") or value.get("detail") or value.get("result")
        if message:
            return stringify(message)
        return str(value)
    return str(value)


def episode_from_steps(
    task_id,
    task,
    steps,
    *,
    task_type="generic",
    backend="unknown",
    final="success",
    error=None,
):
    return {
        "schema": SCHEMA,
        "task_id": task_id or "",
        "task": task or "",
        "task_type": task_type or "generic",
        "backend": backend or "unknown",
        "final": final or "success",
        "error": error or "",
        "steps": list(steps or []),
    }


def episode_from_task_queue(
    task_id,
    task,
    queue,
    *,
    task_type="generic",
    backend="sim",
    final="success",
    error=None,
):
    steps = []
    tasks = getattr(queue, "tasks", None) or []
    for item in tasks:
        if not item.get("done"):
            continue
        status = str(item.get("status") or "success")
        ok = status == "success"
        steps.append({
            "order": item.get("order"),
            "phase": item.get("subtask") or "",
            "detail": stringify(item.get("result")),
            "status": status,
            "claimed_ok": ok,
            "verify_ok": None,
            "verify_detail": stringify(item.get("result")),
        })
    return episode_from_steps(
        task_id, task, steps,
        task_type=task_type, backend=backend, final=final, error=error,
    )


def episode_from_reception_trace(trace, *, backend="mock"):
    steps = []
    order = 0
    for rnd in (trace or {}).get("rounds") or []:
        for att in rnd.get("attempts") or []:
            for st in att.get("steps") or []:
                order += 1
                exec_r = st.get("exec") or {}
                verify_ok = st.get("verify_ok")
                steps.append({
                    "order": order,
                    "phase": st.get("step") or "",
                    "detail": st.get("verify_detail") or "",
                    "status": "success" if verify_ok else "failure",
                    "claimed_ok": bool(exec_r.get("success")),
                    "verify_ok": verify_ok,
                    "verify_detail": st.get("verify_detail") or "",
                })
    report = (trace or {}).get("report_card") or {}
    for check in report.get("checks") or []:
        order += 1
        passed = bool(check.get("pass"))
        steps.append({
            "order": order,
            "phase": check.get("name") or "终局复核",
            "detail": check.get("detail") or "",
            "status": "success" if passed else "failure",
            "claimed_ok": True,
            "verify_ok": passed,
            "verify_detail": check.get("detail") or "",
        })
    fc = (trace or {}).get("final_check") or {}
    if fc.get("verdict") is True:
        final = "success"
    elif fc.get("aborted"):
        final = "failure"
    else:
        final = "success" if not steps else (
            "failure" if any(s.get("verify_ok") is False for s in steps) else "success"
        )
        if report.get("verdict") is False:
            final = "failure"
        elif report.get("verdict") is True:
            final = "success"
    return episode_from_steps(
        (trace or {}).get("task_id") or "",
        (trace or {}).get("task") or (report.get("task") if isinstance(report, dict) else "") or "开始接待",
        steps,
        task_type="reception",
        backend=backend,
        final=final,
    )
