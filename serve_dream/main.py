"""serve_dream — DREAM(导航建图组)真机后端适配服务入口。

用法:
  python serve_dream/main.py                # 默认端口 5006,交换目录见 config.yaml
  python serve_dream/main.py --port 5006 --exchange /path/to/shared_dir
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    parser = argparse.ArgumentParser(description="serve_dream - DREAM 真机后端适配")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--exchange", type=str, default="",
                        help="覆盖交换目录(否则用 config.yaml 的 exchange.dir)")
    args = parser.parse_args()

    if args.exchange:
        import yaml
        cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        cfg.setdefault("exchange", {})["dir"] = args.exchange
        with open(cfg_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
        print(f"[serve_dream] exchange.dir -> {args.exchange}")

    from service.server import start_server
    start_server(port=args.port)


if __name__ == "__main__":
    main()
