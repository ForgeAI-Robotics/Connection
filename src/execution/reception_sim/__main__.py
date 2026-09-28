"""Run one NAV or VLA protocol simulator; both use the same local state file."""
import argparse

from execution.reception_sim.server import create_app, PORTS
from execution.reception_sim.store import SCENARIOS
from shared.paths import workspace_root


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=PORTS)
    parser.add_argument("--port", type=int)
    parser.add_argument("--state", default=str(workspace_root() / "data/reception_sim/state.sqlite3"))
    parser.add_argument("--delay", type=float, default=2.0)
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="success")
    parser.add_argument("--nav-url", default="http://127.0.0.1:18001")
    args = parser.parse_args()
    from shared.log_setup import attach_process_log
    attach_process_log("reception_" + args.role)
    app = create_app(args.role, args.state, delay=args.delay, scenario=args.scenario, nav_url=args.nav_url)
    app.run(host="127.0.0.1", port=args.port or PORTS[args.role], threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
