"""Trajectory provider that builds a Google Maps directions URL (no API key)."""
from __future__ import annotations

from urllib.parse import quote_plus

from ..models import TrajectoryResult
from .base import TrajectoryProvider


class MapsLinkProvider(TrajectoryProvider):
    def get_trajectory(self, origin: str, destination: str) -> TrajectoryResult:
        url = (
            "https://www.google.com/maps/dir/?api=1"
            f"&origin={quote_plus(origin)}"
            f"&destination={quote_plus(destination)}"
        )
        return TrajectoryResult(summary=f"{origin} -> {destination}", url=url)
