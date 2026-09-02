"""
search_and_grasp — ALFWorld 专用自动搜索工具。

代替手动「raw_action("go to drawer N") + raw_action("open drawer N")」序列。
直接从 admissible_commands 拿当前可达位置，逐个导航 → open → 检测目标，
找到即取，绝不需要 LLM 生成容器实例号。

对应 ALFRED gold plan 里的 `GotoLocation(物体处) + PickupObject(X)` 这一对：
gold planner 有完美信息知道物体在哪，我们没有，所以用遍历探索替代。

适用场景：
  "搜索并抓取 mug" → search_and_grasp(object_name="mug")
  "搜索并抓取 cd"  → search_and_grasp(object_name="cd")
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Optional

# location_memory.json 在 master/memory/ 下，search.py 在 slaver/robot/module/
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_LOCATION_MEMORY_FILE = os.path.join(_REPO_ROOT, "master", "memory", "location_memory.json")


def _load_location_hint(object_base: str) -> str | None:
    """从 location_memory.json 取上次找到该物体的位置，找不到返回 None。
    设 DISABLE_LOCATION_MEMORY=1 可彻底禁用（用于对照实验）。
    """
    if os.environ.get("DISABLE_LOCATION_MEMORY") == "1":
        return None
    try:
        if os.path.exists(_LOCATION_MEMORY_FILE):
            mem = json.loads(open(_LOCATION_MEMORY_FILE, encoding="utf-8").read())
            entry = mem.get(object_base)
            if entry:
                return entry.get("location")
    except Exception:
        pass
    return None


def _invalidate_location_memory(object_base: str) -> None:
    """漂移检测:去记忆位置没找到目标 → 清除过期条目,等下次找到后重建。"""
    if os.environ.get("DISABLE_LOCATION_MEMORY") == "1":
        return
    try:
        if os.path.exists(_LOCATION_MEMORY_FILE):
            mem = json.loads(open(_LOCATION_MEMORY_FILE, encoding="utf-8").read())
            if object_base in mem:
                del mem[object_base]
                with open(_LOCATION_MEMORY_FILE, "w", encoding="utf-8") as f:
                    json.dump(mem, f, indent=2, ensure_ascii=False)
                print(f"[search] 漂移检测: 已清除 '{object_base}' 的过期位置记忆", file=sys.stderr)
    except Exception:
        pass


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from robot_api.config import load_robot_api_config


def _backend_url() -> Optional[str]:
    """Return URL of the first enabled backend, or None."""
    try:
        cfg = load_robot_api_config()
        for b in cfg.backends:
            if b.enabled:
                return b.url.rstrip("/")
    except Exception:
        pass
    return None


def _get(path: str, timeout: float = 10.0) -> dict:
    url = (_backend_url() or "") + path
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"error": str(e)}


def _post(path: str, body: dict, timeout: float = 10.0) -> dict:
    url = (_backend_url() or "") + path
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"success": False, "result": str(e)}


def _scene() -> dict:
    s = _get("/scene_state")
    return s if isinstance(s, dict) else {}


def _admissible() -> list:
    return _scene().get("admissible_commands") or []


def _held_object() -> Optional[str]:
    """Return what the robot currently holds (e.g. 'towel 1'), or None."""
    return _scene().get("holding") or None


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", str(s).strip().lower())


def _base(name: str) -> str:
    """Remove trailing instance number: 'mug 2' → 'mug'."""
    return re.sub(r"\s*\d+$", "", _norm(name))


# 带门容器（搜起来慢：要 go+open，且开容器会破坏后续导航）。物体多在开放表面，
# 所以第一遍把容器排到最后，常见物体几步就命中，省 ALFWorld step（预算仅 50/局）。
_CONTAINER_KW = ("cabinet", "drawer", "fridge", "microwave", "safe", "box", "kettle")


def _is_container(go_cmd: str) -> bool:
    return any(k in go_cmd.lower() for k in _CONTAINER_KW)


def _drop_held(admissible: list) -> bool:
    """Put down whatever is currently held at the current location.

    Needed because ALFWorld only allows holding one object: if a previous subtask
    grabbed the wrong thing (e.g. towel instead of cloth), we can't take the target
    until we drop it. Returns True if something was dropped.
    """
    held = _held_object()
    if not held:
        return False
    held_base = _base(held)
    for cmd in admissible:
        c = _norm(cmd)
        if (c.startswith("move ") or c.startswith("put ")) and held_base in c:
            r = _post("/raw", {"command": cmd})
            if r.get("success"):
                print(f"[search] 放下错误持有物 '{held}': {cmd}", file=sys.stderr)
                return True
    return False


def _find_take(object_base, admissible, exclude_from=""):
    """Return the exact 'take <obj> from <loc>' command if target is graspable now.

    exclude_from: skip 'take ... from <loc>' at this receptacle (used by pick_two
    second round so we don't grab back the instance we just placed at the target).
    """
    exclude = _norm(exclude_from)
    for cmd in admissible:
        c = _norm(cmd)
        if c.startswith("take ") and object_base in c:
            if exclude and (" from " + exclude) in c:
                continue
            return cmd
    return None


def _is_mujoco_backend() -> bool:
    """True = 当前后端不是 ALFWorld(即 MuJoCo 等几何后端)。

    ALFWorld 的 search_and_grasp 读文本 admissible_commands;MuJoCo 的 /scene_state
    返回的是工作点→物体的 belief,没有 admissible_commands → 文本搜索必然失败。
    所以 MuJoCo 上必须改走工作点发现。
    """
    try:
        sys.path.insert(0, os.path.dirname(__file__))
        from base import _is_alfworld
        return not _is_alfworld()
    except Exception:
        return False


def _mujoco_search_and_grasp(object_base: str) -> str:
    """MuJoCo 原生"搜索并抓取":工作点发现(belief 命中直达 / 未命中逐工作点搜) + 抓取。

    复用 base._discover_object_waypoint(真实驱动 /nav、局部观测、命中后更新 belief),
    抓取走 robot_api(后端 /grasp 的 lift 阶段会把 belief 更新成 robot_hand)。
    这就是 ALFWorld 文本搜索在 MuJoCo 上的等价物——也更贴近真实机器人(有位置、逐点观测)。
    """
    try:
        sys.path.insert(0, os.path.dirname(__file__))
        from base import _discover_object_waypoint
        from robot_api.client import grasp_object as _grasp
    except Exception as e:
        msg = f"MuJoCo 搜索初始化失败: {e}"
        print(f"[search] ✗ {msg}", file=sys.stderr)
        return json.dumps([msg, {"_status": "failure"}])

    ok, msg = _discover_object_waypoint(object_base)
    if not ok:
        print(f"[search] ✗ (MuJoCo 工作点发现) {msg}", file=sys.stderr)
        return json.dumps([msg, {"_status": "failure"}])

    r = _grasp(object_base)
    if r.get("success"):
        full = f"{msg}; {r.get('result', f'已抓取 {object_base}')}"
        print(f"[search] ✓ (MuJoCo 发现+抓取) {full}", file=sys.stderr)
        return json.dumps([full, {"_status": "success", "won": False}])
    m = r.get("result", f"已发现 {object_base} 但抓取失败")
    print(f"[search] ✗ (MuJoCo 抓取失败) {m}", file=sys.stderr)
    return json.dumps([m, {"_status": "failure"}])


def register_tools(mcp):

    @mcp.tool()
    async def search_and_grasp(object_name: str, exclude_from: str = "") -> str:
        """在所有可达位置自动搜索目标物体并抓取（ALFWorld 专用）。

        不需要指定容器编号——自动遍历所有位置、检测目标是否可取，找到即立即 take。
        比手动 raw_action("go to drawer N") 更可靠，消除实例号猜错的问题。
        若机器人手里已拿着别的物体（上一步抓错了），会先放下再搜索。

        Args:
            object_name: 目标物体基础名称（不含实例号），例如 "mug"、"cd"、"soapbar"
            exclude_from: 可选。放置"两个同类物体"的第二轮时，传入第一个的放置位置
                          （如 "shelf 1"），避免把刚放好的那个又取回来。一般留空。

        Returns:
            成功: JSON[结果描述, {"_status": "success", "won": bool}]
            失败: JSON[失败描述,  {"_status": "failure"}]

        Examples:
            search_and_grasp(object_name="mug")
            search_and_grasp(object_name="cd")
            search_and_grasp(object_name="alarmclock", exclude_from="shelf 1")
        """
        object_base = _base(object_name)
        print(f"[search] 开始搜索 '{object_base}' ...", file=sys.stderr)

        # MuJoCo(非 ALFWorld):文本搜索读不到 admissible_commands,必然失败。
        # 改走 MuJoCo 原生工作点发现 + 抓取。
        if _is_mujoco_backend():
            return _mujoco_search_and_grasp(object_base)

        # ── 以下为 ALFWorld 文本搜索原逻辑 ──
        # 抓错恢复：手里拿着非目标物体会阻塞抓取（ALFWorld 一次只能持有一个），先放下
        held = _held_object()
        if held and _base(held) != object_base:
            _drop_held(_admissible())

        def _try_take():
            """若目标当前可取就取，返回 (msg, won)，否则 None。"""
            t = _find_take(object_base, _admissible(), exclude_from)
            if t:
                r = _post("/raw", {"command": t})
                if r.get("success"):
                    return r.get("result", f"picked up {object_base}"), r.get("won", False)
            return None

        def _done(msg, won, where):
            suffix = " [TASK COMPLETE]" if won else ""
            print(f"[search] ✓ {where}找到: {msg}", file=sys.stderr)
            return json.dumps([msg + suffix, {"_status": "success", "won": won}])

        # 当前位置先试
        got = _try_take()
        if got:
            return _done(got[0], got[1], "当前位置")

        all_gos = [c for c in _admissible() if _norm(c).startswith("go to ")]
        surfaces = [g for g in all_gos if not _is_container(g)]
        containers = [g for g in all_gos if _is_container(g)]

        # 位置记忆优先：上次在这里找到过，先去试，命中则省去全量遍历
        hint = _load_location_hint(object_base)
        if hint:
            hint_norm = _norm(hint)
            for go_cmd in all_gos:
                if hint_norm in _norm(go_cmd):
                    print(f"[search] 位置记忆: {object_base} → {hint}，优先尝试", file=sys.stderr)
                    _post("/raw", {"command": go_cmd})
                    got = _try_take()
                    if got:
                        return _done(got[0], got[1], f"位置记忆({hint})")
                    _invalidate_location_memory(object_base)  # 漂移:清除过期记忆
                    print(f"[search] 位置记忆未命中，疑似漂移，已清除，fallback 全量遍历", file=sys.stderr)
                    break

        # 第一遍：开放表面优先（物体多在台面），移动到每个位置只查 take（go to 即使返回
        # "Nothing happens" 也继续查——此类游戏 receptacle 共址、go to 常失败，且 take 命令
        # 移动后会全局可见；不开容器以免破坏后续导航，也省步数。50 步预算下顺序至关重要）。
        for go_cmd in surfaces + containers:
            _post("/raw", {"command": go_cmd})
            got = _try_take()
            if got:
                return _done(got[0], got[1], "第一遍")

        # 第二遍：目标可能在关闭容器里，容器优先逐位置开容器再查。
        for go_cmd in containers + surfaces:
            _post("/raw", {"command": go_cmd})
            for oc in [c for c in _admissible() if _norm(c).startswith("open ")]:
                op = _post("/raw", {"command": oc})
                if op.get("result") == "Nothing happens.":
                    continue
                got = _try_take()
                if got:
                    return _done(got[0], got[1], "第二遍(开容器)")

        summary = f"搜索全部位置仍未找到 '{object_base}'"
        print(f"[search] ✗ {summary}", file=sys.stderr)
        return json.dumps([summary, {"_status": "failure"}])

    print("[search.py] search_and_grasp 工具已注册", file=sys.stderr)
