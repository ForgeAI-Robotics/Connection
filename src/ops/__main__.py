import threading

from ops.app import create_app


def _start_brain_layer():
    try:
        from ops.control import ensure_brain_layer
        ensure_brain_layer()
    except Exception as exc:
        print(f"大脑层随面板启动未完成：{exc}", flush=True)


def main():
    from shared.log_setup import attach_process_log
    attach_process_log('panel')
    app = create_app()
    threading.Thread(target=_start_brain_layer, name='ops-brain-layer', daemon=True).start()
    try:
        app.run(host='0.0.0.0', port=5678, threaded=True, use_reloader=False)
    finally:
        for worker in app.extensions['workers']:
            worker.shutdown(wait=False, cancel_futures=True)


if __name__ == '__main__':
    main()
