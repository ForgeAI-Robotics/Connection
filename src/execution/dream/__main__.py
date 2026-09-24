"""serve_dream — DREAM(导航建图组)真机后端适配服务入口。

用法:
  python -m execution.dream                # 默认端口 5006,交换目录见 config.yaml
  python -m execution.dream --port 5006 --exchange /path/to/shared_dir
"""

from shared.paths import workspace_root
import argparse
import os
import sys



def main():
    from shared.log_setup import attach_process_log

    attach_process_log("serve_dream")
    parser = argparse.ArgumentParser(description="serve_dream - DREAM 真机后端适配")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--exchange", type=str, default="",
                        help="覆盖交换目录(否则用 config.yaml 的 exchange.dir)")
    args = parser.parse_args()

    if args.exchange:
        import yaml
        cfg_path = str(workspace_root() / "config/dream.yaml")
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        cfg.setdefault("exchange", {})["dir"] = args.exchange
        with open(cfg_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
        print(f"[serve_dream] exchange.dir -> {args.exchange}")

    from execution.dream.service.server import start_server
    start_server(port=args.port)


if __name__ == "__main__":
    main()
