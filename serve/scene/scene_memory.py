"""
scene_memory.py - 场景状态记忆
记录物体相对位置（在哪个家具/位置上），在动作执行后自动更新。
重启仿真时自动恢复为初始状态。
"""

import os
import shutil
import numpy as np
import yaml
from datetime import datetime

_DIR = os.path.join(os.path.dirname(__file__), 'config')
STATE_PATH = os.path.join(_DIR, 'scene_state.yaml')
INITIAL_PATH = os.path.join(_DIR, 'scene_state_initial.yaml')
WAYPOINTS_PATH = os.path.join(_DIR, 'waypoints.yaml')


def _load_waypoint_coords() -> dict:
    """返回工作点名→坐标的映射"""
    with open(WAYPOINTS_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return {wp['name']: wp['pos'][:2] for wp in data['waypoints']}


def load_state() -> dict:
    with open(STATE_PATH, 'r', encoding="utf-8") as f:
        return yaml.safe_load(f)


def save_state(state: dict):
    state['last_updated'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(STATE_PATH, 'w', encoding="utf-8") as f:
        yaml.dump(state, f, allow_unicode=True, default_flow_style=False)


def reset_to_initial():
    shutil.copy(INITIAL_PATH, STATE_PATH)
    print("[SceneMemory] 场景状态已重置为初始状态")


def reset_belief_unknown():
    """学习模式:把 belief 清空——保留工作点→fixture 结构,但所有物体回到 unknown。

    全可观测模式用 reset_to_initial()(belief 直接 = 真值,无需探索);
    学习模式用这个:机器人开局不知道任何物体在哪,靠逐工作点导航+局部观测逐步填回
    belief,从而产生 ALFWorld 那样的 memory_speedup 学习曲线。
    """
    with open(INITIAL_PATH, "r", encoding="utf-8") as f:
        state = yaml.safe_load(f) or {}
    for info in (state.get('locations') or {}).values():
        info['objects'] = []
    save_state(state)
    print("[SceneMemory] 学习模式:belief 已清空(所有物体 → unknown)")


def get_object_location(obj_name: str) -> str:
    """返回物体所在的工作点名，如 'nav_012'"""
    state = load_state()
    for loc, info in state['locations'].items():
        if obj_name in (info.get('objects') or []):
            return loc
    return 'unknown'


def get_object_coords(obj_name: str) -> list:
    """
    返回物体所在工作点的坐标
    记忆模式下用这个而不是实时 API
    """
    location = get_object_location(obj_name)
    if location == 'unknown' or location == 'robot_hand':
        return None
    coords = _load_waypoint_coords()
    wp_coords = coords.get(location)
    if wp_coords:
        return [wp_coords[0], wp_coords[1], 0.9]
    return None


def move_object(obj_name: str, to_location: str):
    """更新物体位置，to_location 是工作点名或 'robot_hand'"""
    state = load_state()
    from_location = 'unknown'
    for loc, info in state['locations'].items():
        objs = info.get('objects') or []
        if obj_name in objs:
            objs.remove(obj_name)
            info['objects'] = objs
            from_location = loc
            break

    if to_location not in state['locations']:
        state['locations'][to_location] = {'fixture': None, 'objects': []}
    objs = state['locations'][to_location].get('objects') or []
    if obj_name not in objs:
        objs.append(obj_name)
    state['locations'][to_location]['objects'] = objs

    save_state(state)
    print(f"[SceneMemory] {obj_name}: {from_location} → {to_location}")


def get_location_objects(location: str) -> list:
    state = load_state()
    return state['locations'].get(location, {}).get('objects') or []


def get_all_locations() -> dict:
    state = load_state()
    return {loc: info.get('objects') or []
            for loc, info in state['locations'].items()}


def get_belief_by_object(all_objects=None) -> dict:
    """belief 的物体视角：{obj: {'location': <家具名 / robot_hand / 工作点 / unknown>}}。

    location 优先用工作点绑定的家具名（可读，如 counter / sink / stove），没有就退回工作点名。
    传入 all_objects 时，belief 里没出现的物体补 'unknown'（学习模式起始 / 尚未发现）。
    这是前端「物体状态(belief)」表的数据源，对齐用户要的 {mug:{location:...}} 格式。
    """
    state = load_state()
    result = {}
    for loc, info in (state.get('locations') or {}).items():
        objs = info.get('objects') or []
        if not objs:
            continue
        if loc == 'robot_hand':
            label = 'robot_hand'
        else:
            label = info.get('fixture') or loc
        for o in objs:
            result[o] = {'location': label}
    for o in (all_objects or []):
        result.setdefault(o, {'location': 'unknown'})
    return result


def build_initial_state(env) -> dict:
    """
    根据仿真中物体的实际位置自动生成初始场景状态。

    每次 env.reset() 后调用，可确保工作点名称与当前 waypoints.yaml 完全一致。
    """
    objects_path = os.path.join(_DIR, 'objects.yaml')
    with open(WAYPOINTS_PATH, "r", encoding="utf-8") as f:
        wps_data = yaml.safe_load(f)
    with open(objects_path, "r", encoding="utf-8") as f:
        objs_data = yaml.safe_load(f)

    waypoints = wps_data['waypoints']
    # 物体名 → 所在家具（来自 objects.yaml placement.fixture）
    obj_fixtures = {
        o['name']: o.get('placement', {}).get('fixture')
        for o in objs_data.get('objects', [])
    }

    # 家具关键词（用于从 serves 字段推断 fixture 类型）
    _FIXTURE_KW = {'counter', 'island', 'sink', 'stove', 'fridge', 'microwave', 'oven', 'cabinet'}

    # 初始化每个工作点
    locations = {}
    for wp in waypoints:
        fixture = None
        for s in wp.get('serves', []):
            if s.lower() in _FIXTURE_KW:
                fixture = s
                break
        locations[wp['name']] = {'fixture': fixture, 'objects': []}
    locations['robot_hand'] = {'fixture': None, 'objects': []}

    # 按最近距离把物体分配到工作点
    coords = {wp['name']: wp['pos'][:2] for wp in waypoints}
    for obj_name in list(env.obj_body_id.keys()):
        try:
            pos = env.get_object_pos(obj_name)
            p = np.array(pos[:2])
            best_wp = min(coords, key=lambda n: np.linalg.norm(p - np.array(coords[n])))
            entry = locations[best_wp]
            entry['objects'].append(obj_name)
            # serves 中没有家具关键词时，用物体自身 placement fixture 补充
            if entry['fixture'] is None:
                entry['fixture'] = obj_fixtures.get(obj_name)
        except Exception as e:
            print(f"[SceneMemory] 无法获取 {obj_name} 位置，跳过: {e}")

    return {'last_updated': '初始状态', 'locations': locations}


def coords_to_waypoint(pos: list) -> str:
    """根据放置坐标找最近的工作点名，总是返回最近的"""
    if pos is None:
        return 'unknown'
    p = np.array(pos[:2])
    coords = _load_waypoint_coords()

    best_name = 'unknown'
    best_dist = float('inf')  # 不设上限，总是返回最近的
    for name, wp_coords in coords.items():
        dist = np.linalg.norm(p - np.array(wp_coords))
        if dist < best_dist:
            best_dist = dist
            best_name = name

    print(f"[SceneMemory] 放置位置 {p.tolist()} → 最近工作点 {best_name} (dist={best_dist:.2f})")
    return best_name
