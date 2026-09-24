"""Compatibility import for the installed package."""
import importlib as _importlib
import sys as _sys
_sys.modules[__name__] = _importlib.import_module('connection.entries.feishu.bridge')
