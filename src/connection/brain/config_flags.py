"""Real-body permission and the application scheduler selection."""


def kernel_enabled(config) -> bool:
    real = {}
    if isinstance(config, dict):
        real = config.get("reception_real") or {}
    return real.get("kernel_enabled") is True


def scheduler_runtime(config) -> bool:
    """All task packages use Runtime unless explicitly rolled back to legacy."""
    brain = {}
    if isinstance(config, dict):
        brain = config.get("brain") or {}
    mode = str(brain.get("scheduler") or "runtime").strip().lower()
    if mode not in {"runtime", "legacy"}:
        raise ValueError(f"未知 brain.scheduler: {mode}")
    return mode == "runtime"
