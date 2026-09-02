#!/usr/bin/env python3
"""Launch FQPlanner MuJoCo bridge with slam_toolbox."""

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    backend_url = LaunchConfiguration("backend_url")
    use_sim_time = LaunchConfiguration("use_sim_time")
    rviz_config = os.path.join(
        get_package_share_directory("fqplanner_nav_bridge"),
        "config",
        "mujoco_slam.rviz",
    )

    return LaunchDescription([
        DeclareLaunchArgument("backend_url", default_value="http://127.0.0.1:5001"),
        DeclareLaunchArgument("use_sim_time", default_value="false"),
        Node(
            package="fqplanner_nav_bridge",
            executable="mujoco_bridge",
            name="fqplanner_mujoco_bridge",
            output="screen",
            parameters=[{
                "backend_url": backend_url,
                "publish_rate": 20.0,
                "scan_rate": 5.0,
            }],
        ),
        Node(
            package="slam_toolbox",
            executable="sync_slam_toolbox_node",
            name="slam_toolbox",
            output="screen",
            parameters=[{
                "use_sim_time": use_sim_time,
                "base_frame": "base_link",
                "odom_frame": "odom",
                "map_frame": "map",
                "scan_topic": "/scan",
            }],
        ),
        Node(
            package="rviz2",
            executable="rviz2",
            name="rviz2",
            output="screen",
            arguments=["-d", rviz_config],
        ),
    ])
