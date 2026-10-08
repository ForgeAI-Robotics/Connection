from shared.brain_journal import emit as journal_emit


def _plan_steps(record):
    specs = record.get('phase_specs') or []
    steps = []
    for spec in specs:
        if not isinstance(spec, dict) or not spec.get('step_id'):
            continue
        item = {'step_id': spec.get('step_id'), 'kind': spec.get('kind')}
        for key in ('key', 'target_area'):
            if spec.get(key):
                item[key] = spec[key]
        if spec.get('requires_gate'):
            item['requires_gate'] = True
        if spec.get('optional'):
            item['optional'] = True
        steps.append(item)
    if steps:
        return steps
    return [step_id for step_id in (record.get('phase_order') or []) if step_id]


def _last_passed(record, phase):
    """The step whose verification just passed; the cursor has already moved past it."""
    order = record.get('phase_order') or []
    end = order.index(phase) if phase in order else len(order)
    for step_id in reversed(order[:end]):
        attempts = (record.get('steps', {}).get(step_id) or {}).get('attempts') or []
        if attempts:
            return step_id if attempts[-1].get('verdict') == 'PASS' else phase
    return phase


def emit(event, record, **details):
    # Runtime saves after moving the cursor, so name the step the event is about.
    phase = record.get('phase')
    if event == 'step_manually_skipped' and record.get('manual_skips'):
        if record.get('state') == 'running':  # a skip of the last action ends the flow
            details.setdefault('next_step_id', phase)
        phase = record['manual_skips'][-1].get('step_id') or phase
    elif event == 'step_passed':
        phase = _last_passed(record, phase)
    elif event == 'handoff_observed':
        observed = record.get('handoff_observation') or {}
        details.setdefault('handoff_reason', observed.get('reason'))
        details.setdefault('handoff_confirmed', observed.get('confirmed') is True)
        details.setdefault('skipped_steps', observed.get('skipped_steps'))
    attempts = (record.get('steps', {}).get(phase) or {}).get('attempts') or []
    attempt = attempts[-1] if attempts else {}
    abnormal = (record.get('blocked_reason') or record.get('state') in
                {'waiting_human', 'recovery_required', 'failed'} or details.get('error'))
    diagnostic = None
    if abnormal:
        # Keep absent values absent, and actual null/false values intact.
        diagnostic = {key: record[key] for key in (
            'open_command_id', 'command_unknown', 'resources_cleared') if key in record}
        progress = attempt.get('progress') or {}
        if progress.get('error'):
            diagnostic['error'] = progress['error']
        for key in ('attempt_id', 'command_id', 'outcome', 'verdict', 'evidence_ref'):
            if key in attempt:
                diagnostic[key] = attempt[key]
    advice = record.get('recovery_advice') if event in {'recovery_advised', 'task_restored'} else None
    return journal_emit('TASK', event=event, task_id=record.get('task_id'), inherit_task=False,
        step_id=phase, attempt_id=attempt.get('attempt_id'),
        command_id=record.get('open_command_id') or attempt.get('command_id'),
        blocked_reason=record.get('blocked_reason'),
        task_desc=record.get('task_desc') if event in {'task_opened', 'task_restored'} else None,
        step_count=len(record.get('phase_order') or record.get('phase_specs') or []) if event == 'plan_ready' else None,
        plan_steps=_plan_steps(record) if event == 'plan_ready' else None,
        advice_action=(record.get('recovery_advice') or {}).get('action') if event == 'recovery_advised' else None,
        diagnostic=diagnostic, recovery_advice=advice,
        source=record.get('entry'), component='learning' if event.startswith('reflection_') else 'runtime',
        state=record.get('state'), backend=record.get('execution_backend'),
        environment_revision=record.get('environment_revision'), release_id=record.get('release_id'),
        revision=record.get('revision'), **details)
