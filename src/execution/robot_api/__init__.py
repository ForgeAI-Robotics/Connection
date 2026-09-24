"""Unified semantic robot API."""

from .client import (
    capture_image,
    get_arm_status,
    get_base_status,
    get_fixtures,
    get_map_data,
    get_object_pos,
    get_objects,
    get_robot_state,
    get_scene,
    grasp_object,
    is_object_grasped,
    move_forward,
    navigate_to,
    place_object,
    rotate,
    set_backend_url,
)

__all__ = [
    "capture_image",
    "get_arm_status",
    "get_base_status",
    "get_fixtures",
    "get_map_data",
    "get_object_pos",
    "get_objects",
    "get_robot_state",
    "get_scene",
    "grasp_object",
    "is_object_grasped",
    "move_forward",
    "navigate_to",
    "place_object",
    "rotate",
    "set_backend_url",
]
