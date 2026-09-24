"""Explicit composition. Importing this module never starts the application."""
from copy import deepcopy


def create_runtime(config, port, *, package="reception"):
    from connection.brain.kernel.runtime import TaskRuntime
    from connection.brain.packages.registry import PackagePolicy
    from connection.brain.storage.releases import bound_release
    from connection.brain.storage.tasks import KernelStore
    from connection.brain.service_support import runtime_dir
    return TaskRuntime(KernelStore(runtime_dir(config)), port,
                       config=deepcopy(config), package=package,
                       policy=PackagePolicy(), release_reader=bound_release)


def create_service(config, *, model=None, port_factory=None, inputs=None,
                   desk_planner=None, reflection=None):
    from connection.brain.service import BrainService
    from connection.brain.kernel.planner import Planner
    from connection.brain.packages.prompts import MASTER_PLANNING_PLANNING
    from connection.brain.planning import PlanningService
    from connection.brain.learning.experiences import Experiences
    from connection.brain.adapters.planning_inputs import PlanningInputs
    from connection.brain.adapters.ports import build_port
    from connection.paths import workspace_root

    config = deepcopy(config)
    if model is None:
        from connection.brain.adapters.model import ModelClient
        model = ModelClient(config)
    planner = Planner(model, MASTER_PLANNING_PLANNING,
                      attempts=int((config.get("model") or {}).get("model_retry_planning", 0)) + 1)
    experiences = Experiences(workspace_root() / "master/memory", planner,
                             (config.get("experience") or {}).get("exploration_rate", .8))
    transports = []
    import threading
    transport_lock = threading.Lock()

    def transport():
        with transport_lock:
            if not transports:
                from connection.brain.adapters.slaver import SlaverTransport
                transports.append(SlaverTransport(config))
            return transports[0]

    def make_port(backend):
        return build_port(config, backend, agent=transport() if backend.startswith("slaver:") else None)

    planning = PlanningService(config, planner, inputs or PlanningInputs(config, transport=transport),
                               experiences, desk_planner=desk_planner)
    service = BrainService(config, planning, port_factory or make_port, reflection=reflection)
    service.experiences = experiences
    service.transports = transports
    return service
