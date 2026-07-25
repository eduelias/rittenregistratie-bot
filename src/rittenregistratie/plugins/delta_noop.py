"""Default DeltaAllocator: does nothing but track. Never fabricates trips.

Trip-generating allocators (rule-based or LLM) live in separate packages.
"""
from __future__ import annotations

from datetime import datetime
from typing import List

from ..models import VirtualTrip
from .base import DeltaAllocator


class NoopAllocator(DeltaAllocator):
    def allocate(
        self,
        delta_pool_km: int,
        locations: dict,
        routes: dict,
        year: int,
        now: datetime,
    ) -> List[VirtualTrip]:
        return []
