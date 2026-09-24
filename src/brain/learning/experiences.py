"""Existing human-reviewed experience files, independent of the legacy scheduler."""
import os
import threading
from pathlib import Path
from datetime import datetime
from brain.kernel.planner import extract_json
from brain.packages import prompts


def skill_name(task):
    text = str(task).lower()
    for skill, words in [('grasp', ('抓', 'grasp', '拿', '捡', '取')),
                         ('place', ('放', 'place')), ('navigate', ('导航', 'navigate', '去', '移动到'))]:
        if any(word in text for word in words):
            return skill
    return 'multi_step'


class Experiences:
    def __init__(self, root, model, exploration_rate=.8):
        self.root, self.model = Path(root), model
        self.exploration_rate = float(os.environ.get('EXPLORATION_RATE', exploration_rate))
        self._lock = threading.RLock()

    def load(self, task):
        name = skill_name(task)
        parts = []
        for skill in dict.fromkeys((name, 'multi_step')):
            path = self.root / 'skills' / (skill + '.md')
            if path.is_file() and path.read_text().strip():
                parts.append(f'### {skill} 专项经验\n' + path.read_text().strip())
        if not parts:
            return ''
        pct = int(self.exploration_rate * 100)
        return f'\n\n## 过往经验（请参考）：\n> 参考策略：{100-pct}% 借鉴以下经验，{pct}% 自由探索新方案。\n' + '\n\n'.join(parts)

    def read(self):
        with self._lock:
            paths = [self.root / 'experiences.md'] + [self.root / 'skills' / (n + '.md')
                for n in ('navigate', 'grasp', 'place', 'multi_step')]
            return '\n\n'.join(p.read_text().strip() for p in paths if p.is_file())

    def save(self, note, kind, record, *, classify=False):
        task = record.get('task_desc') or '未知任务'
        name = skill_name(task)
        if classify:
            template = prompts.EXPERIENCE_CLASSIFY_SUCCESS if kind == 'positive' else prompts.EXPERIENCE_CLASSIFY
            prompt = template.format(task_desc=task, raw_input=note)
        else:
            steps = record.get('steps') or {}
            summary = '\n'.join(f'{step}: {bucket.get("attempts", [])}' for step, bucket in steps.items())
            prompt = prompts.EXPERIENCE_GENERATION.format(task=task, subtask_summary=summary or '无子任务记录',
                feedback_type='正向（方案有效）' if kind == 'positive' else '负向（方案有问题）',
                user_note_section=f'用户备注：{note}' if note else '')
        try:
            result = self.model.generate(prompt)
            if classify:
                parsed = extract_json(result) or {}
                name = parsed.get('skill') if parsed.get('skill') in {'navigate','grasp','place','multi_step'} else name
                result = parsed.get('tip' if kind == 'positive' else 'rule') or note
        except Exception:
            result = note
        result = result or ('此方案有效，可复用' if kind == 'positive' else '此方案有问题，需避免')
        section = '## 正向经验' if kind == 'positive' else '## 避免规则'
        with self._lock:
            path = self.root / 'skills' / (name + '.md')
            path.parent.mkdir(parents=True, exist_ok=True)
            content = path.read_text() if path.is_file() else f'# {name} Skill 经验库\n\n## 正向经验\n\n## 避免规则\n'
            if section not in content:
                content += '\n' + section + '\n'
            pos = content.index(section) + len(section)
            content = content[:pos] + f'\n### {datetime.now():%Y-%m-%d %H:%M} {task}\n- 任务：{task}\n- 教训：{result}\n' + content[pos:]
            path.write_text(content)
        return {'success': True, 'skill': name, 'message': f'经验已保存到 {name}.md',
                'experience': result, 'tip' if kind == 'positive' else 'rule': result}
