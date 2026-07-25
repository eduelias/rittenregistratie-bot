"""Default odometer source: readings come from the WhatsApp message itself.

The reading is supplied by the webhook, so this source is a passive holder.
"""
from __future__ import annotations

from typing import Optional

from ..models import OdometerReading
from .base import OdometerSource


class WhatsAppManualSource(OdometerSource):
    def __init__(self) -> None:
        self._pending: Optional[OdometerReading] = None

    def set_reading(self, reading: OdometerReading) -> None:
        self._pending = reading

    def get_reading(self) -> Optional[OdometerReading]:
        return self._pending
