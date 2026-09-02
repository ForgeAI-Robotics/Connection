"""进程内 mock 世界 —— 八月「G1 会议接待」demo(替代桌面 serve_desk,先跑通大脑程序结构)。

分两层(对应「VLA 自报 vs 大脑重新观测」这个 demo 核心):
  - **物理操作**(w_set_light / w_pick_cola / w_walk / w_place_cola):会被 fault 注入干扰,
    模拟 VLA/导航「抓不到 / 过不去 / 放偏还谎报成功」。技能层(reception_skills)调这些。
  - **观测接口**(w_count_cola / w_robot_holding / w_robot_at / w_last_slot ...):永远返回世界
    真相。大脑闭环(reception_loop)调这些做**状态确认**,不采信技能层的自报。

真机替换:mock world → VLA 组 /grasp /place + 导航组 /nav(语义地名)+ 世界模型组场景图(计数)。
"""

# 进程内单例世界状态。reset_world() 初始化;各操作就地改。
WORLD: dict = {}


def reset_world(headcount=4, meeting_cola=1, tea_cola=10, chairs_blocking=False):
    """重置世界。headcount=参会人数;meeting_cola=会议室现有可乐;tea_cola=茶水间库存。"""
    WORLD.clear()
    WORLD.update({
        "headcount": headcount,
        "rooms": {
            "会议室": {"lights": "off", "cola": meeting_cola,
                     "chairs_blocking": chairs_blocking,
                     "placed_slots": [], "floor_cola": 0},  # placed_slots=已摆放槽位;floor_cola=放偏掉落
            "茶水间": {"lights": "off", "cola": tea_cola},
        },
        "robot": {"at": "会议室", "holding": None},   # 接待场景:机器人起点在会议室
        "fault": {"next": None},                      # grasp_fail | place_miss | walk_blocked | label_wrong
    })


def inject_fault(kind):
    """注入一次性故障(下一次对应操作触发,触发后清除)。"""
    WORLD["fault"]["next"] = kind


def _consume_fault(kind):
    if WORLD["fault"]["next"] == kind:
        WORLD["fault"]["next"] = None
        return True
    return False


# ---------- 物理操作(技能层调;可能被 fault 干扰,可能与自报不符)----------

def w_set_light(room, on=True):
    WORLD["rooms"][room]["lights"] = "on" if on else "off"
    return True


def w_pick_cola(room):
    """从 room 取一罐可乐到手上。grasp_fail → 没抓到(手上仍空,库存不减)。"""
    if _consume_fault("grasp_fail"):
        return False
    if WORLD["rooms"][room]["cola"] <= 0:
        return False
    WORLD["rooms"][room]["cola"] -= 1
    WORLD["robot"]["holding"] = "可乐"
    return True


def w_walk(to_room):
    """走到 to_room。walk_blocked → 办公椅挡路,没走到(位置不变)。"""
    if _consume_fault("walk_blocked"):
        return False
    WORLD["robot"]["at"] = to_room
    return True


def w_place_cola(room):
    """把手上可乐放到 room。
    正常:会议室 cola +1,记一个合格槽位(间隔+朝向都对)。
    place_miss:可乐脱手但没入有效位(掉地上)→ cola 不增(大脑重数逮住),仍返回 True 模拟谎报。
    label_wrong:入位了但标签朝向乱 → cola 增但槽位 label_aligned=False(大脑查朝向逮住)。
    """
    if WORLD["robot"]["holding"] != "可乐":
        return False                       # 手上没可乐,放个寂寞(通常是前序 pick 谎报导致)
    WORLD["robot"]["holding"] = None       # 可乐离手
    if _consume_fault("place_miss"):
        WORLD["rooms"][room]["floor_cola"] += 1   # 掉地上,没进有效摆放区
        return True                        # 谎报成功
    label_ok = not _consume_fault("label_wrong")
    WORLD["rooms"][room]["cola"] += 1
    n = len(WORLD["rooms"][room]["placed_slots"])
    WORLD["rooms"][room]["placed_slots"].append(
        {"index": n, "in_place": True, "spaced": True, "label_aligned": label_ok})
    return True


# ---------- 观测接口(大脑调;永远返回真相,用于状态确认)----------

def w_headcount():           return WORLD["headcount"]
def w_light_state(room):     return WORLD["rooms"][room]["lights"]
def w_count_cola(room):      return WORLD["rooms"][room]["cola"]
def w_chairs_blocking(room): return WORLD["rooms"][room].get("chairs_blocking", False)
def w_robot_holding():       return WORLD["robot"]["holding"]
def w_robot_at():            return WORLD["robot"]["at"]


def w_last_slot(room):
    """最近一次摆放的槽位(含间隔/朝向是否合格);无则 None。"""
    slots = WORLD["rooms"][room]["placed_slots"]
    return slots[-1] if slots else None


def w_all_slots(room):
    """所有已摆放槽位(终局全局复核用:逐罐查间隔/朝向)。"""
    return list(WORLD["rooms"][room]["placed_slots"])
