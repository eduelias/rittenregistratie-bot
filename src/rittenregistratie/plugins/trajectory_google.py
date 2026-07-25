"""Trajectory provider backed by the Google Directions API.

Falls back to a plain Maps link (never loses the trip) when no API key is
configured or the request fails.
"""
from __future__ import annotations

import httpx

from ..models import TrajectoryResult
from .base import TrajectoryProvider
from .trajectory_link import MapsLinkProvider

_ENDPOINT = "https://maps.googleapis.com/maps/api/directions/json"


class GoogleDirectionsProvider(TrajectoryProvider):
    def __init__(self, api_key: str = "") -> None:
        self.api_key = api_key
        self._fallback = MapsLinkProvider()

    def get_trajectory(self, origin: str, destination: str) -> TrajectoryResult:
        if not self.api_key:
            return self._fallback.get_trajectory(origin, destination)
        try:
            resp = httpx.get(
                _ENDPOINT,
                params={
                    "origin": origin,
                    "destination": destination,
                    "key": self.api_key,
                },
                timeout=10.0,
            )
            resp.raise_for_status()
            data = resp.json()
            routes = data.get("routes") or []
            if not routes:
                return self._fallback.get_trajectory(origin, destination)
            leg = routes[0]["legs"][0]
            distance_km = round(leg["distance"]["value"] / 1000.0, 1)
            summary = routes[0].get("summary") or f"{origin} -> {destination}"
            link = self._fallback.get_trajectory(origin, destination).url
            return TrajectoryResult(
                summary=summary,
                distance_km=distance_km,
                url=link,
                raw={"status": data.get("status")},
            )
        except (httpx.HTTPError, KeyError, ValueError):
            return self._fallback.get_trajectory(origin, destination)
