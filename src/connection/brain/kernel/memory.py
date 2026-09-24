"""Short-term context and observation provenance.

The window is a read view. It does not delete the task ledger, open commands,
or the append-only event file.
"""

from __future__ import annotations

from datetime import datetime


DEFAULT_EVENT_LIMIT = 20
DEFAULT_EVENT_TTL_SEC = 30 * 60


def event_window_settings(config) -> tuple[int, float]:
    real = {}
    if isinstance(config, dict):
        real = config.get("reception_real") or {}
    return (
        _positive_int(real.get("event_memory_limit"), DEFAULT_EVENT_LIMIT),
        _positive_float(real.get("event_memory_ttl_sec"), DEFAULT_EVENT_TTL_SEC),
    )


def select_window(events, *, limit: int, ttl_sec: float, now=None) -> list:
    """Return the newest events still inside the window. The input list is unchanged."""
    moment = _require_now(now)
    fresh = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        stamp = _parse_time(event.get("time"))
        if stamp is None:
            continue
        if (moment - stamp).total_seconds() <= float(ttl_sec):
            fresh.append(event)
    keep = int(limit)
    if keep < 1:
        return []
    return fresh[-keep:]


def append_observation(
    record: dict,
    *,
    subject: str,
    value,
    source: str,
    observed_at,
    kind: str,
    valid_until=None,
) -> dict:
    if kind not in {"established", "observed"}:
        raise ValueError("观测种类只能是 established 或 observed")
    if not subject or not source:
        raise ValueError("观测必须带主题和来源")
    observed = _parse_time(observed_at)
    if observed is None:
        raise ValueError("观测时间必须带时区")
    until_text = None
    if kind == "established":
        if valid_until:
            raise ValueError("已确认进展不设有效期")
    else:
        until = _parse_time(valid_until)
        if until is None:
            raise ValueError("现场观测必须带有效期")
        until_text = until.isoformat(timespec="milliseconds")
    row = {
        "subject": subject,
        "value": value,
        "source": source,
        "observed_at": observed.isoformat(timespec="milliseconds"),
        "valid_until": until_text,
        "kind": kind,
    }
    record.setdefault("observations", []).append(row)
    return row


def read_subject(observations, subject: str, *, now=None) -> dict:
    """Confirmed progress stays. Disagreements stay beside it. Expired rows are clues."""
    moment = _require_now(now)
    rows = [
        row for row in (observations or [])
        if isinstance(row, dict) and row.get("subject") == subject
    ]
    established = [row for row in rows if row.get("kind") == "established"]
    latest = established[-1] if established else None
    fresh = []
    clues = []
    for row in rows:
        if row.get("kind") == "established":
            continue
        until = _parse_time(row.get("valid_until"))
        if until is None or until <= moment:
            clues.append(row)
        else:
            fresh.append(row)
    if latest is not None:
        conflicts = [row for row in fresh if row.get("value") != latest.get("value")]
        certain = None if conflicts else latest.get("value")
    else:
        values = {row.get("value") for row in fresh}
        conflicts = list(fresh) if len(values) > 1 else []
        certain = fresh[-1].get("value") if len(values) == 1 else None
    return {
        "established": latest,
        "history": established,
        "current": fresh,
        "conflicts": conflicts,
        "clues": clues,
        "certain": certain,
    }


def _positive_int(value, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _positive_float(value, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _require_now(now):
    moment = now or datetime.now().astimezone()
    if not isinstance(moment, datetime) or moment.tzinfo is None:
        raise ValueError("当前时间必须带时区")
    return moment


def _parse_time(value):
    if isinstance(value, datetime):
        stamp = value
    elif isinstance(value, str) and value.strip():
        try:
            stamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if stamp.tzinfo is None:
        return None
    return stamp.astimezone()
