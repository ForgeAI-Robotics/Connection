"""
底盘导航模块。
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))
from robot_api.client import move_forward as _move_forward
from robot_api.client import navigate_to as _navigate_to
from robot_api.client import rotate as _rotate
from robot_api.client import get_objects as _get_objects


# ============================================================
# 记忆/部分可观测模式:逐工作点发现 + 漂移恢复
#   开关 = slaver/config.yaml use_realtime_coords(False=记忆模式)。
#   全可观测模式下这些函数不参与(navigate_to_target 走原 find_waypoint 路径)。
# ============================================================

import re as _re

# 家具关键词:目标名含其一即视为"位置固定的家具",不走逐工作点发现。
# 必须覆盖 ALFWorld 词表(shelf/cabinet/countertop)和 RoboCasa 片段,否则
# "导航到 shelf" 会被误当成物体、徒劳遍历所有工作点。
_FIXTURE_KW = ('counter', 'countertop', 'island', 'sink', 'stove', 'stovetop',
               'floor', 'fridge', 'microwave', 'oven', 'cabinet', 'cab',
               'drawer', 'shelf', 'shelves', 'table')


def _mem_mode() -> bool:
    """记忆模式 = use_realtime_coords 取反。复用 waypoint_manager 的配置读取。"""
    try:
        from waypoint_manager import _load_perception_config
        return not _load_perception_config()
    except Exception:
        return False


def _obj_base(name: str) -> str:
    return _re.sub(r'\s*\d+$', '', str(name).strip().lower())


def _is_fixture_name(target: str) -> bool:
    t = str(target).strip().lower()
    return any(k in t for k in _FIXTURE_KW)


def _scene_mem():
    """延迟导入 serve 的 scene_memory(把 serve 加到 path)。"""
    serve_path = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', '..', '..', 'serve'))
    if serve_path not in sys.path:
        sys.path.insert(0, serve_path)
    from scene import scene_memory
    return scene_memory


# ── 感知后端:geometric(坐标真值,baseline) / segmentation(相机分割图,真部分可观测) ──

def _perception_cfg(key: str, default: str) -> str:
    """读 slaver/config.yaml 的 perception.<key>,失败返回 default。"""
    try:
        import yaml
        cfg_path = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', '..', 'config.yaml'))
        with open(cfg_path, encoding='utf-8') as f:
            c = yaml.safe_load(f) or {}
        return str((c.get('perception') or {}).get(key, default)).strip().lower()
    except Exception:
        return default


def _backend_url() -> str:
    """第一个 enabled 后端的 URL(调 serve /visible_objects 用)。"""
    try:
        from robot_api.config import load_robot_api_config
        for b in load_robot_api_config().backends:
            if b.enabled:
                return b.url.rstrip('/')
    except Exception:
        pass
    return 'http://127.0.0.1:5001'


def _observe_at_waypoint(wp_name: str) -> set:
    """局部观测:机器人当前工作点能"看见"的物体基名集合(部分可观测的核心)。

    按 perception.backend 分发(都不依赖判定层,切换不影响 won 裁判):
      geometric    = 坐标真值过滤(最近工作点==当前工作点),无视野限制 = 接相机前 baseline。
      segmentation = 渲染机器人相机分割图,只认视野里可见的物体(真实视角/遮挡/距离)。
      vlm          = 渲染 RGB 发 VLM,让视觉大模型认出视野里的物体(最贴现实,不靠特权 ID)。
    """
    backend = _perception_cfg('backend', 'geometric')
    if backend == 'segmentation':
        return _segmentation_observe()
    if backend == 'vlm':
        return _vlm_observe()
    return _geometric_observe(wp_name)


def _geometric_observe(wp_name: str) -> set:
    """坐标真值过滤:物体最近工作点==当前工作点即纳入认知 = baseline 行为。"""
    observed = set()
    try:
        sm = _scene_mem()
        objs = _get_objects()
        if isinstance(objs, dict) and 'objects' in objs:
            objs = objs['objects']
        if isinstance(objs, dict):
            for name, data in objs.items():
                if not isinstance(data, dict):
                    continue
                pos = data.get('pos')
                if pos and sm.coords_to_waypoint(pos) == wp_name:
                    observed.add(_obj_base(name))
    except Exception as e:
        print(f"[base] 局部观测失败: {e}", file=sys.stderr)
    return observed


def _segmentation_observe() -> set:
    """渲染机器人相机的 segmentation,返回视野里可见的物体基名集。

    机器人已 nav 到工作点(相机朝向该处 fixture),看到的就是这个工作点该有的物体——
    但受真实视角/遮挡/距离限制(看不全、太远太小看不到)=相机带来的部分可观测。
    渲染失败视作"这一处没观测到"(返回空),不回退几何,保持感知后端纯粹。
    """
    import urllib.request, urllib.parse
    cam = _perception_cfg('camera', 'head_cam')
    try:
        # scan=1:到工作点后转头扫描(绕开 nav 朝向不到位致 head_cam 看不到→误判漂移)
        url = _backend_url() + '/visible_objects?camera=' + urllib.parse.quote(cam) + '&scan=1'
        with urllib.request.urlopen(url, timeout=30) as r:
            resp = json.loads(r.read().decode())
        seen = set(_obj_base(o) for o in (resp.get('visible') or []))
        print(f"[base] 📷 segmentation({cam}) 看到: {sorted(seen)}", file=sys.stderr)
        return seen
    except Exception as e:
        print(f"[base] segmentation 观测失败: {e}(视作未观测到)", file=sys.stderr)
        return set()


# perception 相机名 → serve 相机名(和 serve /visible_objects 里的映射保持一致)
_PERC_CAM_MAP = {
    "head_cam": "robot0_frontview",
    "right_arm_cam": "robot0_eye_in_hand",
    "left_arm_cam": "robot0_agentview_center",
}

# VLM 认物别名:模型常按 mesh 真实叫法或近义词回答(bread 的 mesh 其实是 baguette 法棍,
# GLM/mimo 都只说 baguette 从不说 bread → 漏检)。把这些叫法映回我们的物体基名。
# 保守收录,避免误报;词边界匹配已挡住 dish→dishwasher 之类。key 是物体基名,value 是额外接受的叫法。
_VLM_ALIASES = {
    "bread": ["baguette", "french bread", "loaf"],
    # ketchup 是小瓶,VLM 常叫 bottle/condiment/sauce(我们物体集里没别的瓶子,不会误伤)
    "ketchup": ["tomato sauce", "ketchup bottle", "sauce bottle", "bottle", "condiment", "sauce"],
    "cheese": ["cheese block", "cheese wedge"],
    "mug": ["coffee mug"],
    "cup": ["glass cup"],
    "pot": ["cooking pot", "saucepan"],
    "plate": ["dish"],
}


def _vlm_candidate_objects() -> list:
    """问 VLM"看到哪些"的候选清单 = serve 当前所有物体名(机器人知道场景有哪些物体,靠视觉认哪些可见)。"""
    import urllib.request
    try:
        with urllib.request.urlopen(_backend_url() + '/objects', timeout=15) as r:
            resp = json.loads(r.read().decode())
        objs = resp.get('objects', resp)
        return sorted([n for n, v in objs.items() if isinstance(v, dict)])
    except Exception:
        return []


def _vlm_observe() -> set:
    """渲染机器人相机 RGB 发 VLM,让视觉大模型认出视野里有哪些已知物体(比 segmentation 更贴现实:
    看 RGB 图、不靠 MuJoCo 特权 segmentation ID)。返回可见物体基名集。

    VLM 配置(model/api_base)读 slaver/config.yaml 的 camera.vlm;key 用 VLM_API_KEY(见 .env,
    与 master 规划用的 deepseek CLOUD_API_KEY 分开)。渲染/识别失败视作"未观测到"(返回空)。
    """
    import urllib.request
    import re as _re
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.normpath(os.path.join(os.path.dirname(__file__), '..', '..', '..', '.env')))
    except Exception:
        pass
    cam = _perception_cfg('camera', 'head_cam')
    cams = list(_PERC_CAM_MAP.values()) if cam == 'all' else [_PERC_CAM_MAP.get(cam, 'robot0_frontview')]
    candidates = _vlm_candidate_objects()
    if not candidates:
        print("[base] vlm 观测:拿不到候选物体(视作未观测到)", file=sys.stderr)
        return set()
    # 渲染 RGB。默认转头扫描(perception.scan):serve 非物理转底盘 yaw 到多个角度各渲一张,
    # 多帧一次性发 VLM = 一次调用覆盖多视角,提高目标召回(单视角常漏认目标致假阴性→搜索失败)。
    images = {}
    scan = _perception_cfg('scan', True)
    if scan:
        scan_cam = _PERC_CAM_MAP.get('head_cam', 'robot0_frontview')  # 转头扫描用朝工作区的机身前视相机
        angles = _perception_cfg('scan_angles', [0, 90, 180, 270])
        try:
            req = urllib.request.Request(
                _backend_url() + '/scan_rgb',
                data=json.dumps({'camera_name': scan_cam, 'width': 640, 'height': 480,
                                 'angles': angles}).encode(),
                headers={'Content-Type': 'application/json'}, method='POST')
            with urllib.request.urlopen(req, timeout=90) as r:
                resp = json.loads(r.read().decode())
            for i, fr in enumerate(resp.get('frames') or []):
                images[f'view{i}'] = fr
        except Exception as e:
            print(f"[base] vlm 扫描截图失败: {e}(回退单帧)", file=sys.stderr)
    if not images:
        # 非扫描 / 扫描失败:退回当前朝向单帧(cam=='all' 用机身多相机)
        for c in cams:
            try:
                req = urllib.request.Request(
                    _backend_url() + '/screenshot',
                    data=json.dumps({'camera_name': c, 'width': 640, 'height': 480}).encode(),
                    headers={'Content-Type': 'application/json'}, method='POST')
                with urllib.request.urlopen(req, timeout=30) as r:
                    resp = json.loads(r.read().decode())
                if resp.get('success') and resp.get('image'):
                    images[c] = resp['image']
            except Exception:
                pass
    if not images:
        print("[base] vlm 观测:截图失败(视作未观测到)", file=sys.stderr)
        return set()
    # 调 VLM 识别可见物体
    try:
        import yaml
        from openai import OpenAI
        cfg_path = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', '..', 'config.yaml'))
        with open(cfg_path, encoding='utf-8') as f:
            vcfg = ((yaml.safe_load(f) or {}).get('camera') or {}).get('vlm') or {}
        model = vcfg.get('model', 'qwen-vl-max')
        api_base = vcfg.get('api_base', 'https://dashscope-intl.aliyuncs.com/compatible-mode/v1')
        max_tokens = int(vcfg.get('max_tokens', 800))
        extra_body = vcfg.get('extra_body') or {}   # GLM 关思考 {thinking:{type:disabled}};原样透传
        key = os.environ.get('VLM_API_KEY') or os.environ.get('CLOUD_API_KEY', '')
        # 用英文 prompt(mimo 对中文常思考到 token 耗尽返回空);清单里选可见项,逗号分隔
        prompt = (
            "These are photos from a robot camera looking around a kitchen from several angles. "
            "Look carefully across ALL the photos, including SMALL items like bottles, sponges and produce. "
            "From the candidate object list below, reply with ONLY the object names you can actually SEE "
            "(comma-separated, use the exact english names; if you see none, reply 'none').\n"
            "Candidates: " + ", ".join(candidates)
        )
        content = [{"type": "text", "text": prompt}]
        for _c, b64 in images.items():
            content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
        client = OpenAI(api_key=key, base_url=api_base)
        # 推理模型(mimo/GLM-4.6V)要么关思考(extra_body.thinking.disabled,~5s),要么留足 max_tokens。
        create_kw = dict(model=model, messages=[{"role": "user", "content": content}],
                         max_tokens=max_tokens, temperature=0)
        if extra_body:
            create_kw['extra_body'] = extra_body
        # 免费 Flash 档偶发 429 限流("访问量过大"):退避重试,别让限流被当成"没看到"→ 误判漂移。
        import time as _time
        resp = None
        for _try in range(3):
            try:
                resp = client.chat.completions.create(**create_kw)
                break
            except Exception as _e:
                em = str(_e)
                if ('429' in em or '1305' in em) and _try < 2:
                    print(f"[base] vlm 限流,{_try + 1}/3 退避重试...", file=sys.stderr)
                    _time.sleep(5)
                    continue
                raise
        raw = (resp.choices[0].message.content or "").lower()
        # 用词边界匹配候选名(+别名),避免 pot 命中 teapot 之类的误报;别名解决 bread=baguette 这种漏检
        seen = set()
        for cand in candidates:
            base = _obj_base(cand)
            terms = [cand.lower()] + _VLM_ALIASES.get(base.lower(), [])
            if any(_re.search(r'(?<![a-z])' + _re.escape(t) + r'(?![a-z])', raw) for t in terms):
                seen.add(base)
        print(f"[base] 👁 vlm({cam}) 看到: {sorted(seen)}  (VLM原始: {raw[:80]!r})", file=sys.stderr)
        return seen
    except Exception as e:
        print(f"[base] vlm 观测失败: {e}(视作未观测到)", file=sys.stderr)
        return set()


def _list_containers() -> list:
    """从 serve 取"可藏物容器"列表 [{name, pos}](有门 joint 的高柜)。失败返回空。"""
    import urllib.request
    try:
        with urllib.request.urlopen(_backend_url() + '/containers', timeout=15) as r:
            resp = json.loads(r.read().decode())
        return resp.get('containers') or []
    except Exception as e:
        print(f"[base] 取容器列表失败: {e}", file=sys.stderr)
        return []


def _open_container(name: str) -> dict:
    """开一个容器(转门 + 把内容物取到机器人脚边)。返回 serve 结果 dict(含 lifted)。

    serve 每次 open 计一步(持续模式)= 逐柜翻找的搜索成本。检测"柜里有没有目标"用返回的
    lifted 列表(=开柜后看见的内容物),而不是 head_cam——物体取到脚边 0.3m 太近/太低,
    正落在 head_cam 视锥盲区,转头扫描看不到;而"开柜看里面有什么"本就该由开柜这个动作回答。
    """
    import urllib.request
    try:
        req = urllib.request.Request(
            _backend_url() + '/open_container',
            data=json.dumps({'container': name}).encode(),
            headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"[base] 开容器 {name} 失败: {e}", file=sys.stderr)
        return {'success': False, 'result': str(e)}


def _lifted_bases(open_resp: dict) -> set:
    """从 open_container 返回里取"开柜后露出的内容物"基名集(=柜里有什么)。"""
    return set(_obj_base(o) for o in (open_resp.get('lifted') or []))


def _discover_object_waypoint(obj_name: str):
    """搜索目标物体,发现后更新 belief。返回 (success, msg)。

    搜索顺序(每步真实驱动 serve,首次代价高、命中代价低 → memory_speedup 学习曲线):
      ① belief 命中容器 → 直达 open 那个柜确认(便宜,1 次 open)
      ② belief 命中开放工作点 → 去那儿确认(顺带漂移检测)
      ③ 逐开放工作点遍历(转头扫描,物体在台面则命中)
      ④ 表面找不到 → 逐柜 open 翻找(物体藏在容器里,每 open 一步=搜索成本)
    物体在开放表面时 ③ 一眼扫到(便宜);藏进柜时要走完 ③ 再 ④ 逐柜开(贵)→ speedup<1。
    """
    base = _obj_base(obj_name)

    # 全可观测 / 无 belief 后端(如 motrixsim/3dgs:serve /scene_state=503)→ 降级(方案 Y):
    # 不走 belief/逐工作点遍历/漂移(那套依赖 scene_memory);直接用 /objects 真值坐标定位目标、
    # 导航过去交抓取。开关 = use_realtime_coords:true(_mem_mode()=False)。robocasa 记忆模式不受影响。
    if not _mem_mode():
        try:
            objs = _get_objects()
            if isinstance(objs, dict) and 'objects' in objs:
                objs = objs['objects']
            data = (objs.get(obj_name) or objs.get(base)) if isinstance(objs, dict) else None
            if isinstance(data, dict) and data.get('pos'):
                pos = data['pos']
                _navigate_to(pos[:2])  # 导航到目标真值坐标附近(不依赖工作点/belief)
                return True, (f"(全可观测)按 /objects 真值坐标定位 {obj_name} @ "
                              f"{[round(float(v), 2) for v in pos[:2]]},已导航交抓取")
            return False, f"(全可观测){obj_name} 不在 /objects 里"
        except Exception as e:
            return False, f"(全可观测)真值定位失败: {e}"

    try:
        sm = _scene_mem()
        from waypoint_manager import load_waypoints
        waypoints = load_waypoints()
    except Exception as e:
        return False, f"发现机制初始化失败: {e}"

    try:
        belief_loc = sm.get_object_location(obj_name)
    except Exception:
        belief_loc = None
    orig_belief = belief_loc  # 记住最初 belief(reset 时上帝真值播种的已知位置),供 VLM 全程漏认时兜底

    # 已在手中:物体随机器人移动,无需也无法"导航过去",直接成功
    if belief_loc == 'robot_hand':
        return True, f"{obj_name} 已在机器人手中"

    containers = _list_containers()
    container_names = {c['name'] for c in containers}

    # ① belief 命中容器 → 直达开那个柜确认(便宜,1 步)。扑空则清记忆转全场重搜。
    if belief_loc and belief_loc in container_names:
        if base in _lifted_bases(_open_container(belief_loc)):
            return True, f"位置记忆命中(容器):直达开 {belief_loc} 找到 {obj_name}"
        print(f"[base] ⚠ 漂移检测:belief 说 {obj_name}@{belief_loc}(容器)实际不在,清记忆重搜",
              file=sys.stderr)
        try:
            sm.move_object(obj_name, 'unknown')
        except Exception:
            pass
        belief_loc = 'unknown'

    # ② belief 命中开放工作点 → 先去那儿确认(顺带做漂移检测)
    drift = False
    ordered = list(waypoints)
    if belief_loc and belief_loc not in ('unknown', 'robot_hand') and belief_loc not in container_names:
        wp = next((w for w in waypoints if w['name'] == belief_loc), None)
        if wp:
            _navigate_to(wp['pos'][:2], yaw=wp.get('yaw_deg'))
            if base in _observe_at_waypoint(belief_loc):
                return True, f"位置记忆命中:{obj_name} 在 {belief_loc}"
            # belief 指向的地方扑空 → 漂移:清记忆,把该点排到最后再全场重搜
            drift = True
            print(f"[base] ⚠ 漂移检测:belief 说 {obj_name}@{belief_loc} 实际不在,清记忆重搜",
                  file=sys.stderr)
            try:
                sm.move_object(obj_name, 'unknown')
            except Exception:
                pass
            ordered = ([w for w in waypoints if w['name'] != belief_loc] +
                       [w for w in waypoints if w['name'] == belief_loc])

    # ③ 逐开放工作点遍历发现(转头扫描,物体在台面则命中)
    for wp in ordered:
        _navigate_to(wp['pos'][:2], yaw=wp.get('yaw_deg'))
        if base in _observe_at_waypoint(wp['name']):
            try:
                sm.move_object(obj_name, wp['name'])
            except Exception:
                pass
            tag = "(漂移恢复)" if drift else ""
            return True, f"发现 {obj_name} 在 {wp['name']}{tag},已更新位置记忆"

    # ④ 开放表面找不到 → 物体可能藏在容器里:逐柜 open 翻找(每 open 一步=搜索成本)。
    #    用开柜返回的 lifted(柜里内容物)判定,不靠 head_cam(脚边太近是盲区)。
    for c in containers:
        if base in _lifted_bases(_open_container(c['name'])):
            try:
                sm.move_object(obj_name, c['name'])
            except Exception:
                pass
            tag = "(漂移恢复)" if drift else ""
            return True, f"逐柜翻找:开 {c['name']} 发现 {obj_name}{tag},已更新位置记忆(容器)"

    # ⑤ VLM 全程没认出(小物体/渲染难认)→ 回退信任位置,别让弱感知致整任务失败。
    #    优先用最初 belief(reset 上帝真值播种);若 belief 已丢(上一轮搜索失败被漂移清成 unknown,
    #    兜底就失灵了——正是 ketchup 的坑),退一步用 god's-eye 真值坐标的最近工作点兜底
    #    (和 belief 同源=上帝真值,只是这次直接查实时坐标)。抓到后顺便把 belief 修回,下轮自愈。
    fallback_wp = None
    if orig_belief and orig_belief not in ('unknown', 'robot_hand') and orig_belief not in container_names:
        fallback_wp = orig_belief
    else:
        try:
            objs = _get_objects()
            if isinstance(objs, dict) and 'objects' in objs:
                objs = objs['objects']
            data = (objs.get(obj_name) or objs.get(base)) if isinstance(objs, dict) else None
            if isinstance(data, dict) and data.get('pos'):
                fallback_wp = sm.coords_to_waypoint(data['pos'])
        except Exception:
            pass
    if fallback_wp and fallback_wp not in ('unknown', 'robot_hand'):
        wp = next((w for w in waypoints if w['name'] == fallback_wp), None)
        if wp:
            _navigate_to(wp['pos'][:2], yaw=wp.get('yaw_deg'))
            try:
                sm.move_object(obj_name, fallback_wp)  # belief 自愈:下轮不再是 unknown
            except Exception:
                pass
            src = "belief" if fallback_wp == orig_belief else "真值坐标"
            print(f"[base] ⚠ VLM 全程未识别 '{obj_name}',回退信任位置({src}:{fallback_wp}) → 导航交抓取",
                  file=sys.stderr)
            return True, f"VLM 未识别但回退信任位置({src}):{obj_name} 在 {fallback_wp}(已导航,交抓取)"
    return False, f"全场逐工作点 + 逐柜翻找仍未发现 '{obj_name}'"


def _is_alfworld() -> bool:
    """True when ALFWorld is the active required backend."""
    try:
        from robot_api.config import load_robot_api_config
        cfg = load_robot_api_config()
        for b in cfg.backends:
            if b.name == "alfworld" and b.enabled and getattr(b, "required", False):
                return True
    except Exception:
        pass
    return False


def _is_dream_backend() -> bool:
    """True only when DREAM is the explicitly selected active backend."""
    try:
        from robot_api.config import load_robot_api_config
        return (load_robot_api_config().active_backend or "").lower() == "dream"
    except Exception:
        return False


def _dream_reviewed_target(name: str):
    """Resolve table1/table2 aliases from the reviewed navigation SOP."""
    try:
        root = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
        dream_dir = os.path.join(root, 'serve_dream')
        if dream_dir not in sys.path:
            sys.path.insert(0, dream_dir)
        import reviewed_targets
        spec = reviewed_targets.resolve_reviewed_route_target(name)
        context_path = os.path.join(dream_dir, 'runtime', 'current_agent_task.json')
        context = {}
        try:
            with open(context_path, encoding='utf-8') as handle:
                context = json.load(handle) or {}
        except (OSError, ValueError, TypeError):
            pass
        task_id = str(context.get('task_id') or '').strip() or None
        leg_index = int(spec.get('leg_index', 1)) if spec else 1
        return (
            reviewed_targets.as_robot_api_target(
                spec, task_id=task_id, leg_index=leg_index
            ) if spec else None,
            spec,
        )
    except Exception as exc:
        print(f"[base] DREAM 审核目标读取失败: {exc}", file=sys.stderr)
        return None, None

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from waypoint_manager import find_waypoint


# ============================================================
# MCP 工具注册
# ============================================================

def register_tools(mcp):

    @mcp.tool()
    async def navigate_to_target(target: str) -> str:
        """导航到目标位置。

        将机器人底盘导航到场景中的指定位置。
        优先使用物体名称（如 "apple"、"counter"），系统会自动找到最佳工作点和朝向。
        仅在没有对应物体名时才传坐标。

        Args:
            target: 物体名称（推荐）或坐标字符串

        Returns:
            包含结果消息和状态更新的 JSON 字符串。

        Examples:
            navigate_to_target(target="apple")
            navigate_to_target(target="counter")
        """
        print(f"[base] 导航请求: '{target}'", file=sys.stderr)

        # 先尝试解析为坐标
        try:
            cleaned = target.strip().strip("()")
            parts = [float(x.strip()) for x in cleaned.split(",")]
            if len(parts) >= 2:
                x, y = parts[0], parts[1]
                yaw_deg = parts[2] if len(parts) > 2 else None
                return await _do_navigate(x, y, yaw_deg)
        except ValueError:
            pass

        # DREAM 真机:自然语言 table1/table2/审核门阶段必须先解析成审核过的 map 点、弧度朝向和
        # 运动方式；绝不落入 MuJoCo 工作点或把符号名称直接发给 8001。
        if _is_dream_backend():
            nav_target, spec = _dream_reviewed_target(target)
            if nav_target:
                result = _navigate_to(nav_target)
                goal = spec["goal_xyt"]
                contract = (f"target_id={spec['target_id']}, route_phase="
                            f"{spec.get('route_phase', '')}, goal_xyt="
                            f"[{goal[0]:.6f}, {goal[1]:.6f}, {goal[2]:.6f}] rad, "
                            f"motion_mode={spec.get('motion_mode', 'forward_path')}")
                if result.get("success"):
                    return json.dumps([
                        f"DREAM 导航成功；已向导航组发送 {contract}",
                        {"_status": "success", "command_id": result.get("command_id"),
                         "state": result.get("state"), "navigation_contract": nav_target},
                    ], ensure_ascii=False)
                return json.dumps([
                    f"DREAM 导航失败；本次请求参数为 {contract}；{result.get('result', '')}",
                    {"_status": "failure", "command_id": result.get("command_id"),
                     "state": result.get("state"), "navigation_contract": nav_target},
                ], ensure_ascii=False)
            return json.dumps([
                f"DREAM 未找到 '{target}' 的审核目标；请使用 table1/table2 或 door_approach/door_lateral_exit",
                {"_status": "failure"},
            ], ensure_ascii=False)

        # 记忆模式 + MuJoCo 后端 + 物体目标(非家具):走"逐工作点发现"——belief 已知则
        # 直达确认,未知/扑空则遍历搜索并更新位置记忆。家具(counter/sink/...)位置固定不走发现。
        # 必须排除 ALFWorld:那是符号后端,导航(含 shelf 等 receptacle)透传名称,不用 MuJoCo 工作点。
        if _mem_mode() and not _is_alfworld() and not _is_fixture_name(target):
            ok, msg = _discover_object_waypoint(target)
            print(f"[base] {'✓' if ok else '✗'} 发现导航 '{target}': {msg}", file=sys.stderr)
            return json.dumps([msg, {"_status": "success" if ok else "failure"}])

        # 是物体名称，优先找工作点坐标(MuJoCo)，找不到则透传名称(ALFWorld等符号后端)。
        # ALFWorld 是纯符号后端,绝不能发坐标:必须跳过 MuJoCo 工作点查找直接透传名称。
        # 否则 "countertop 1" 会模糊匹配到 MuJoCo 的 counter 工作点 → 发坐标 → ALFWorld
        # /nav 报 "coordinates not supported"(这正是 shelf 能成功、countertop 失败的原因:
        # shelf 无对应 MuJoCo 工作点会抛异常透传,countertop 却匹配到 counter 工作点)。
        if not _is_alfworld():
            try:
                wp = find_waypoint(target)
                return await _do_navigate(wp['x'], wp['y'], wp['yaw_deg'])
            except Exception:
                pass

        result = _navigate_to(target)
        if result.get("success"):
            response = result.get("result", f"已导航到 '{target}'")
            # In ALFWorld (text backend), navigation is not terminal — the slaver should
            # continue to examine / take after arriving. Use "navigated" to keep the loop alive.
            nav_status = "navigated" if _is_alfworld() else "success"
            return json.dumps([response, {"_status": nav_status}])
        else:
            msg = result.get("result", f"导航失败：无法到达 '{target}'")
            print(f"[base] {msg}", file=sys.stderr)
            return json.dumps([msg, {"_status": "failure"}])

    async def _do_navigate(x, y, yaw_deg):
        if x is None or y is None:
            # 坐标没解析出来就别发 /nav(会 400),直接如实失败,交 Master 决策
            return json.dumps([f"导航目标坐标无法解析(x={x}, y={y}),无法导航", {"_status": "failure"}])
        result = _navigate_to([x, y], yaw=yaw_deg)

        if result.get("success"):
            pos = result.get("pos", [0, 0, 0])
            yaw = result.get("yaw", 0)
            response = f"导航成功，当前位置: [{pos[0]:.2f}, {pos[1]:.2f}, {pos[2]:.2f}], 朝向: {yaw:.1f}°"
            return json.dumps([response, {
                "_status": "success",
                "coordinates": pos,
            }])
        else:
            msg = result.get("result", f"导航到 ({x:.2f}, {y:.2f}) 失败，请重试。")
            return json.dumps([msg, {"_status": "failure"}])

    @mcp.tool()
    async def move_forward(duration: float = 1.0, speed: float = 0.5) -> str:
        """控制机器人以指定速度持续前进/后退一段时间。

        适用于"往前走2秒"、"后退1秒"等基于时间的运动指令。
        正 speed 为前进，负 speed 为后退。

        Args:
            duration: 持续时间（秒），默认 1.0
            speed:    前进速度 [-1, 1]，正值前进，负值后退，默认 0.5

        Returns:
            包含结果消息和最终位置的 JSON 字符串。

        Examples:
            move_forward(duration=2.0)         # 前进2秒
            move_forward(duration=1.0, speed=-0.3)  # 后退1秒
        """
        print(f"[base] 持续移动请求: speed={speed}, duration={duration}s", file=sys.stderr)
        result = _move_forward(duration=duration, speed=speed)

        if result.get("success"):
            pos = result.get("pos", [0, 0, 0])
            yaw = result.get("yaw", 0)
            response = f"移动完成（{duration}秒），当前位置: [{pos[0]:.2f}, {pos[1]:.2f}, {pos[2]:.2f}], 朝向: {yaw:.1f}°"
            return json.dumps([response, {
                "_status": "success",
                "coordinates": pos,
            }])
        else:
            msg = result.get("result", "移动失败，请重试。")
            return json.dumps([msg, {"_status": "failure"}])

    @mcp.tool()
    async def rotate(duration: float = 1.0, speed: float = 0.5) -> str:
        """控制机器人原地旋转一段时间。

        适用于"左转2秒"、"右转1秒"等基于时间的旋转指令。
        正 speed 为左转（逆时针），负 speed 为右转（顺时针）。

        Args:
            duration: 持续时间（秒），默认 1.0
            speed:    旋转速度 [-1, 1]，正值左转，负值右转，默认 0.5

        Returns:
            包含结果消息和最终朝向的 JSON 字符串。

        Examples:
            rotate(duration=2.0)          # 左转2秒
            rotate(duration=1.0, speed=-0.5)  # 右转1秒
        """
        print(f"[base] 旋转请求: speed={speed}, duration={duration}s", file=sys.stderr)
        direction = "left" if speed >= 0 else "right"
        result = _rotate(direction=direction, duration=duration, speed=abs(speed))

        if result.get("success"):
            pos = result.get("pos", [0, 0, 0])
            yaw = result.get("yaw", 0)
            response = f"旋转完成（{duration}秒），当前朝向: {yaw:.1f}°，位置: [{pos[0]:.2f}, {pos[1]:.2f}]"
            return json.dumps([response, {
                "_status": "success",
                "yaw": yaw,
            }])
        else:
            msg = result.get("result", "旋转失败，请重试。")
            return json.dumps([msg, {"_status": "failure"}])

    print("[base.py] 底盘控制模块已注册 (robot_api)", file=sys.stderr)
