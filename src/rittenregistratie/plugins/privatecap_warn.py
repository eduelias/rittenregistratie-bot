"""Default PrivateCapPlugin: warns only, never reclassifies trips.

Auto-conversion of over-cap private km lives in a separate package.
"""
from __future__ import annotations

from typing import List

from ..i18n import t
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
                message=t("cap.over", km=private_total_km, cap=cap_km, over=over, year=year),
                important=True,
            )
        remaining = cap_km - private_total_km
        return CapAction(message=t("cap.status", km=private_total_km, cap=cap_km, left=remaining))
