"""Explicit, request-local diagnostic identity; never a task-state authority."""
from contextlib import contextmanager
from contextvars import ContextVar
from concurrent.futures import CancelledError
import threading
import traceback


_FIELDS = frozenset({'task_id', 'step_id', 'attempt_id', 'command_id', 'request_id',
                     'trace_id', 'call_id', 'parent_call_id', 'control_id', 'job_id'})
_CONTEXT = ContextVar('brain_diagnostic_context', default=None)
_REPORT_LOCK = threading.Lock()


def context_fields():
    """Return a detached scalar snapshot, safe to carry to another worker."""
    return dict(_CONTEXT.get() or {})


@contextmanager
def diagnostic_context(*, replace=False, **fields):
    """Override identity; replace=True restores a captured context without leakage."""
    current = {} if replace else context_fields()
    for key, value in fields.items():
        if key not in _FIELDS:
            continue
        if value is None or value == '':
            current.pop(key, None)
        elif isinstance(value, str) and len(value) <= 256:
            current[key] = value
    token = _CONTEXT.set(current)
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def task_fields(record):
    """Bind only the supplied Runtime snapshot, never whatever task is now current."""
    phase = record.get('phase')
    attempts = ((record.get('steps') or {}).get(phase) or {}).get('attempts') or []
    attempt = attempts[-1] if attempts else {}
    return {'task_id': record.get('task_id'), 'step_id': phase,
            'attempt_id': attempt.get('attempt_id'),
            'command_id': record.get('open_command_id') or attempt.get('command_id')}


def diagnostic_event(kind, event, **fields):
    """A diagnostic failure must not replace a business outcome."""
    try:
        from shared.brain_journal import emit
        payload = context_fields()
        payload.update(fields)
        return emit(kind, event=event, inherit_task=False, **payload)
    except Exception:
        # The journal owns write-error accounting and fallback output. This guard
        # also protects early startup and third-party patched diagnostic sinks.
        return None


def report_exception(exc, *, component, **fields):
    """Capture an unexpected exception once even if its Future is never awaited."""
    from contracts.tasks import Rejected
    if isinstance(exc, (Rejected, CancelledError)):
        return False
    try:
        with _REPORT_LOCK:
            if getattr(exc, '_brain_diagnostic_reported', False):
                return False
            try:
                exc._brain_diagnostic_reported = True
            except Exception:
                pass
        diagnostic_event('DIAGNOSTIC', 'exception', component=component, level='ERROR',
                         exception_type=type(exc).__name__, message=str(exc),
                         exception=''.join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
                         **fields)
    except Exception:
        diagnostic_event('DIAGNOSTIC', 'exception', component=component, level='ERROR',
                         exception_type=type(exc).__name__, message='Exception detail unavailable', **fields)
    return True
