"""Explicit production policy wiring for mechanism-level test fixtures."""
from brain.kernel.runtime import TaskRuntime as Runtime
from brain.packages.registry import PackagePolicy
from brain.storage.releases import bound_release

def TaskRuntime(store, port, **kwargs):
    return Runtime(store, port, policy=PackagePolicy(), release_reader=bound_release, **kwargs)
