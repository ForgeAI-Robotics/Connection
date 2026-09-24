"""Plan only from supplied facts/capabilities. Never dispatch an action."""
import json
import re
from contracts.tasks import Rejected

class Planner:
    def __init__(self, model, prompt, *, attempts=1):
        self.model, self.prompt = model, prompt
        self.attempts = max(1, int(attempts))

    def plan(self, context):
        content = self.prompt.format(robot_name_list=list(context.robot_names),
            robot_tools_info=context.capabilities, task=context.task, scene_info=context.scene,
            experience_section=context.experiences)
        if context.rules:
            content += "\n本任务绑定的规则快照：" + json.dumps(context.rules, ensure_ascii=False)
        for _ in range(self.attempts):
            result = extract_json(self.model(self._build_messages(content, context.history)))
            if valid_plan(result, context.robot_names):
                return result
        raise Rejected("Planner 没有返回有效计划")

    def generate(self, prompt):
        return self.model([{"role": "user", "content": prompt}])

    def _build_messages(self, content: str, history: list = None) -> list:
        """构建消息列表。"""
        messages = []
        if history:
            for msg in history:
                c = msg.get("content")
                if isinstance(c, list):
                    c = "".join(p.get("text", "") if isinstance(p, dict) else getattr(p, "text", "") for p in c)
                elif c is None:
                    c = ""
                elif not isinstance(c, str):
                    c = str(c)
                messages.append({"role": msg["role"], "content": c})
        messages.append({"role": "user", "content": content})
        return messages

def extract_json(text):
    if not isinstance(text, str):
        return None
    match = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    raw = match.group(1) if match else text[text.find("{"):text.rfind("}") + 1]
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def valid_plan(value, robot_names):
    if not isinstance(value, dict) or not isinstance(value.get("subtask_list"), list):
        return False
    return all(isinstance(step, dict) and isinstance(step.get("subtask"), str)
               and step["subtask"].strip() and step.get("robot_name") in robot_names
               for step in value["subtask_list"])
