"""Compatibility import; implementation is connection.ops.execution."""
import sys
from connection.ops import execution as _impl
sys.modules[__name__] = _impl
