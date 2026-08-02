"""Plugin discovery via setuptools entry points.

External packages register implementations under the same groups
(``rittenregistratie.odometer`` etc.) to add or override behaviour.
"""
from __future__ import annotations

from importlib.metadata import entry_points
from typing import Type

GROUPS = {
    "odometer": "rittenregistratie.odometer",
    "trajectory": "rittenregistratie.trajectory",
    "privatecap": "rittenregistratie.privatecap",
}


def _load(group_key: str, name: str) -> Type:
    group = GROUPS[group_key]
    eps = entry_points(group=group)
    for ep in eps:
        if ep.name == name:
            return ep.load()
    available = ", ".join(sorted(ep.name for ep in eps)) or "(none)"
    raise KeyError(
        f"No '{name}' plugin in group '{group}'. Available: {available}"
    )


def get_odometer_source(name: str):
    return _load("odometer", name)


def get_trajectory_provider(name: str):
    return _load("trajectory", name)


def get_private_cap_plugin(name: str):
    return _load("privatecap", name)
