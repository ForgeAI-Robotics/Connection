"""Compatibility import; implementation is connection.ops.vla_remote."""
import sys
from connection.ops import vla_remote as _impl
sys.modules[__name__] = _impl
