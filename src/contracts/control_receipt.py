"""Optional status extension shared by physical and simulated downstreams.

An idle transport alone is not a controller receipt. Older services return no receipt.
"""
from datetime import datetime, timezone

VERSION = "fq/control-receipt/v1"
CONTROLLERS = {"to_nav": "navigation", "to_vla": "manipulation", "safe_idle": "safe_idle"}


def valid_receipt(receipt, context, *, completed_at, now=None):
    if not isinstance(receipt, dict):
        return False
    expected = {"contract_version": VERSION, "task_id": context["task_id"],
                "source_command_id": context["source_command_id"], "kind": context["kind"],
                "controller": CONTROLLERS.get(context["kind"]), "active_command_id": None}
    if expected["controller"] is None or any(k not in receipt or receipt[k] != v for k, v in expected.items()):
        return False
    if receipt.get("confirmed") is not True:
        return False
    try:
        observed = datetime.fromisoformat(str(receipt["observed_at"]).replace("Z", "+00:00"))
        completed = datetime.fromisoformat(str(completed_at).replace("Z", "+00:00"))
        if observed.tzinfo is None or completed.tzinfo is None:
            return False
        age = ((now or datetime.now(timezone.utc)) - observed).total_seconds()
        return -2 <= age <= 10 and observed >= completed
    except (ValueError, TypeError, KeyError):
        return False
