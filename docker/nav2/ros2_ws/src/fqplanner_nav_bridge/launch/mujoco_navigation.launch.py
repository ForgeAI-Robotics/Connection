#!/usr/bin/env python3
"""Launch FQPlanner MuJoCo bridge with Nav2."""

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.conditions import IfCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def _default_project_root():
    env_root = os.environ.get("FQPLANNER_ROOT")
    if env_root:
        return env_root

    candidates = [
        os.getcwd(),
        os.path.join(os.getcwd(), "FQPlanner_Mujoco"),
        os.path.join(os.getcwd(), "..", "FQPlanner_Mujoco"),
        os.path.join(os.getcwd(), "..", "..", "FQPlanner_Mujoco"),
    ]
    package_share = get_package_share_directory("fqplanner_nav_bridge")
    candidates.extend([
        os.path.join(package_share, "..", "..", "..", "..", "..", "FQPlanner_Mujoco"),
        os.path.join(package_share, "..", "..", "..", "..", "..", "..", "FQPlanner_Mujoco"),
    ])

    for candidate in candidates:
        root = os.path.abspath(candidate)
        if os.path.exists(os.path.join(root, "nav2", "maps", "kitchen_map.yaml")):
            return root
    return os.getcwd()


def generate_launch_description():
    package_share = get_package_share_directory("fqplanner_nav_bridge")
    nav2_launch_dir = os.path.join(get_package_share_directory("nav2_bringup"), "launch")
    project_root = _default_project_root()
    default_map = os.path.join(project_root, "nav2", "maps", "kitchen_map.yaml")

    backend_url = LaunchConfiguration("backend_url")
    nav_bridge_url = LaunchConfiguration("nav_bridge_url")
    http_host = LaunchConfiguration("http_host")
    http_port = LaunchConfiguration("http_port")
    map_file = LaunchConfiguration("map")
    params_file = LaunchConfiguration("params_file")
    use_sim_time = LaunchConfiguration("use_sim_time")
    launch_rviz = LaunchConfiguration("launch_rviz")
    rviz_config = os.path.join(package_share, "config", "mujoco_navigation.rviz")

    return LaunchDescription([
        DeclareLaunchArgument("backend_url", default_value="http://127.0.0.1:5001"),
        DeclareLaunchArgument("nav_bridge_url", default_value="http://127.0.0.1:5102"),
        DeclareLaunchArgument("http_host", default_value="0.0.0.0"),
        DeclareLaunchArgument("http_port", default_value="5102"),
        DeclareLaunchArgument("map", default_value=default_map),
        DeclareLaunchArgument(
            "params_file",
            default_value=os.path.join(package_share, "config", "nav2_params.yaml"),
        ),
        DeclareLaunchArgument("use_sim_time", default_value="false"),
        DeclareLaunchArgument("launch_rviz", default_value="false"),
        Node(
            package="fqplanner_nav_bridge",
            executable="mujoco_bridge",
            name="fqplanner_mujoco_bridge",
            output="screen",
            parameters=[{
                "backend_url": backend_url,
                "fake_localization": True,
                "publish_initial_pose": True,
                "initial_pose_repeats": 120,
                "initial_pose_period": 1.0,
            }],
        ),
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(os.path.join(nav2_launch_dir, "bringup_launch.py")),
            launch_arguments={
                "map": map_file,
                "use_sim_time": use_sim_time,
                "params_file": params_file,
            }.items(),
        ),
        Node(
            package="fqplanner_nav_bridge",
            executable="nav2_goal_bridge",
            name="fqplanner_nav2_goal_bridge",
            output="screen",
            parameters=[{
                "backend_url": backend_url,
                "http_host": http_host,
                "http_port": http_port,
                "success_xy_tolerance": 0.10,
                "success_yaw_tolerance_deg": 8.0,
            }],
        ),
        Node(
            package="rviz2",
            executable="rviz2",
            name="rviz2",
            output="screen",
            arguments=["-d", rviz_config],
            condition=IfCondition(launch_rviz),
        ),
    ])
