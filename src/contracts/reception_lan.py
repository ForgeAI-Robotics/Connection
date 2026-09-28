"""Shared reception LAN request contract; no execution or planner dependencies."""
import math
import re

from contracts.tasks import CONTRACT_VERSION, OBJECT_ID

NAVIGATION_LEGS = {
    "table2": {
        "target_id": "table_2",
        "route_phase": "",
        "leg_index": 1,
        "goal_xyt": [0.9903405869861586, 1.3761315438191244, -0.39236607751253016],
        "motion_mode": "forward_path",
    },
    "relay2": {
        "target_id": "door_1",
        "route_phase": "door_approach",
        "leg_index": 2,
        "goal_xyt": [3.733075988421528, 6.215369909530748, 2.718279944258407],
        "motion_mode": "forward_path",
    },
    "relay3": {
        "target_id": "door_1",
        "route_phase": "door_lateral_exit",
        "leg_index": 3,
        "goal_xyt": [4.185939449618811, 7.560143924693016, 2.7689146673931306],
        "motion_mode": "lateral_path_aligned",
    },
    "table1": {
        "target_id": "table_1",
        "route_phase": "table1_approach",
        "leg_index": 4,
        "goal_xyt": [3.0873798986272165, 8.279995338440145, 1.175238157458919],
        "motion_mode": "forward_path",
    },
}


class InvalidRequest(ValueError):
    pass


def validate_request(role, body):
    if not isinstance(body, dict):
        raise InvalidRequest("请求必须是 JSON 对象")
    common = {"contract_version", "task_id", "command_id"}
    fields = ({"target_id", "route_phase", "leg_index", "frame_id", "goal_xyt",
               "motion_mode", "require_final_orientation"} if role == "nav" else
              {"operation", "object_id", "target_area", "navigation_proof"})
    if set(body) != common | fields:
        raise InvalidRequest("请求字段缺失或包含未约定字段")
    if body["contract_version"] != CONTRACT_VERSION:
        raise InvalidRequest("不支持的 contract_version")
    for key in ("task_id", "command_id"):
        if not isinstance(body[key], str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", body[key]):
            raise InvalidRequest(key + " 必须为非空、可用于 URL 的标识")
    if role == "nav":
        goal = body["goal_xyt"]
        if (not isinstance(goal, list) or len(goal) != 3 or
                any(type(v) not in (int, float) or not math.isfinite(v) for v in goal)):
            raise InvalidRequest("goal_xyt 必须为三个有限数")
        if body["frame_id"] != "map" or body["require_final_orientation"] is not True:
            raise InvalidRequest("需要 map 坐标系与最终朝向")
        if type(body["leg_index"]) is not int:
            raise InvalidRequest("leg_index 必须为整数")
        expected = next((leg for leg in NAVIGATION_LEGS.values() if leg["leg_index"] == body["leg_index"]), None)
        if expected is None or any(body[k] != v for k, v in expected.items()):
            raise InvalidRequest("导航段的目标、坐标、运动模式不符合单罐接待合同")
    else:
        if body["operation"] not in ("pick", "place") or body["object_id"] != OBJECT_ID:
            raise InvalidRequest("只支持 cola_can_1 的 pick/place")
        target = "table_2" if body["operation"] == "pick" else "table_1"
        proof = body["navigation_proof"]
        if body["target_area"] != target or not isinstance(proof, dict) or set(proof) != {"dream_command_id", "target_id", "state"}:
            raise InvalidRequest("目标或导航凭证字段不符合合同")
        if (not isinstance(proof["dream_command_id"], str) or not proof["dream_command_id"]
                or proof["target_id"] != target or proof["state"] != "succeeded"):
            raise InvalidRequest("导航凭证无效")
