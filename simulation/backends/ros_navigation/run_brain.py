"""Start the unchanged Brain HTTP application with an isolated simulation ledger."""
import argparse
import json
import os
from pathlib import Path
import signal
import uuid


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('output', type=Path)
    parser.add_argument('--navigation-url', default='http://127.0.0.1:18072')
    parser.add_argument('--port', type=int, default=15000)
    parser.add_argument('--config', type=Path, help='Reuse project Brain settings, including configured LLM advice')
    parser.add_argument('--skill-file', type=Path, help='Select a registered reception Skill for this experiment')
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=True)
    if (root / 'execution.json').exists() or (root / 'tasks').exists():
        raise ValueError('Use a fresh experiment directory; existing task evidence is preserved')
    for key, name in [('FQ_EXECUTION_STATE', 'execution.json'), ('FQ_EXECUTION_LOCK', 'execution.lock'),
                      ('FQ_EXECUTION_BLOCK', 'execution.blocked')]:
        os.environ[key] = str(root / name)
    from shared.execution_profile import resolve
    config = {'mode': 'simulation', 'simulation_backend': 'desk', 'modules': {
        'reception': {'mode': 'simulation', 'simulation_backend': 'reception_simple'},
        'execution': {'mode': 'disabled'}, 'observation': {'mode': 'disabled'}}}
    profile = resolve(config, {'backends': {'simple_o7': {'url': args.navigation_url}}})
    profile.update(revision=uuid.uuid4().hex, experiment='office_ros_navigation_only')
    (root / 'execution.json').write_text(json.dumps(profile, indent=2))
    from brain.app import create_service
    from brain.application import BrainApplication
    from brain.api.app import create_app
    from werkzeug.serving import make_server
    settings = {'brain': {'scheduler': 'runtime', 'capture_timeline': False,
                         'reasoning': {'semantic_selection': False, 'recovery_advice': False}},
                'reception_real': {'kernel_enabled': False, 'kernel_runtime_dir': str(root / 'tasks'),
                                   'dream_inspection_enabled': False},
                'reflection': {'enabled': False}}
    if args.config:
        from dotenv import load_dotenv
        from shared.paths import workspace_root
        from shared.config import load_config
        load_dotenv(workspace_root() / '.env')
        settings = load_config(args.config)
        if not isinstance(settings, dict):
            raise ValueError('Brain configuration must be a mapping')
        settings.setdefault('brain', {}).update(scheduler='runtime', capture_timeline=False)
        settings.setdefault('reception_real', {}).update(
            kernel_enabled=False, kernel_runtime_dir=str(root / 'tasks'), dream_inspection_enabled=False)
        settings.setdefault('reflection', {})['enabled'] = False
    if args.skill_file:
        settings['reception_real']['skill_file'] = str(args.skill_file.resolve())
    from brain.packages.reception_skill import load_skill
    load_skill(settings)  # Validate before starting a task or opening the HTTP listener.
    application = BrainApplication(create_service(settings)).start()
    server = make_server('127.0.0.1', args.port, create_app(application), threaded=True)
    def stop(*_):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        application.close()


if __name__ == '__main__':
    main()
