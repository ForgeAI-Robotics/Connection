"""Compatibility import; implementation lives in the installed connection package."""
import importlib as _importlib
import sys as _sys
_module = _importlib.import_module('connection.brain.kernel.runner')
_sys.modules[__name__] = _module
