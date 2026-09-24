"""Legacy constructor boundary; state machine implementation is shared."""
from connection.brain.kernel.runtime import *
from connection.brain.kernel.runtime import TaskRuntime as _Runtime
from connection.brain.kernel.runtime import _TRANSITIONS
from connection.brain.packages.registry import PackagePolicy
from connection.brain.storage.releases import bound_release

class TaskRuntime(_Runtime):
    def __init__(self, store, port, **kwargs):
        super().__init__(store, port, policy=PackagePolicy(), release_reader=bound_release, **kwargs)
