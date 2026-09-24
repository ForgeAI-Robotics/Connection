"""Installed code, operator configuration and mutable data have separate roots."""
from pathlib import Path
import os

def workspace_root() -> Path:
    configured = os.environ.get("CONNECTION_WORKSPACE")
    if configured:
        return Path(configured).expanduser().resolve()
    checkout = Path(__file__).resolve().parents[2]
    if (checkout / "pyproject.toml").is_file() and (checkout / "src").is_dir():
        return checkout
    return Path.home() / ".local/share/connection"

def data_path(value, *, relative_to="") -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else workspace_root() / relative_to / path

def business_config(name) -> Path:
    return workspace_root() / "config" / "business" / name
