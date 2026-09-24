from ops.app import create_app


def main():
    from shared.log_setup import attach_process_log
    attach_process_log('panel')
    app = create_app()
    try:
        app.run(host='0.0.0.0', port=5678, threaded=True, use_reloader=False)
    finally:
        for worker in app.extensions['workers']:
            worker.shutdown(wait=False, cancel_futures=True)


if __name__ == '__main__':
    main()
