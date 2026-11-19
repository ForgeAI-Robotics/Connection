"""Bind two YAML SOPs to one Runtime task; neither SOP dispatches motion."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import yaml

from contracts.planning import Plan
from contracts.steps import StepSpec
from contracts.tasks import Rejected


DEFINITION = Path(__file__).with_name('definitions') / 'teaching_session_v2.yaml'


def validate_definition(value):
    if (not isinstance(value, dict) or type(value.get('schema_version')) is not int or value['schema_version'] != 1
            or value.get('package') != 'teaching' or not isinstance(value.get('sops'), dict)
            or set(value['sops']) != {'start', 'finish'}):
        raise Rejected('示教 YAML 必须定义 start 和 finish 两个 SOP')
    for name, trigger in (('start', '开始示教'), ('finish', '结束示教')):
        sop = value['sops'][name]
        if (not isinstance(sop, dict) or sop.get('id') != 'teaching.' + name or sop.get('version') != '2'
                or sop.get('triggers') != [trigger] or not isinstance(sop.get('description'), str)
                or not isinstance(sop.get('steps'), list) or len(sop['steps']) != 1):
            raise Rejected('示教 SOP 身份或步骤无效：' + name)
        step = sop['steps'][0]
        if (not isinstance(step, dict) or set(step) - {'step_id', 'kind', 'display_name', 'wait_for'}
                or step.get('step_id') != ('TEACHING_STARTED' if name == 'start' else 'TEACHING_FINISHED')
                or step.get('kind') != 'teaching_' + name
                or step.get('wait_for', '') != ('' if name == 'start' else 'finish_teaching')
                or not isinstance(step.get('display_name'), str)):
            raise Rejected('示教 SOP 只允许登记的会话步骤及结束信号')
    return value


def load_definition():
    return validate_definition(yaml.safe_load(DEFINITION.read_text(encoding='utf-8')))


def metadata(definition, name):
    return {k: deepcopy(v) for k, v in definition['sops'][name].items() if k != 'steps'}


def plan(selection):
    definition = validate_definition(selection['teaching_definition'])
    digest = hashlib.sha256(json.dumps(definition, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    steps = []
    for name in ('start', 'finish'):
        parameters = {'teaching': deepcopy(selection['teaching']), 'sop': metadata(definition, name),
                      'definition_sha256': digest}
        steps.extend(StepSpec(**step, parameters=parameters) for step in definition['sops'][name]['steps'])
    return Plan(tuple(steps), '两个 YAML SOP 共用一个示教任务；开始保存位姿和头部快照，结束核验并归档原始快照。')
