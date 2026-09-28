"""Regressions from live simulation failures, with no robot or network calls."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

from brain.adapters.ports import verify_scene
from execution.robot_api.config import BackendConfig
from execution.robot_api.runtime import RobotRuntime


def load_tool(name):
    path = Path(__file__).resolve().parents[2] / 'simulation/backends/gs/tools' / (name + '.py')
    spec = importlib.util.spec_from_file_location('gs_test_' + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SimulationTruthTests(unittest.TestCase):
    def test_discovery_uses_selected_backend_and_typed_perception_options(self):
        from execution.slaver.robot.module import base, search
        cfg = SimpleNamespace(server_url='http://selected:5001', backends=[SimpleNamespace(enabled=True, url='http://wrong:5002')])
        with patch('execution.robot_api.config.load_robot_api_config', return_value=cfg), patch.object(search, 'load_robot_api_config', return_value=cfg):
            self.assertEqual(base._backend_url(), 'http://selected:5001')
            self.assertEqual(search._backend_url(), 'http://selected:5001')
        from unittest.mock import mock_open
        with patch('builtins.open', mock_open(read_data='perception:\n  scan: false\n  scan_angles: [0, 90]\n')):
            self.assertIs(base._perception_cfg('scan', True), False)
            self.assertEqual(base._perception_cfg('scan_angles', []), [0, 90])

    def test_placement_on_object_requires_measured_geometry(self):
        scene = {'objects': {
            'bread': {'grasped': False, 'bounds': {'min': [.1, .1, .94], 'max': [.3, .2, 1.0]}},
            'plate': {'grasped': False, 'bounds': {'min': [0, 0, .90], 'max': [.4, .4, .93]}}}}
        self.assertTrue(verify_scene('将 bread 放到 plate 上', scene))
        scene['objects']['bread']['bounds']['min'][2] = 1.1
        scene['objects']['bread']['bounds']['max'][2] = 1.2
        self.assertFalse(verify_scene('将 bread 放到 plate 上', scene))
        scene['objects']['bread'].pop('bounds')
        self.assertIsNone(verify_scene('将 bread 放到 plate 上', scene))

    def test_kitchen_fixture_memory_and_post_migration_map_paths(self):
        from execution.slaver.robot import waypoint_manager as wm, path_planner as pp
        from shared.paths import workspace_root
        self.assertEqual(Path(pp._FREE_POINTS_PATH), workspace_root() / 'simulation/nav2/maps/free_points.json')
        with patch.object(wm, '_load_perception_config', return_value=False), patch.object(wm, 'load_waypoints', return_value=[{'name':'nav_0','pos':[1.,2.],'serves':['plate']}]), patch.object(wm, 'get_fixtures', return_value={}), patch('simulation.backends.mujoco.scene.scene_memory.load_state', return_value={'locations':{'nav_0':{'fixture':'counter'}}}):
            self.assertEqual(wm.get_object_pos('counter'), [1.,2.,.9])
        from brain.adapters.execution import parse_sim_action
        self.assertEqual(parse_sim_action('导航到 counter（plate 所在位置）'), ('navigate', 'counter'))

    def test_gs_does_not_use_kitchen_search_or_waypoints(self):
        import asyncio
        from execution.slaver.robot.module import base, search
        tools = {}
        class Registry:
            def tool(self):
                def register(function):
                    tools[function.__name__] = function
                    return function
                return register
        base.register_tools(Registry())
        search.register_tools(Registry())
        with patch('execution.robot_api.config.load_robot_api_config', return_value=SimpleNamespace(backend='mujoco_3dgs', active_backend='mujoco_3dgs', backends=[])), patch.object(base, '_discover_object_waypoint') as discover, patch.object(base, 'find_waypoint') as waypoint, patch.object(base, '_navigate_to', return_value={'success': False, 'result': 'unsupported target'}) as nav:
            asyncio.run(tools['navigate_to_target']('bottle'))
            result = asyncio.run(tools['search_and_grasp']('bottle'))
            discover.assert_not_called()
            waypoint.assert_not_called()
            nav.assert_called_once_with('bottle')
            self.assertIn('failure', result)

    def test_error_and_missing_terminal_are_never_success(self):
        for raw in [{'error': 'missing robot joints'}, {}, {'success': False}, {'success': True, 'error': 'timeout'}]:
            for required in [True, False]:
                self.assertFalse(RobotRuntime._merge([dict(raw, _backend='mujoco', _required=required)])['success'])
        self.assertTrue(RobotRuntime._merge([{'success': True, '_backend': 'mujoco', '_required': True}])['success'])

    def test_gs_navigation_keeps_base_endpoint_and_orientation(self):
        runtime = RobotRuntime.__new__(RobotRuntime)
        backend = BackendConfig('mujoco_3dgs', True, True, True, True)
        self.assertEqual(runtime._nav_call(backend, {'target': [-.5, 0], 'yaw': 90}),
                         ('/nav', {'x': -.5, 'y': 0, 'target_yaw': 90}))

    def test_navigation_verifies_measured_pose_and_requested_yaw(self):
        scene = {'robot': {'base_pos': [-.5, 0, .3], 'yaw': 90}}
        self.assertTrue(verify_scene('导航到 -0.5,0,90', scene))
        self.assertTrue(verify_scene('导航到坐标 (-0.5, 0, 90) 并停止', scene))
        self.assertFalse(verify_scene('导航到 -0.5,0,180', scene))
        self.assertFalse(verify_scene('导航到 0.5,0', scene))
        self.assertIsNone(verify_scene('导航到 -0.5,0', {}))

    def test_xle_base_uses_chassis_and_its_actual_actuators(self):
        move = load_tool('move')
        forward, turn = Mock(name='forward'), Mock(name='turn')
        forward.name, turn.name = 'forward', 'turn'
        env = SimpleNamespace(_gs_cfg=SimpleNamespace(base_link_name='chassis'),
            _link_name_to_idx={'chassis': 7}, data=object(),
            model=Mock(num_actuators=2))
        env.model.get_actuator.side_effect = lambda index: [forward, turn][index]
        move._set_wheel_actuators(env, .4, -.2)
        forward.set_ctrl.assert_called_once_with(env.data, .4)
        turn.set_ctrl.assert_called_once_with(env.data, -.2)
        self.assertEqual(move._get_chassis_link(env)[0], 7)

    def test_rear_navigation_reverses_and_braking_opposes_measured_velocity(self):
        move = load_tool('move')
        import math
        linear, turn = move.navigation_velocity(.5, math.pi)
        self.assertLess(linear, 0)
        self.assertAlmostEqual(turn, 0)
        env = Mock()
        with patch.object(move, 'get_base_info', side_effect=[{'qvel': [-.4, 0, 0]}, {'qvel': [0, 0, 0]}, {'qvel': [0, 0, 0]}]), patch.object(move, '_set_wheel_actuators') as controls:
            move.stop_base(env, settle=True)
            self.assertGreater(controls.call_args_list[0].args[1], 0)
            self.assertEqual(controls.call_args_list[-1].args[1:], (0.0, 0.0))

    def test_unreachable_grasp_does_not_close_or_claim_object(self):
        arm = load_tool('arm')
        env = Mock(grasped_object=None, _link_name_to_idx={'bottle': 1})
        env.get_body_xpos.side_effect = lambda name: np.array([0., 0., 0.]) if name == 'bottle' else np.array([0., 0., 1.])
        with patch.object(arm, 'move_arm', return_value=False), patch.object(arm, 'close_gripper') as close:
            self.assertFalse(arm.grasp(env, 'bottle'))
            close.assert_not_called()
            self.assertIsNone(env.grasped_object)

    def test_moving_hand_without_moving_object_is_not_grasp(self):
        arm = load_tool('arm')
        env = Mock(grasped_object=None, _link_name_to_idx={'bottle': 1})
        env.get_body_xpos.side_effect = lambda name: np.array([0., 0., 0.])
        with patch.object(arm, 'move_arm', return_value=True), patch.object(arm, 'close_gripper'), patch.object(arm, 'open_gripper'):
            self.assertFalse(arm.grasp(env, 'bottle'))
            self.assertIsNone(env.grasped_object)
            self.assertFalse(arm.place(env, 'bottle', [0, 0, 1]))

    def test_new_task_planning_failure_does_not_reuse_old_phases(self):
        import tempfile
        from tests.factories import TaskRuntime
        from brain.storage.tasks import KernelStore
        from tests.brain.test_kernel_reception import FakeBody
        from brain.packages.generic import steps_from_subtasks
        with tempfile.TemporaryDirectory() as root:
            runtime = TaskRuntime(KernelStore(root), FakeBody(), package='generic')
            runtime.open_task('old-task', phases=steps_from_subtasks([
                {'subtask': '抓取 milk_1', 'robot_name': 'FQrobot'}]))
            runtime.drive()
            self.assertEqual(runtime.state, 'succeeded')
            runtime.open_task('new-task', planning=True)
            runtime.plan(lambda: (_ for _ in ()).throw(ConnectionError('model offline')))
            self.assertEqual(runtime.state, 'recovery_required')
            self.assertEqual(runtime.record['phase_specs'], [])
            self.assertEqual(runtime.public_status()['subtask_list'], [])
            self.assertEqual(runtime.record['dispatch_counts'], {})


if __name__ == '__main__':
    unittest.main()
