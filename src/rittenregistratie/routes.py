"""Known-locations and known-routes lookup (plain config, no AI).

``locations.yaml``::

    Home:   { address: "Dorpsstraat 1, Utrecht" }
    Office: { address: "Keizersgracht 1, Amsterdam" }

``routes.yaml``::

    Home->Office:
      expected_km: 40
      variants: [38, 40, 43]
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class RouteInfo:
    expected_km: int
    variants: List[int]

    def nearest_variant(self, actual_km: int) -> int:
        """Return the known variant closest to the actual distance driven."""
        candidates = self.variants or [self.expected_km]
        return min(candidates, key=lambda v: abs(v - actual_km))


class RouteBook:
    def __init__(self, locations: Dict[str, dict], routes: Dict[str, dict]):
        self._locations = locations or {}
        self._routes: Dict[str, RouteInfo] = {}
        for key, val in (routes or {}).items():
            self._routes[key] = RouteInfo(
                expected_km=int(val.get("expected_km", 0)),
                variants=[int(v) for v in val.get("variants", [])],
            )

    def address_for(self, name: str) -> str:
        loc = self._locations.get(name)
        if loc and loc.get("address"):
            return loc["address"]
        return name

    def is_known(self, name: str) -> bool:
        """True if the name maps to a known location with an address."""
        loc = self._locations.get(name)
        return bool(loc and loc.get("address"))

    def lookup(self, origin: str, destination: str) -> Optional[RouteInfo]:
        return self._routes.get(f"{origin}->{destination}")
