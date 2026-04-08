"""
Plugin discovery for usphere-DAQ analysis plugins.

Each .py file in this directory (except __init__.py and base.py) is scanned
for a ``Plugin`` class that subclasses ``AnalysisPlugin``.
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

from .base import AnalysisPlugin


def discover_plugins() -> list[type[AnalysisPlugin]]:
    """Return all Plugin classes found in the plugins package."""
    plugins: list[type[AnalysisPlugin]] = []
    pkg_path = Path(__file__).parent

    for _finder, name, _ispkg in pkgutil.iter_modules([str(pkg_path)]):
        if name in ("base",):
            continue
        try:
            mod = importlib.import_module(f".{name}", package=__name__)
            cls = getattr(mod, "Plugin", None)
            if cls is not None and isinstance(cls, type) and issubclass(cls, AnalysisPlugin):
                plugins.append(cls)
        except Exception as exc:
            print(f"[plugins] Failed to load {name}: {exc}")

    return plugins
