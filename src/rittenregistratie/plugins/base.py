"""Plugin interfaces (ABCs) — the stable contract for extension packages.

Three plug points:
- OdometerSource     : where odometer readings come from.
- TrajectoryProvider : how a route between two addresses is resolved.
- PrivateCapPlugin   : how to report on private km vs. the yearly cap.

The core ships only *safe* defaults. The default PrivateCapPlugin only reports a
message and never modifies recorded trips. The core records trips exactly as the
user reports them and never fabricates, invents, or reclassifies them.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

from ..models import (
    CapAction,
    OdometerReading,
    TrajectoryResult,
    Trip,
)


class OdometerSource(ABC):
    @abstractmethod
    def get_reading(self) -> Optional[OdometerReading]:
        """Return the latest odometer reading, or None if unavailable."""


class TrajectoryProvider(ABC):
    @abstractmethod
    def get_trajectory(self, origin: str, destination: str) -> TrajectoryResult:
        """Resolve the route between two addresses."""


class PrivateCapPlugin(ABC):
    @abstractmethod
    def on_evaluate(
        self,
        year: int,
        private_total_km: int,
        cap_km: int,
        trips: List[Trip],
        context: Optional[dict] = None,
    ) -> CapAction:
        """Report on the current private total against the cap.

        ``context`` is an optional, neutral bag of runtime information (e.g.
        data directory and car id) that a plugin *may* use. The core itself
        never modifies recorded trips; it only records what the user reports.
        """
