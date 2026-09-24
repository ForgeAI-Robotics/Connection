from common.brain_journal import emit as journal_emit


def emit(event, record, **details):
    phase = record.get('phase')
    attempts = (record.get('steps', {}).get(phase) or {}).get('attempts') or []
    attempt = attempts[-1] if attempts else {}
    return journal_emit('TASK', event=event, task_id=record.get('task_id'), inherit_task=False,
        step_id=phase, attempt_id=attempt.get('attempt_id'),
        command_id=record.get('open_command_id') or attempt.get('command_id'),
        source=record.get('entry'), component='learning' if event.startswith('reflection_') else 'runtime',
        state=record.get('state'), backend=record.get('execution_backend'),
        environment_revision=record.get('environment_revision'), release_id=record.get('release_id'),
        revision=record.get('revision'), **details)
