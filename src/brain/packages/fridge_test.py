"""Explicit standalone test compiled into the existing Runtime, without navigation."""
from dataclasses import replace
from contracts.steps import StepSpec
from contracts.tasks import Rejected

SOP = dict(id='fridge.close_test', version='1',
    description='单独测试关冰箱：复用现有精调、生成关门轨迹和效果核验。',
    constraints='只在现场已就绪时测试；不执行接待、导航、抓取或运送，不声明持物成功。',
    triggers=['单独测试关冰箱'])


def phases(skill):
    closed = next((s for s in skill.real_steps if s.kind == 'close_fridge'), None)
    if closed is None:
        raise Rejected('当前 Skill 未登记关冰箱步骤')
    test = replace(closed, step_id='TEST_CLOSE_FRIDGE', kind='close_fridge_test',
                   prefix='test-close-fridge', display_name='单独测试关冰箱',
                   handoff_before='', requires_gate=True, navigation_step_id='',
                   required_holding='', writes='')
    return (
        StepSpec('INITIALIZING', 'local'), StepSpec('FETCHING_WORLD', 'local'),
        test,
        StepSpec('VERIFY_TEST_CLOSE', 'verify', evidence=closed.evidence, display_name='确认门已关闭且双臂回位'),
    )
