"""SIMPLE task declaration only; physical commands use the common BodyAdapter."""
from brain.adapters.execution import BodyAdapter
from contracts.tasks import Rejected


class SimpleBodyAdapter(BodyAdapter):
    def prepare_plan(self, task_id, steps, snapshot):
        from brain.skills.body_plan import segments
        from brain.packages.reception_skill import from_snapshot
        skill = from_snapshot(snapshot)
        nav, vla = self.dream.health(), self.vla.health()
        simulation = nav.get('simulation') or {}
        if (simulation.get('kind') != 'simple_physics'
                or simulation.get('task_plan_version') != 1
                or not simulation.get('instance_id')
                or simulation.get('instance_id') != (vla.get('simulation') or {}).get('instance_id')
                or simulation.get('navigation_legs') != skill.navigation_points):
            raise Rejected('SIMPLE 尚未接入通用身体计划，或路线／实例不匹配')
        expected = segments(steps)
        result = self.dream.http.request_json('POST', '/v1/task_plan', {
            'task_id': task_id, 'instance_id': simulation['instance_id'], 'segments': expected})[0]
        if result.get('task_id') != task_id or result.get('segments') != expected or result.get('accepted') is not True:
            raise Rejected('SIMPLE 任务计划回执不匹配')
