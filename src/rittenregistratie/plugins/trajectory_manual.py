"""Manual trajectory provider: no external calls; echoes the addresses."""
from __future__ import annotations

from ..models import TrajectoryResult
from .base import TrajectoryProvider


class ManualProvider(TrajectoryProvider):
    def get_trajectory(self, origin: str, destination: str) -> TrajectoryResult:
        return TrajectoryResult(summary=f"{origin} -> {destination}")
