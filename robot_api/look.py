"""Compatibility import of the shared entry contract."""
import sys
from connection.contracts import look as _impl
sys.modules[__name__] = _impl
