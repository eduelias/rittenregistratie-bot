"""Documented stub for a future Mercedes connected-vehicle odometer source.

Mercedes exposes cloud REST APIs (Mercedes-Benz Developer platform) but:
- there is no public MBUX API and no trip/route API;
- odometer access requires OAuth owner consent, a supported region, and
  production approval that is commercially gated.

This stub intentionally returns no reading. A real implementation belongs in a
separate package once production access is confirmed.
"""
from __future__ import annotations

from typing import Optional

from ..models import OdometerReading
from .base import OdometerSource


class MercedesStubSource(OdometerSource):
    def get_reading(self) -> Optional[OdometerReading]:  # pragma: no cover
        return None
