"""Compatibility import; implementation is connection.ops.services."""
import sys
from connection.ops import services as _impl
sys.modules[__name__] = _impl
