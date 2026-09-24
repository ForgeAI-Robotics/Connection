"""Compatibility launcher for the operations Redis helper."""
import sys
from ops import redis_service as _impl
if __name__ == '__main__':
    _impl.main()
else:
    sys.modules[__name__] = _impl
