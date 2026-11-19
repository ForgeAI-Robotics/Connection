"""Teaching input parameters; no robot commands or session state."""
from contracts.tasks import Rejected


def is_start(text):
    return '开始示教' in ''.join(c for c in str(text) if not c.isspace() and c not in '，。,.!！?？、')


def validate_parameters(value):
    if not isinstance(value, dict) or set(value) != {'intent', 'index', 'name'}:
        raise Rejected('示教参数必须只包含 intent、index、name')
    if value['intent'] != 'start_teaching':
        raise Rejected('请明确要求开始示教，并说明示教次数和流程名称')
    if type(value['index']) is not int or value['index'] <= 0:
        raise Rejected('请提供正整数示教次数，例如第 1 次')
    if not isinstance(value['name'], str) or not 1 <= len(value['name'].strip()) <= 128:
        raise Rejected('请提供 1–128 字的示教流程名称')
    return dict(value, name=value['name'].strip())
