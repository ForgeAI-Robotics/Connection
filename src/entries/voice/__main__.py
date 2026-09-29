import argparse

from entries.voice.app import create_app


def main():
    parser = argparse.ArgumentParser(description="Voice text entry")
    parser.add_argument("--brain-url")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8890)
    args = parser.parse_args()
    from shared.log_setup import attach_process_log

    attach_process_log("voice")
    create_app(args.brain_url).run(host=args.host, port=args.port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
