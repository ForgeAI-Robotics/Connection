"""Explicit YAML loading with environment placeholders; no import-time I/O."""
import os
import re
from pathlib import Path
import yaml


def load_config(path):
    def expand(value):
        if isinstance(value, dict):
            return {key: expand(item) for key, item in value.items()}
        if isinstance(value, list):
            return [expand(item) for item in value]
        if isinstance(value, str):
            return re.sub(r'\$\{(\w+)\}', lambda m: os.environ.get(m[1], m[0]), value)
        return value
    config = expand(yaml.safe_load(Path(path).read_text()))
    if not isinstance(config, dict):
        raise ValueError('配置必须是 YAML 映射')
    return config
