"""
sim.py - RoboCasa 仿真接口

提供统一的仿真 API 接口，供 MCP 工具调用。
使用 urllib 标准库，无需额外安装依赖。

使用方式:
    from sim import call_sim
    result = call_sim("/grasp", {"obj_name": "mug"})
"""

import json
import urllib.request
import urllib.error

# 默认服务器地址
SERVER_URL = "http://127.0.0.1:5002"
TIMEOUT = 120


def set_server_url(url):
    """设置服务器地址"""
    global SERVER_URL
    SERVER_URL = url


def call_sim(endpoint, data=None):
    """
    调用 RoboCasa 仿真服务器 API

    Args:
        endpoint: API 路径 (如 "/grasp", "/place", "/nav")
        data: POST 数据 (dict), None 则为 GET 请求

    Returns:
        dict: 服务器返回的 JSON 结果
            成功: {"success": True, "result": ...}
            失败: {"success": False, "result": "错误信息"}
    """
    url = f"{SERVER_URL}{endpoint}"

    try:
        if data is not None:
            req = urllib.request.Request(
                url,
                data=json.dumps(data).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        else:
            req = urllib.request.Request(url)

        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))

    except urllib.error.URLError as e:
        return {"success": False, "result": f"仿真服务器连接失败: {e}"}
    except Exception as e:
        return {"success": False, "result": f"请求错误: {e}"}


# ============================================================
# 状态查询
# ============================================================

def get_arm_status():
    """获取机械臂状态（末端位置、夹爪状态）"""
    return call_sim("/status")


def get_base_status():
    """获取底座状态（位置、偏航角）"""
    return call_sim("/base_status")


def get_objects():
    """获取所有物体位置和抓取状态"""
    return call_sim("/objects")


def get_scene():
    """获取完整场景信息：物体、家具、机器人位置"""
    return call_sim("/scene")


# ============================================================
# 机械臂控制
# ============================================================

def grasp_object(obj_name, snap_threshold=0.15):
    """
    抓取物体

    Args:
        obj_name: 物体名称
        snap_threshold: 吸附触发距离（米）

    Returns:
        dict: {"success": bool, ...}
    """
    return call_sim("/grasp", {
        "obj_name": obj_name,
        "snap_threshold": snap_threshold
    })


def place_object(obj_name, target_pos, snap_threshold=0.15):
    """
    放置物体到目标位置

    Args:
        obj_name: 物体名称
        target_pos: 目标位置 [x, y, z]
        snap_threshold: 瞬移触发距离（米）

    Returns:
        dict: {"success": bool, ...}
    """
    return call_sim("/place", {
        "obj_name": obj_name,
        "target": target_pos,
        "snap_threshold": snap_threshold
    })


def move_arm(target_pos, max_steps=200, pos_threshold=0.03):
    """
    移动机械臂到目标位置

    Args:
        target_pos: 目标位置 [x, y, z]
        max_steps: 最大仿真步数
        pos_threshold: 到达判定阈值（米）

    Returns:
        dict: {"reached": bool, "ee_pos": [x, y, z]}
    """
    return call_sim("/move_to", {
        "target": target_pos,
        "max_steps": max_steps,
        "pos_threshold": pos_threshold
    })


def open_gripper():
    """打开夹爪"""
    return call_sim("/open_gripper", {})


def close_gripper():
    """关闭夹爪"""
    return call_sim("/close_gripper", {})


# ============================================================
# 底盘导航
# ============================================================

def navigate(x, y, target_yaw=None):
    """
    底盘导航到目标位置

    Args:
        x: 目标世界坐标 x
        y: 目标世界坐标 y
        target_yaw: 目标偏航角（度），None 表示不调整朝向

    Returns:
        dict: {"success": bool, "pos": [x, y, z], "yaw": float}
    """
    payload = {"x": x, "y": y}
    if target_yaw is not None:
        payload["target_yaw"] = target_yaw
    return call_sim("/nav", payload)


def navigate_path_by_points(path, target_yaw=None):
    """
    沿预计算路径导航（绕障）

    Args:
        path: [{"x": ..., "y": ...}, ...] 路径点列表
        target_yaw: 终点朝向（度），None 表示不调整

    Returns:
        dict: {"success": bool, "pos": [...], "yaw": float}
    """
    payload = {"path": path}
    if target_yaw is not None:
        payload["w"] = target_yaw
    return call_sim("/nav_path", payload)


# ============================================================
# 截图
# ============================================================

def capture_screenshot(camera_name="overhead_cam", width=640, height=480):
    """从指定相机捕获截图

    Args:
        camera_name: 相机名称 (overhead_cam, side_cam, robot0_frontview, robot0_eye_in_hand)
        width: 图像宽度
        height: 图像高度

    Returns:
        dict: {"success": bool, "image": "base64_string"} 或 {"success": False, "result": "错误信息"}
    """
    return call_sim("/screenshot", {
        "camera_name": camera_name,
        "width": width,
        "height": height,
    })


# ============================================================
# 辅助函数
# ============================================================

def get_object_pos(obj_name):
    """
    获取指定物体的位置

    Args:
        obj_name: 物体名称

    Returns:
        list: [x, y, z] 或 None（物体不存在）
    """
    result = get_objects()
    if result and "error" not in result:
        if obj_name in result:
            return result[obj_name]["pos"]
    return None


def is_object_grasped(obj_name):
    """
    检查物体是否被抓取

    Args:
        obj_name: 物体名称

    Returns:
        bool: 是否被抓取
    """
    result = get_objects()
    if result and "error" not in result:
        if obj_name in result:
            return result[obj_name].get("grasped", False)
    return False


def get_ee_pos():
    """
    获取末端执行器位置

    Returns:
        list: [x, y, z] 或 None
    """
    result = get_arm_status()
    if result and "error" not in result:
        return result.get("ee_pos")
    return None


def get_base_pos():
    """
    获取底座位置

    Returns:
        list: [x, y, z] 或 None
    """
    result = get_base_status()
    if result and "error" not in result:
        return result.get("pos")
    return None
