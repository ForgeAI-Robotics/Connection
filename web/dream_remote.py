"""Compatibility import; implementation is connection.ops.dream_remote."""
import sys
from connection.ops import dream_remote as _impl
sys.modules[__name__] = _impl
