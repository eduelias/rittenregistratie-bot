"""Known-locations and known-routes lookup (plain config, no AI).

``locations.yaml``::

    Home:   { address: "Dorpsstraat 1, Utrecht" }
    Office: { address: "Keizersgracht 1, Amsterdam" }
    hq:     { address: "Office" }      # alias: resolves to Office's address

``routes.yaml``::

    Home->Office:
      expected_km: 40
      variants: [38, 40, 43]

Location names are matched case- and whitespace-insensitively, so ``Home``,
``home`` and ``" HOME "`` are one location. An address that is itself the name
of another known location is followed (a few hops at most), so every alias
yields the single canonical address string instead of the alias name.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

_MAX_ALIAS_HOPS = 5


def normalize_name(name: str) -> str:
    """Canonical form of a location name: trimmed, lower-case, single spaces."""
    return " ".join((name or "").strip().lower().split())


@dataclass
class RouteInfo:
    expected_km: int
    variants: List[int]

    def nearest_variant(self, actual_km: int) -> int:
        """Return the known variant closest to the actual distance driven."""
        candidates = self.variants or [self.expected_km]
        return min(candidates, key=lambda v: abs(v - actual_km))


class RouteBook:
    def __init__(
        self,
        locations: Dict[str, dict],
        routes: Dict[str, dict],
        *extra_locations: Dict[str, dict],
    ):
        """Build the lookup from one or more location mappings.

        Later mappings override earlier ones for the same (normalized) name, so
        pass the committed seed config first and learned locations last.
        """
        self._locations: Dict[str, dict] = {}
        for source in (locations, *extra_locations):
            for key, val in (source or {}).items():
                if isinstance(val, dict):
                    self._locations[normalize_name(str(key))] = val
        self._routes: Dict[str, RouteInfo] = {}
        for key, val in (routes or {}).items():
            self._routes[self._route_key(str(key))] = RouteInfo(
                expected_km=int(val.get("expected_km", 0)),
                variants=[int(v) for v in val.get("variants", [])],
            )

    @staticmethod
    def _route_key(key: str) -> str:
        if "->" in key:
            origin, dest = key.split("->", 1)
            return f"{normalize_name(origin)}->{normalize_name(dest)}"
        return normalize_name(key)

    def _raw_address(self, name: str) -> str:
        loc = self._locations.get(normalize_name(name))
        if loc and loc.get("address"):
            return str(loc["address"]).strip()
        return ""

    def address_for(self, name: str) -> str:
        """Resolve a name to its address, following aliases to other names.

        Unknown names are returned unchanged (they are already an address or
        free text).
        """
        address = self._raw_address(name)
        if not address:
            return name
        seen = {normalize_name(name)}
        for _ in range(_MAX_ALIAS_HOPS):
            key = normalize_name(address)
            if key in seen or key not in self._locations:
                break
            nxt = self._raw_address(address)
            if not nxt or normalize_name(nxt) == key:
                break
            seen.add(key)
            address = nxt
        return address

    def is_known(self, name: str) -> bool:
        """True if the name maps to a known location with an address."""
        return bool(self._raw_address(name))

    def lookup(self, origin: str, destination: str) -> Optional[RouteInfo]:
        return self._routes.get(
            f"{normalize_name(origin)}->{normalize_name(destination)}"
        )
