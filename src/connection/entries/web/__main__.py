import argparse
from connection.entries.web.app import create_app


def main():
    parser = argparse.ArgumentParser(description='Independent Connection browser entry')
    parser.add_argument('--brain-url')
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8888)
    args = parser.parse_args()
    from common.log_setup import attach_process_log
    attach_process_log('deploy')
    create_app(args.brain_url).run(host=args.host, port=args.port, threaded=True, use_reloader=False)


if __name__ == '__main__':
    main()
