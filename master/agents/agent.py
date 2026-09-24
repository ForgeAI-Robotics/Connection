"""Legacy compatibility only. Production starts connection.brain."""
import sys
from connection.compat import legacy_agent as _impl
sys.modules[__name__] = _impl
