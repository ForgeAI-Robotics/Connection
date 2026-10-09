"""Exact standalone task controls; never match a substring of a motion request."""


def control_action(text):
    if not isinstance(text, str):
        return None
    value = text.strip().lower().rstrip('。！!，, ')
    for action, commands in {
        'cancel': {'取消', '取消任务', '取消当前任务', '/cancel'},
        'pause': {'暂停', '暂停任务', '暂停当前任务', '/pause', '停止', '停止任务', '停止当前任务', '/stop'},
        'continue': {'继续', '继续任务', '继续当前任务', '恢复任务', '/continue', '/resume'},
    }.items():
        if value in commands:
            return action
    return None
