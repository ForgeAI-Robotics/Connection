"""python -m brain --config /path/to/config/brain.yaml"""
import argparse
import signal
from pathlib import Path
import yaml
from shared.paths import workspace_root


def main():
    parser = argparse.ArgumentParser(description='Connection brain service')
    parser.add_argument('--config', type=Path, default=workspace_root() / 'config/brain.yaml')
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=5000)
    args = parser.parse_args()
    from dotenv import load_dotenv
    load_dotenv(workspace_root() / '.env')
    from shared.networks import apply_proxy_bypass, apply_to_brain_config
    apply_proxy_bypass()
    from shared.log_setup import attach_process_log
    attach_process_log('master')
    from brain.app import create_service
    from brain.application import BrainApplication
    from brain.api.app import create_app
    from shared.config import load_config
    config = apply_to_brain_config(load_config(args.config))
    if not isinstance(config, dict):
        raise ValueError('大脑配置必须是 YAML 映射')
    if (config.get('brain') or {}).get('scheduler', 'runtime') != 'runtime':
        raise ValueError('新服务只使用 Runtime；旧调度回退须按停机回退记录执行')
    application = BrainApplication(create_service(config)).start()
    def stop(signum, frame):
        raise SystemExit(0)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop)
    from werkzeug.serving import make_server
    from shared.log_setup import print_beijing
    app = create_app(application)
    server = make_server(args.host, args.port, app, threaded=True)
    print_beijing('INFO', 'master', f'大脑服务已监听（listen）\n  http://{args.host}:{server.port}')
    try:
        server.serve_forever()
    finally:
        server.server_close()
        application.close()


if __name__ == '__main__':
    main()
