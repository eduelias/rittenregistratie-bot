"""Plugin interfaces (ABCs) — the stable contract for extension packages.

Four plug points:
- OdometerSource     : where odometer readings come from.
- TrajectoryProvider : how a route between two addresses is resolved.
- DeltaAllocator     : what to do with excess km ('delta') vs. expected route.
- PrivateCapPlugin   : what happens as private km approach / exceed the cap.

The core ships only *safe* defaults: the default DeltaAllocator does nothing but
report, and the default PrivateCapPlugin only warns. Implementations that
generate or reclassify trips live in separate packages.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Optional

from ..models import (
    CapAction,
    OdometerReading,
    TrajectoryResult,
    Trip,
    VirtualTrip,
)


class OdometerSource(ABC):
    @abstractmethod
    def get_reading(self) -> Optional[OdometerReading]:
        """Return the latest odometer reading, or None if unavailable."""


class TrajectoryProvider(ABC):
    @abstractmethod
    def get_trajectory(self, origin: str, destination: str) -> TrajectoryResult:
        """Resolve the route between two addresses."""


class DeltaAllocator(ABC):
    @abstractmethod
    def allocate(
        self,
        delta_pool_km: int,
        locations: dict,
        routes: dict,
        year: int,
        now: datetime,
    ) -> List[VirtualTrip]:
        """Optionally turn accumulated delta into virtual trips.

        The core default returns an empty list (never fabricates trips).
        """


class PrivateCapPlugin(ABC):
    @abstractmethod
    def on_evaluate(
        self,
        year: int,
        private_total_km: int,
        cap_km: int,
        trips: List[Trip],
    ) -> CapAction:
        """React to the current private total (warn and/or convert)."""
