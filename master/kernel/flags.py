"""Default-off switch. Missing or false keeps the existing reception path."""


def kernel_enabled(config) -> bool:
    real = {}
    if isinstance(config, dict):
        real = config.get("reception_real") or {}
    return real.get("kernel_enabled") is True
