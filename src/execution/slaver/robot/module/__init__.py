"""
机器人模块包
- base: 底盘控制模块（导航）
- grasp: 抓取控制模块
- place: 放置/开关控制模块
- example: 示例模块（模板）
"""

from .base import register_tools as register_base_tools
from .grasp import register_tools as register_grasp_tools
from .place import register_tools as register_place_tools
from .example import register_tools as register_example_tools
from .raw import register_tools as register_raw_tools

__all__ = [
    'register_base_tools',
    'register_grasp_tools',
    'register_place_tools',
    'register_example_tools',
    'register_raw_tools',
]
