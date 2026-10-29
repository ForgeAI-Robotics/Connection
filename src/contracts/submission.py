"""Recognize pre-dispatch validation and admission refusals."""

from copy import deepcopy


VALIDATION_REFUSALS = frozenset({
    "INVALID_REQUEST", "UNSUPPORTED_CONTRACT_VERSION", "INVALID_FRAME_ID",
    "INVALID_GOAL_XYT", "INVALID_ROUTE_PHASE", "INVALID_LEG_ORDER",
    "INVALID_MOTION_MODE", "NAVIGATION_PROOF_INVALID",
    "MOTOR_HEALTH_NOT_READY", "SAFETY_NOT_READY", "VLA_ACTION_PORT_OWNED",
    "NAVIGATION_STILL_ACTIVE", "NAVIGATION_STATUS_UNVERIFIABLE",
    "STOP_NOT_CONFIRMED",
})


def submission_refusal(status_code, payload, *, command_id, task_id):
    # Admission checks reject before creation. A same-ID conflict, a bare 404
    # or a transport/server error does not prove that this identity never ran.
    if status_code not in {400, 403, 409, 422, 501, 503} or not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if payload.get("success") is not False or not isinstance(error, dict):
        return None
    details = error.get("details")
    details = details if isinstance(details, dict) else {}
    unavailable = (error.get('code') == 'CAPABILITY_UNAVAILABLE'
                   and details.get('action_started') is False
                   and details.get('action_state_uncertain') is False)
    disabled = (status_code == 503 and error.get('code') == 'REAL_ACTIONS_DISABLED'
                and details.get('action_started') is False
                and details.get('action_state_uncertain') is False)
    if status_code == 503 and not disabled:
        return None
    if status_code == 501 and not unavailable:
        return None
    busy_other_command = (error.get("code") in {"NAVIGATION_BUSY", "VLA_BUSY"}
                          and isinstance(details.get("active_command_id"), str)
                          and bool(details["active_command_id"])
                          and details["active_command_id"] != command_id)
    if error.get("code") not in VALIDATION_REFUSALS and not busy_other_command and not unavailable and not disabled:
        return None
    for item in (payload, details):
        if any(item.get(key) not in (None, "", value)
               for key, value in (("command_id", command_id), ("task_id", task_id))):
            return None
        if item.get("accepted") is True or item.get("started") is True or item.get("state"):
            return None
    return {"status_code": status_code, "code": error["code"],
            "message": str(error.get("message") or error["code"]),
            "command_id": command_id, "task_id": task_id, "raw": deepcopy(payload)}


def refusal_observation(receipt):
    """The refused attempt acquired no resources; this says nothing about other motion."""
    return {
        "command_id": receipt["command_id"], "terminal": "failed", "timed_out": False,
        "started": False, "stopped": True, "resources_released": True,
        "error": receipt["message"], "raw": receipt["raw"],
        "evidence": {"submission_rejected": deepcopy(receipt), "supports": False,
                     "contradicts": True, "identity": receipt["command_id"],
                     "identity_ok": True, "time_ok": True},
    }
