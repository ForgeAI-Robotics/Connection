"""Compatibility import; implementation is connection.ops.tmuxctl."""
import sys
from connection.ops import tmuxctl as _impl
sys.modules[__name__] = _impl
