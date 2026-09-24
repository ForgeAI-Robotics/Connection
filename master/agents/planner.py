"""Legacy compatibility only. Production starts connection.brain."""
import sys
from connection.compat import legacy_planner as _impl
sys.modules[__name__] = _impl
