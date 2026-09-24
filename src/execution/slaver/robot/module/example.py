"""
示例模块 (Example Module)

这是一个示例模板，展示如何添加新的功能模块。

使用方法：
1. 复制这个文件并重命名，例如：camera.py, sensor.py 等
2. 修改模块文档字符串，说明模块用途
3. 在 register_tools 函数中添加你的工具函数
4. 在 skill.py 中导入并调用 register_tools

文件结构：
    - 模块文档字符串：描述模块功能
    - import 语句：导入需要的库
    - 辅助函数：可选的内部函数
    - register_tools 函数：注册所有工具到 MCP 服务器
    - 每个工具函数使用 @mcp.tool() 装饰器
"""

import sys
from typing import Tuple, Dict


def register_tools(mcp):
    """
    注册本模块的所有工具函数到 MCP 服务器

    Args:
        mcp: FastMCP 服务器实例

    注意：
        - 所有工具函数必须是 async 函数
        - 使用 @mcp.tool() 装饰器注册
        - 返回格式：tuple[str, dict] - (结果消息, 状态更新字典)
    """

    @mcp.tool()
    async def example_tool(param1: str, param2: float = 10.0) -> Tuple[str, Dict]:
        """示例工具函数模板.

        这是一个示例函数，展示如何创建新的工具函数。
        请根据实际需求修改此函数。

        Args:
            param1: 第一个参数的说明（必需参数）
            param2: 第二个参数的说明（可选参数，默认值10.0）

        Returns:
            A tuple containing the result message and updated robot state.

        Examples:
            example_tool(param1="test", param2=5.0)  # 使用自定义参数
            example_tool(param1="test")  # 使用默认参数
        """
        # 在这里实现你的功能逻辑

        # 记录日志（使用 sys.stderr 输出到终端）
        print(f"[example.example_tool] Called with param1='{param1}', param2={param2}", file=sys.stderr)

        # 构建返回结果
        result = f"Example tool executed with param1={param1}, param2={param2}"

        # 构建状态更新（可选）
        state_update = {
            "param1": param1,
            "param2": param2,
            "success": True
        }

        # 返回结果和状态
        return result, state_update

    @mcp.tool()
    async def another_example(device_id: int) -> Tuple[str, Dict]:
        """另一个示例工具函数.

        展示如何访问设备或执行其他操作。

        Args:
            device_id: 设备ID，用于标识要操作的设备

        Returns:
            A tuple containing the result message and device status.

        Examples:
            another_example(device_id=1)  # 操作设备1
        """
        # 示例：假设我们在读取传感器数据
        print(f"[example.another_example] Reading from device {device_id}", file=sys.stderr)

        # 模拟读取数据
        result = f"Successfully read data from device {device_id}"

        # 返回设备状态
        state_update = {
            "device_id": device_id,
            "status": "active",
            "data": "sample_data"
        }

        return result, state_update

    # 添加更多工具函数...

    print("[example.py] 示例模块已注册（这是模板，可以根据需要删除或修改）", file=sys.stderr)


# 可选：添加模块级的辅助函数
def _helper_function(value: float) -> float:
    """内部辅助函数示例.

    这种函数不会被注册为工具，仅供模块内部使用。
    """
    return value * 2


# 可选：添加模块初始化/清理函数
def initialize_module():
    """初始化模块（如果需要）"""
    print("[example.py] 模块初始化", file=sys.stderr)


def cleanup_module():
    """清理模块资源（如果需要）"""
    print("[example.py] 模块清理", file=sys.stderr)
