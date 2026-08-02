"""Default PrivateCapPlugin: warns only, never reclassifies trips.

Auto-conversion of over-cap private km lives in a separate package.
"""
from __future__ import annotations

from typing import List

from ..models import CapAction, Trip
from .base import PrivateCapPlugin


class WarnPlugin(PrivateCapPlugin):
    def on_evaluate(
        self,
        year: int,
        private_total_km: int,
        cap_km: int,
        trips: List[Trip],
        context: dict | None = None,
    ) -> CapAction:
        if private_total_km > cap_km:
            over = private_total_km - cap_km
            return CapAction(
                message=(
                    f"WARNING: private {private_total_km} km exceeds the "
                    f"{cap_km} km cap by {over} km for {year}."
                )
            )
        remaining = cap_km - private_total_km
        return CapAction(
            message=f"Private {private_total_km}/{cap_km} km ({remaining} km left)."
        )
